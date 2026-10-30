"""Наблюдение за PR без нового коммита: таски демона закрыли вручную — снять NEEDS_WORK.

Люди закрывают таски, с которыми не согласны (ложные срабатывания). Когда закрыты все таски
демона, а PR ``resolve_delay_minutes`` стоит на том же проверенном коммите, «Требуются
изменения» снимается (APPROVED — если все проверки прошли). Тишина нужна, чтобы не одобрить
PR между «поправил и закрыл таски» и пушем: пуш проверит обычный прогон. Наблюдение ведётся и
после прогона, где часть проверок не удалась: NEEDS_WORK прошлых прогонов не должен висеть,
когда все таски закрыты.

Запросов в покое нет: изменения видны по списку PR опроса — статусу ревьюера и счётчикам
тасок. Таски сверяются, только когда счётчики изменились, истекла тишина или — для
страховки — давно не сверялись. Каждое решение пишется в журнал; то же решение по PR подряд
не повторяется.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from types import MappingProxyType

from gigareview.bitbucket import BitbucketApi, PullRequest
from gigareview.config import Config
from gigareview.errors import GigaReviewError
from gigareview.pipeline.status import rewrite_summary
from gigareview.pipeline.tracking import TaskStates, fetch_task_states
from gigareview.scenarios import settled_text
from gigareview.statuses import EventLevel, Outcome, ReviewStatus, RunStatus
from gigareview.store import RunRecord, Store, TaskWatchRecord
from gigareview.store.clock import clock_time, utc_now

__all__ = ["TaskWatch"]

#: Пауза после ошибки Bitbucket.
_RETRY = timedelta(minutes=5)
#: Страховочная сверка, когда счётчики тасок есть в списке PR.
_RECHECK = timedelta(hours=6)
#: Без счётчиков: сверка, пока PR ждёт закрытия тасок, и после снятия NEEDS_WORK (ловим
#: только открытую заново таску).
_FALLBACK = timedelta(minutes=5)
_FALLBACK_SETTLED = timedelta(hours=0.5)
#: Прогоны, после которых PR может остаться в NEEDS_WORK.
_WATCHED = frozenset((RunStatus.ISSUES, RunStatus.FAILED))
#: Сколько номеров тасок перечислять в журнале.
_SHOWN_TASKS = 5
_SECONDS_IN_MINUTE = 60

_EVENTS = MappingProxyType({
    ReviewStatus.APPROVED: "все таски gigareview закрыты вручную — PR одобрен",
    ReviewStatus.UNAPPROVED: "все таски gigareview закрыты вручную — «Требуются изменения» сняты",
    ReviewStatus.NEEDS_WORK: "таска gigareview открыта заново — снова «Требуются изменения»",
})
_NO_COUNTS = (
    "Bitbucket не отдаёт счётчики тасок в списке PR (properties.openTaskCount): закрытие тасок "
    "наблюдение замечает сверкой раз в 5 мин, после снятия NEEDS_WORK — раз в 30 мин"
)
_COUNTS_KEY = "watch:counts"


class TaskWatch:
    """Следит за PR, которые опрос пропустил (нового коммита нет), после прогона с замечаниями."""

    def __init__(self, store: Store, api: BitbucketApi) -> None:
        """Наблюдение над базой ``store`` и Bitbucket ``api``."""
        self._store = store
        self._api = api

    async def check(self, prs: Sequence[PullRequest], config: Config) -> None:
        """PR из опроса без нового коммита; сбой по одному PR не мешает остальным."""
        if config.daemon.dry_run:
            return
        for pr in prs:
            # PR по очереди: запросов почти никогда нет, а ошибку каждого PR — в журнал.
            await self._check(pr, config)  # noqa: WPS476

    async def _check(self, pr: PullRequest, config: Config) -> None:
        """Один PR: дешёвые проверки по базе, затем — по ответу опроса."""
        run = self._watched_run(pr)
        if run is None:
            return
        watch = self._store.watch.get(pr.project, pr.repo, pr.id, run.id)
        now = utc_now()
        if watch.retry_after is not None and now < watch.retry_after:
            return
        try:
            watch = await self._observe(pr, run, watch, config)
        except GigaReviewError as error:
            watch = replace(watch, retry_after=now + _RETRY)
            message = f"проверка тасок: {error}; повтор после {clock_time(now + _RETRY)}"
            self._note(pr, EventLevel.WARNING, message, run.id)
        self._store.watch.save(watch)

    def _watched_run(self, pr: PullRequest) -> RunRecord | None:
        """Прогон, после которого наблюдение следит за PR; нет такого — причина в журнал.

        Только прогон с замечаниями, с упавшими проверками (или придержанный ручной таской)
        на текущем коммите: новый коммит (и force-push на старый) проверит прогон, у прогона в
        очереди статус поставит он сам.
        """
        run = self._store.watch.last_published(pr.project, pr.repo, pr.id)
        if run is None or run.commit_id != pr.commit:
            self._unpublished(pr)
            return None
        if run.status not in _WATCHED and not self._store.watch.is_held(run.id):
            message = f"прогон #{run.id} ({run.status}) — наблюдать нечего"
            self._note(pr, EventLevel.DEBUG, message, run.id)
            return None
        active = self._store.watch.active_run(pr.project, pr.repo, pr.id)
        if active is not None:
            self._note(pr, EventLevel.DEBUG, _active_text(active), active.id)
            return None
        return run

    def _unpublished(self, pr: PullRequest) -> None:
        """Итоги текущего коммита не опубликованы: почему — в журнал."""
        latest = self._store.watch.latest_on_commit(pr.project, pr.repo, pr.id, pr.commit)
        if latest is None:
            self._note(pr, EventLevel.DEBUG, "опубликованного прогона коммита нет", None)
        elif latest.status.is_active:
            self._note(pr, EventLevel.DEBUG, _active_text(latest), latest.id)
        else:
            message = (
                f"прогон #{latest.id} коммита {latest.short_commit} — {latest.status}, итоги не "
                "опубликованы: коммит автоматически больше не проверяется, повторить — Re-run "
                "в Web Dashboard"
            )
            self._note(pr, EventLevel.WARNING, message, latest.id)

    async def _observe(
        self,
        pr: PullRequest,
        run: RunRecord,
        watch: TaskWatchRecord,
        config: Config,
    ) -> TaskWatchRecord:
        """Сверка тасок, если она нужна, и решение о статусе; возвращает ход наблюдения."""
        slug = await self._api.current_slug()
        status = pr.reviewer_status(slug)
        # Статус сменили вручную (или его сбросил Bitbucket) — решение за человеком.
        if status != (watch.settled_status or ReviewStatus.NEEDS_WORK):
            level, message = _status_note(run, watch, status, slug)
            self._note(pr, level, message, run.id)
            return watch
        now = utc_now()
        counts = _counts(pr)
        if not counts:
            self._store.events.add(EventLevel.NOTE, _NO_COUNTS, key=_COUNTS_KEY)
        delay = timedelta(seconds=config.daemon.resolve_delay_seconds)
        # Сверка не нужна — в силе прежнее решение, журнал его уже знает.
        if not _due(watch, counts, now, delay):
            return watch
        states = await fetch_task_states(
            self._api,
            self._store,
            pr,
            counts=pr.task_counts,
            slug=slug,
        )
        self._report(pr, run, states)
        watch = replace(watch, task_counts=counts, checked_at=now, retry_after=None)
        return await self._decide(pr, run, watch, states, config)

    async def _decide(
        self,
        pr: PullRequest,
        run: RunRecord,
        watch: TaskWatchRecord,
        states: TaskStates,
        config: Config,
    ) -> TaskWatchRecord:
        """По свежему снимку тасок: держать NEEDS_WORK, ждать тишины или снять его."""
        bot_tasks = [
            record.comment_id
            for record in self._store.comments.open_tasks(pr.project, pr.repo, pr.id)
        ]
        if bot_tasks or states.manual:
            self._note(pr, EventLevel.NOTE, _blocked_text(bot_tasks, states.manual), run.id)
            watch = replace(watch, clear_since=None)
            if watch.settled_status:
                await self._reopen(pr, run, watch, config)
                watch = replace(watch, settled_status="")
            return watch
        clear_since = watch.clear_since or utc_now()
        watch = replace(watch, clear_since=clear_since)
        if watch.settled_status:
            return watch
        target = self._target(run, config)
        delay = timedelta(seconds=config.daemon.resolve_delay_seconds)
        if utc_now() - clear_since < delay:
            message = _quiet_text(clear_since + delay, target, config)
            self._note(pr, EventLevel.NOTE, message, run.id)
            return watch
        if await self._settle(pr, run, (ReviewStatus.NEEDS_WORK, target), config):
            watch = replace(watch, settled_status=target.value)
        return watch

    async def _reopen(
        self,
        pr: PullRequest,
        run: RunRecord,
        watch: TaskWatchRecord,
        config: Config,
    ) -> None:
        """Таску открыли заново после снятия NEEDS_WORK: вернуть «Требуются изменения»."""
        change = (ReviewStatus(watch.settled_status), ReviewStatus.NEEDS_WORK)
        await self._settle(pr, run, change, config)

    def _target(self, run: RunRecord, config: Config) -> ReviewStatus:
        """APPROVED, если можно одобрять и все проверки прогона отработали; иначе UNAPPROVED."""
        failed = any(
            record.outcome is Outcome.ERROR for record in self._store.runs.scenarios(run.id)
        )
        if config.daemon.approve and not failed:
            return ReviewStatus.APPROVED
        return ReviewStatus.UNAPPROVED

    async def _settle(
        self,
        pr: PullRequest,
        run: RunRecord,
        change: tuple[ReviewStatus, ReviewStatus],
        config: Config,
    ) -> bool:
        """Меняет статус ``change`` (был → станет) по свежему PR и переписывает итог.

        Перед записью PR перечитывается: его могли закрыть, запушить в него или сменить
        статус; за время запросов мог встать в очередь прогон — тогда статус поставит он.
        """
        expected, target = change
        fresh = await self._api.get_pull_request(pr.project, pr.repo, pr.id)
        current = fresh.reviewer_status(await self._api.current_slug())
        obstacle = self._obstacle(fresh, run, current, expected)
        if obstacle:
            self._note(pr, EventLevel.NOTE, obstacle, run.id)
            return False
        await self._api.set_review_status(fresh, target)
        self._note(pr, EventLevel.NOTE, _EVENTS[target], run.id)
        base_url = config.dashboard.base_url
        run_url = f"{base_url}/runs/{run.id}"
        try:
            await rewrite_summary(
                self._api,
                self._store,
                fresh,
                run.id,
                lambda table: settled_text(table, target, run_url=run_url, policy=config.daemon),
            )
        # Статус уже поставлен: устаревший итог комментария — не повод повторять.
        except GigaReviewError as error:
            message = f"не удалось обновить статусный комментарий: {error}"
            self._event(pr, EventLevel.WARNING, message, run.id)
        return True

    def _obstacle(
        self,
        fresh: PullRequest,
        run: RunRecord,
        current: str | None,
        expected: ReviewStatus,
    ) -> str:
        """Почему статус по свежему PR менять нельзя; пусто — можно."""
        if fresh.state != "OPEN":
            return f"PR уже не открыт ({fresh.state}) — статус не меняется"
        if fresh.commit != run.commit_id:
            return "в PR новый коммит — статус поставит его прогон"
        if current != expected:
            return f"статус ревью уже {current}, а не {expected} — наблюдение не вмешивается"
        active = self._store.watch.active_run(fresh.project, fresh.repo, fresh.id)
        return "" if active is None else _active_text(active)

    def _report(self, pr: PullRequest, run: RunRecord, states: TaskStates) -> None:
        """Что изменила сверка: таски, закрытые людьми и открытые заново."""
        if states.dismissed:
            message = f"таски закрыты вручную: {_ids(states.dismissed)}"
            self._event(pr, EventLevel.NOTE, message, run.id)
        if states.reopened:
            message = f"таски открыты заново: {_ids(states.reopened)}"
            self._event(pr, EventLevel.NOTE, message, run.id)

    def _note(self, pr: PullRequest, level: EventLevel, message: str, run_id: int | None) -> None:
        """Решение по PR — в журнал; то же решение подряд не повторяется."""
        label = _label(pr)
        self._store.events.add(level, f"{label}: {message}", run_id, key=f"watch:{label}")

    def _event(self, pr: PullRequest, level: EventLevel, message: str, run_id: int) -> None:
        """Разовое событие по PR."""
        self._store.events.add(level, f"{_label(pr)}: {message}", run_id)


def _due(
    watch: TaskWatchRecord,
    counts: str,
    now: datetime,
    delay: timedelta,
) -> bool:
    """Нужна ли сверка тасок: первое наблюдение, счётчики изменились, тишина или страховка."""
    if watch.checked_at is None or counts != watch.task_counts:
        return True
    clear_since = watch.clear_since
    if clear_since is not None and not watch.settled_status and now - clear_since >= delay:
        return True
    return now - watch.checked_at >= _recheck(watch, counts)


def _recheck(watch: TaskWatchRecord, counts: str) -> timedelta:
    """Как часто сверять таски: со счётчиками — для страховки, без них — по расписанию."""
    if counts:
        return _RECHECK
    return _FALLBACK_SETTLED if watch.settled_status else _FALLBACK


def _status_note(
    run: RunRecord,
    watch: TaskWatchRecord,
    status: str | None,
    slug: str,
) -> tuple[EventLevel, str]:
    """Уровень и текст журнала: статус ревьюера не тот, которого ждёт наблюдение."""
    if status is None:
        return (
            EventLevel.WARNING,
            f"пользователь {slug} не найден среди ревьюеров PR — проверьте bitbucket.user",
        )
    if run.status is RunStatus.FAILED and not watch.settled_status:
        return (
            EventLevel.DEBUG,
            f"прогон #{run.id}: часть проверок не удалась, статус ревью {status} — снимать нечего",
        )
    expected = watch.settled_status or ReviewStatus.NEEDS_WORK
    return (
        EventLevel.NOTE,
        f"статус ревью {status}, а не {expected} — его изменили вручную, наблюдение не вмешивается",
    )


def _blocked_text(bot_tasks: Sequence[int], manual: Sequence[int]) -> str:
    """Почему NEEDS_WORK остаётся: открытые таски бота и ручные таски пользователя демона."""
    parts = []
    if bot_tasks:
        parts.append(f"открыты таски бота {_ids(bot_tasks)}")
    if manual:
        parts.append(f"открыты ручные таски пользователя демона {_ids(manual)}")
    joined = "; ".join(parts)
    return f"«Требуются изменения» остаются: {joined}"


def _quiet_text(until: datetime, target: ReviewStatus, config: Config) -> str:
    """Все таски закрыты, идёт тишина: когда и что произойдёт."""
    minutes = format(config.daemon.resolve_delay_seconds / _SECONDS_IN_MINUTE, "g")
    if target is ReviewStatus.APPROVED:
        outcome = "PR будет одобрен"
    elif config.daemon.approve:
        outcome = "«Требуются изменения» будут сняты, но PR не одобрен: часть проверок не удалась"
    else:
        outcome = "«Требуются изменения» будут сняты (approve = false — без одобрения)"
    return (
        f"все таски бота закрыты — если до {clock_time(until)} (тишина {minutes} мин) не будет "
        f"нового коммита, {outcome}"
    )


def _active_text(run: RunRecord) -> str:
    """Прогон в очереди или в работе: статус поставит он."""
    return f"прогон #{run.id} ({run.status}, запуск: {run.trigger}) — статус поставит он"


def _ids(task_ids: Sequence[int]) -> str:
    """Номера тасок для журнала: первые несколько и сколько ещё."""
    shown = ", ".join(f"#{task_id}" for task_id in task_ids[:_SHOWN_TASKS])
    rest = len(task_ids) - _SHOWN_TASKS
    return f"{shown} и ещё {rest}" if rest > 0 else shown


def _counts(pr: PullRequest) -> str:
    """Счётчики тасок PR строкой «открыто/закрыто»; пусто — Bitbucket их не отдал."""
    counts = pr.task_counts
    return "" if counts is None else f"{counts[0]}/{counts[1]}"


def _label(pr: PullRequest) -> str:
    """``PROJECT/repo#id`` для журнала."""
    return f"{pr.project}/{pr.repo}#{pr.id}"
