"""Прогон ревью одного коммита PR от подготовки копии до публикации итогов."""

import asyncio
import dataclasses
import tempfile
from collections.abc import Sequence
from pathlib import Path

from gigareview.bitbucket import BitbucketApi, PullRequest
from gigareview.config import Config, RepoConfig, Scenario
from gigareview.errors import GigaReviewError, SetupError
from gigareview.pipeline.incremental import history_env, scenario_history, settle
from gigareview.pipeline.publish import Publisher
from gigareview.pipeline.workspace import Workspace, prepare_workspace, scenario_env
from gigareview.process import subprocess_env
from gigareview.scenarios import ScenarioContext, ScenarioReport, overall_status, run_scenario
from gigareview.statuses import EventLevel, RunStatus, Trigger
from gigareview.store import NewRun, RunRecord, ScenarioRecord, Store

__all__ = ["Reviewer", "new_run"]


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
        scenarios = self.config.scenarios_for(repo)
        if not scenarios:
            raise SetupError(f"Для {repo.key} не задано ни одного сценария")
        reports = await self._run_scenarios(run_id, repo, pr, scenarios)
        status = overall_status(reports)
        return status, await self._publish(run, pr, reports, status)

    async def _run_scenarios(
        self,
        run_id: int,
        repo: RepoConfig,
        pr: PullRequest,
        scenarios: Sequence[Scenario],
    ) -> list[ScenarioReport]:
        """Готовит копию PR и выполняет сценарии по порядку, сохраняя каждый результат."""
        env = subprocess_env({}, hidden=(self.config.bitbucket.token_env,))
        workspace = await self._prepare(run_id, repo, pr, env)
        artifacts_root = self.config.daemon.home / "artifacts"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        prefix = f"run-{run_id}-"
        with tempfile.TemporaryDirectory(prefix=prefix, dir=artifacts_root) as artifacts_dir:
            artifacts = Path(artifacts_dir)
            context = ScenarioContext(
                workspace.repo_dir,
                artifacts,
                {**env, **scenario_env(pr, workspace, artifacts)},
            )
            return await self._execute(run_id, pr, scenarios, context)

    async def _execute(
        self,
        run_id: int,
        pr: PullRequest,
        scenarios: Sequence[Scenario],
        context: ScenarioContext,
    ) -> list[ScenarioReport]:
        """Сценарии по порядку; результат каждого сохраняется сразу — его видно в WD."""
        reports = []
        for position, scenario in enumerate(scenarios):
            # Сценарии идут строго по очереди: они делят одну рабочую копию.
            report = await self._run_one(pr, scenario, context)  # noqa: WPS476
            self._store.runs.add_scenario(run_id, position, _record(report))
            reports.append(report)
        return reports

    async def _run_one(
        self,
        pr: PullRequest,
        scenario: Scenario,
        context: ScenarioContext,
    ) -> ScenarioReport:
        """Сценарий с историей его прошлых ревью PR (коммит, открытые таски)."""
        history = await scenario_history(self._store, pr, scenario, context.repo_dir)
        env = {**context.env, **history_env(history, context.artifacts, scenario)}
        report = await run_scenario(scenario, dataclasses.replace(context, env=env))
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
        run: RunRecord,
        pr: PullRequest,
        reports: list[ScenarioReport],
        status: RunStatus,
    ) -> str:
        """Публикует итоги, если PR всё ещё открыт на том же коммите; иначе — примечание."""
        if run.dry_run:
            return "Сухой режим: в Bitbucket ничего не опубликовано"
        fresh = await self._api.get_pull_request(pr.project, pr.repo, pr.id)
        if fresh.state != "OPEN" or fresh.commit != pr.commit:
            note = "PR изменился во время прогона: результаты не опубликованы"
            self._event(EventLevel.WARNING, f"{_pr_label(pr)}: {note}", run.id)
            return note
        base_url = self.config.dashboard.base_url
        run_url = f"{base_url}/runs/{run.id}"
        await Publisher(self._api, self._store, run.id).publish(fresh, reports, status, run_url)
        return ""

    def _crashed(self, run_id: int, error: str) -> RunStatus:
        """Прогон упал: статус ``error`` и запись в журнале."""
        self._store.runs.finish(run_id, RunStatus.ERROR, error)
        self._event(EventLevel.ERROR, f"Прогон #{run_id} упал: {error}", run_id)
        return RunStatus.ERROR

    def _event(self, level: EventLevel, message: str, run_id: int) -> None:
        self._store.events.add(level, message, run_id)


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
    )
