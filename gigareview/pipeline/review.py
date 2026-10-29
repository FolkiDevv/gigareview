"""Прогон ревью одного коммита PR от подготовки копии до публикации итогов."""

import asyncio
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from itertools import chain
from pathlib import Path

from gigareview.bitbucket import BitbucketApi, PullRequest
from gigareview.config import Config, RepoConfig, Scenario, ScenarioKind
from gigareview.errors import GigaReviewError, SetupError
from gigareview.pipeline.incremental import history_env, scenario_history, settle
from gigareview.pipeline.publish import Publisher
from gigareview.pipeline.status import StatusBoard
from gigareview.pipeline.workspace import RepoLock, Workspace, prepare_workspace, scenario_env
from gigareview.process import subprocess_env
from gigareview.scenarios import ScenarioContext, ScenarioReport, overall_status, run_scenario
from gigareview.statuses import EventLevel, RunStatus, Trigger
from gigareview.store import NewRun, RunRecord, ScenarioRecord, Store

__all__ = ["Reviewer", "new_run"]

_DRY_RUN_NOTE = "Сухой режим: в Bitbucket ничего не опубликовано"
_CHANGED_NOTE = "PR изменился во время прогона: результаты не опубликованы"


def new_run(pr: PullRequest, trigger: Trigger, *, fingerprint: str, dry_run: bool) -> NewRun:
    """Запись нового прогона по PR."""
    return NewRun(
        project=pr.project,
        repo=pr.repo,
        pr_id=pr.id,
        pr_title=pr.title,
        pr_author=pr.author_name,
        pr_url=pr.url,
        source_branch=pr.from_ref.display_id,
        target_branch=pr.to_ref.display_id,
        commit_id=pr.commit,
        trigger=trigger,
        fingerprint=fingerprint,
        dry_run=dry_run,
    )


class Reviewer:
    """Выполняет прогоны: копия PR → init → сценарии → Bitbucket; всё пишет в базу."""

    def __init__(self, config: Config, store: Store, api: BitbucketApi) -> None:
        """Пайплайн с конфигурацией ``config``, базой ``store`` и клиентом ``api``."""
        self.config = config
        self._store = store
        self._api = api

    async def review(self, run_id: int, pr: PullRequest) -> RunStatus:
        """Выполняет прогон ``run_id`` по ``pr``; любой сбой — статус ``error`` с причиной."""
        try:
            status, note = await self._review(run_id, pr)
        except asyncio.CancelledError:
            self._store.runs.finish(run_id, RunStatus.INTERRUPTED, "Прогон прерван")
            raise
        except GigaReviewError as error:
            return self._crashed(run_id, str(error))
        except Exception as error:
            return self._crashed(run_id, f"Внутренняя ошибка: {error!r}")
        self._store.runs.finish(run_id, status, note)
        self._event(EventLevel.NOTE, f"Прогон {_pr_label(pr)} завершён: {status}", run_id)
        return status

    async def _review(self, run_id: int, pr: PullRequest) -> tuple[RunStatus, str]:
        """Прогон по шагам; возвращает статус и примечание (почему не опубликован)."""
        self._store.runs.mark_running(run_id)
        self._event(EventLevel.NOTE, f"Прогон {_pr_label(pr)} начат", run_id)
        run = self._store.runs.get(run_id)
        repo = self.config.find_repo(pr.project, pr.repo)
        if run is None or repo is None:
            raise SetupError(f"Репозиторий {pr.project}/{pr.repo} не описан в конфигурации")
        phases = _phases(self.config.scenarios_for(repo))
        if not phases:
            raise SetupError(f"Для {repo.key} не задано ни одного сценария")
        if not run.dry_run:
            # Пользователь нужен для статуса ревью: если его не определить, лучше упасть
            # до публикации, чем оставить таски без статуса и повторить их следующим прогоном.
            await self._api.current_slug()
        base_url = self.config.dashboard.base_url
        run_url = f"{base_url}/runs/{run.id}"
        # Одно решение на весь прогон: перечитанная посреди прогона конфигурация не должна
        # развести текст итога и поставленный статус.
        approve = self.config.daemon.approve
        board = StatusBoard(
            self._api,
            self._store,
            run,
            run_url,
            enabled=not run.dry_run,
            approve=approve,
        )
        progress = _Progress(run, pr, board, approve=approve)
        # Копия занята другим процессом — ошибка до того, как в PR что-то написано.
        with RepoLock(self.config.daemon.home, repo):
            # Сообщение — сразу, до подготовки копии: долгие ИИ-сценарии не должны
            # выглядеть так, будто PR никто не проверяет.
            await board.show(pr, (), pending=tuple(chain.from_iterable(phases)))
            try:
                note = await self._run_phases(progress, repo, phases)
            except Exception:
                await board.fail(pr)
                raise
        return overall_status(progress.reports), note

    async def _run_phases(
        self,
        progress: "_Progress",
        repo: RepoConfig,
        phases: Sequence[tuple[Scenario, ...]],
    ) -> str:
        """Готовит копию PR и выполняет этапы по порядку; возвращает примечание прогона."""
        env = subprocess_env({}, hidden=(self.config.bitbucket.token_env,))
        workspace = await self._prepare(progress.run.id, repo, progress.pr, env)
        artifacts_root = self.config.daemon.home / "artifacts"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        run_id = progress.run.id
        prefix = f"run-{run_id}-"
        with tempfile.TemporaryDirectory(prefix=prefix, dir=artifacts_root) as artifacts_dir:
            artifacts = Path(artifacts_dir)
            context = ScenarioContext(
                workspace.repo_dir,
                artifacts,
                {**env, **scenario_env(progress.pr, workspace, artifacts)},
            )
            return await self._run_all(progress, phases, context)

    async def _run_all(
        self,
        progress: "_Progress",
        phases: Sequence[tuple[Scenario, ...]],
        context: ScenarioContext,
    ) -> str:
        """Этапы по порядку; PR изменился — оставшиеся этапы не выполняются."""
        for index, phase in enumerate(phases):
            pending = tuple(chain.from_iterable(phases[index + 1 :]))
            # Этапы идут строго по очереди: сценарии делят одну рабочую копию.
            note = await self._phase(progress, phase, pending, context)  # noqa: WPS476
            if note:
                return note
        return _DRY_RUN_NOTE if progress.run.dry_run else ""

    async def _phase(
        self,
        progress: "_Progress",
        scenarios: Sequence[Scenario],
        pending: Sequence[Scenario],
        context: ScenarioContext,
    ) -> str:
        """Этап: сценарии по порядку (каждый результат сохраняется сразу), затем публикация."""
        phase_reports = []
        for scenario in scenarios:
            report = await self._run_one(progress.pr, scenario, context)  # noqa: WPS476
            position = len(progress.reports)
            self._store.runs.add_scenario(progress.run.id, position, _record(report))
            progress.reports.append(report)
            phase_reports.append(report)
        return await self._publish(progress, phase_reports, pending)

    async def _run_one(
        self,
        pr: PullRequest,
        scenario: Scenario,
        context: ScenarioContext,
    ) -> ScenarioReport:
        """Сценарий с историей его прошлых ревью PR (коммит, открытые таски)."""
        history = await scenario_history(
            self._api,
            self._store,
            pr,
            scenario,
            context.repo_dir,
        )
        env = {**context.env, **history_env(history, context.artifacts, scenario)}
        report = await run_scenario(scenario, replace(context, env=env))
        return settle(report, history)

    async def _prepare(
        self,
        run_id: int,
        repo: RepoConfig,
        pr: PullRequest,
        env: dict[str, str],
    ) -> Workspace:
        """Готовит копию PR; журнал подготовки сохраняется и при сбое."""
        try:
            workspace = await prepare_workspace(self.config.daemon.home, repo, pr, env)
        except GigaReviewError as error:
            self._store.runs.append_setup_log(run_id, str(error))
            raise
        self._store.runs.append_setup_log(run_id, workspace.log)
        return workspace

    async def _publish(
        self,
        progress: "_Progress",
        phase_reports: Sequence[ScenarioReport],
        pending: Sequence[Scenario],
    ) -> str:
        """Таски этапа и статусный комментарий; после последнего этапа — статус ревью.

        Публикуется, только пока PR открыт на том же коммите; иначе — примечание, и
        оставшиеся этапы не выполняются: новый коммит проверит следующий прогон.
        """
        run, pr = progress.run, progress.pr
        if run.dry_run:
            return ""
        fresh = await self._api.get_pull_request(pr.project, pr.repo, pr.id)
        if fresh.state != "OPEN" or fresh.commit != pr.commit:
            note = _CHANGED_NOTE
            if pending:
                note = f"{note}, оставшиеся сценарии не запускались"
            self._event(EventLevel.WARNING, f"{_pr_label(pr)}: {note}", run.id)
            return note
        publisher = Publisher(self._api, self._store, run.id)
        await publisher.publish_tasks(fresh, phase_reports)
        await progress.board.show(fresh, progress.reports, pending)
        if not pending:
            await publisher.finish(
                fresh,
                progress.reports,
                overall_status(progress.reports),
                approve=progress.approve,
            )
            self._store.runs.mark_published(run.id)
        return ""

    def _crashed(self, run_id: int, error: str) -> RunStatus:
        """Прогон упал: статус ``error`` и запись в журнале."""
        self._store.runs.finish(run_id, RunStatus.ERROR, error)
        self._event(EventLevel.ERROR, f"Прогон #{run_id} упал: {error}", run_id)
        return RunStatus.ERROR

    def _event(self, level: EventLevel, message: str, run_id: int) -> None:
        self._store.events.add(level, message, run_id)


@dataclass(slots=True)
class _Progress:
    """Ход прогона: запись, PR, статусный комментарий, решение об APPROVED и итоги сценариев."""

    run: RunRecord
    pr: PullRequest
    board: StatusBoard
    approve: bool
    reports: list[ScenarioReport] = field(default_factory=list)


def _phases(scenarios: Sequence[Scenario]) -> list[tuple[Scenario, ...]]:
    """Этапы прогона: сначала обычные сценарии, затем долгие ИИ-сценарии."""
    simple = tuple(scenario for scenario in scenarios if scenario.kind is ScenarioKind.SIMPLE)
    smart = tuple(scenario for scenario in scenarios if scenario.kind is ScenarioKind.AI)
    return [phase for phase in (simple, smart) if phase]


def _pr_label(pr: PullRequest) -> str:
    """``PROJECT/repo#id`` для журнала."""
    return f"{pr.project}/{pr.repo}#{pr.id}"


def _record(report: ScenarioReport) -> ScenarioRecord:
    """Запись результата сценария для базы."""
    validation = report.validation
    formatting = report.formatting
    return ScenarioRecord(
        name=report.scenario.name,
        title=report.scenario.display_title,
        kind=report.scenario.kind.value,
        outcome=report.outcome,
        run_exit=report.run.exit_code,
        run_output=report.run.output,
        duration=report.duration,
        validate_exit=None if validation is None else validation.exit_code,
        validate_output="" if validation is None else validation.output,
        format_exit=None if formatting is None else formatting.exit_code,
        format_output="" if formatting is None else f"{formatting.output}{formatting.stderr}",
        tasks=tuple(task.text for task in report.tasks),
        error=report.error,
        partial=report.partial,
    )
