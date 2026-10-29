"""Наблюдение за PR без нового коммита: таски демона закрыли вручную — снять NEEDS_WORK.

Люди закрывают таски, с которыми не согласны (ложные срабатывания). Когда закрыты все таски
демона, а PR ``resolve_delay_minutes`` стоит на том же проверенном коммите, «Требуются
изменения» снимается (APPROVED — если все проверки прошли). Тишина нужна, чтобы не одобрить
PR между «поправил и закрыл таски» и пушем: пуш проверит обычный прогон.

Запросов в покое нет: изменения видны по списку PR опроса — статусу ревьюера и счётчикам
тасок. Таски сверяются, только когда счётчики изменились, истекла тишина или — для
страховки — давно не сверялись.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from types import MappingProxyType

from gigareview.bitbucket import BitbucketApi, PullRequest
from gigareview.config import Config
from gigareview.errors import GigaReviewError
from gigareview.pipeline.status import rewrite_summary
from gigareview.pipeline.tracking import fetch_task_states
from gigareview.scenarios import settled_text
from gigareview.statuses import EventLevel, Outcome, ReviewStatus, RunStatus
from gigareview.store import RunRecord, Store, TaskWatchRecord
from gigareview.store.clock import utc_now

__all__ = ["TaskWatch"]

#: Пауза после ошибки Bitbucket.
_RETRY = timedelta(minutes=5)
#: Страховочная сверка, когда счётчики тасок есть в списке PR.
_RECHECK = timedelta(hours=6)
#: Без счётчиков: (сколько прошло после прогона, как часто сверять); дальше — раз в 2 часа.
_FALLBACK = (
    (timedelta(hours=1), _RETRY),
    (timedelta(days=1), timedelta(hours=0.5)),
)
_FALLBACK_LATE = timedelta(hours=2)

_EVENTS = MappingProxyType({
    ReviewStatus.APPROVED: "все таски gigareview закрыты вручную — PR одобрен",
    ReviewStatus.UNAPPROVED: "все таски gigareview закрыты вручную — «Требуются изменения» сняты",
    ReviewStatus.NEEDS_WORK: "таска gigareview открыта заново — снова «Требуются изменения»",
})


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
        run = self._store.watch.last_published(pr.project, pr.repo, pr.id)
        # Только прогон с замечаниями на текущем коммите: новый коммит (и force-push на
        # старый) проверит прогон, у прогона в очереди статус поставит он сам.
        if run is None or run.commit_id != pr.commit or run.status is not RunStatus.ISSUES:
            return
        if self._store.watch.has_active(pr.project, pr.repo, pr.id):
            return
        watch = self._store.watch.get(pr.project, pr.repo, pr.id, run.id)
        now = utc_now()
        if watch.retry_after is not None and now < watch.retry_after:
            return
        try:
            watch = await self._observe(pr, run, watch, config)
        except GigaReviewError as error:
            watch = replace(watch, retry_after=now + _RETRY)
            self._event(EventLevel.WARNING, f"{_label(pr)}: проверка тасок: {error}", run.id)
        self._store.watch.save(watch)

    async def _observe(
        self,
        pr: PullRequest,
        run: RunRecord,
        watch: TaskWatchRecord,
        config: Config,
    ) -> TaskWatchRecord:
        """Сверка тасок, если она нужна, и решение о статусе; возвращает ход наблюдения."""
        expected = watch.settled_status or ReviewStatus.NEEDS_WORK
        slug = await self._api.current_slug()
        # Статус сменили вручную (или его сбросил Bitbucket) — решение за человеком.
        if pr.reviewer_status(slug) != expected:
            return watch
        now = utc_now()
        counts = _counts(pr)
        delay = timedelta(seconds=config.daemon.resolve_delay_seconds)
        if not _due(watch, run, counts, now, delay):
            return watch
        states = await fetch_task_states(
            self._api,
            self._store,
            pr,
            counts=pr.task_counts,
            slug=slug,
        )
        blocked = states.manual > 0 or bool(
            self._store.comments.open_tasks(pr.project, pr.repo, pr.id),
        )
        watch = replace(watch, task_counts=counts, checked_at=now, retry_after=None)
        if blocked:
            watch = replace(watch, clear_since=None)
            if watch.settled_status:
                await self._reopen(pr, run, watch, config)
                watch = replace(watch, settled_status="")
            return watch
        clear_since = watch.clear_since or now
        watch = replace(watch, clear_since=clear_since)
        if watch.settled_status or now - clear_since < delay:
            return watch
        target = self._target(run, config)
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
        unchanged = fresh.state == "OPEN" and fresh.commit == run.commit_id
        if not unchanged or current != expected:
            return False
        if self._store.watch.has_active(pr.project, pr.repo, pr.id):
            return False
        await self._api.set_review_status(fresh, target)
        self._event(EventLevel.NOTE, f"{_label(pr)}: {_EVENTS[target]}", run.id)
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
            message = f"{_label(pr)}: не удалось обновить статусный комментарий: {error}"
            self._event(EventLevel.WARNING, message, run.id)
        return True

    def _event(self, level: EventLevel, message: str, run_id: int) -> None:
        self._store.events.add(level, message, run_id)


def _due(
    watch: TaskWatchRecord,
    run: RunRecord,
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
    return now - watch.checked_at >= _recheck(run, counts, now)


def _recheck(run: RunRecord, counts: str, now: datetime) -> timedelta:
    """Как часто сверять таски для страховки: без счётчиков — чаще сразу после прогона."""
    if counts:
        return _RECHECK
    age = now - (run.finished_at or run.created_at)
    return next((every for limit, every in _FALLBACK if age < limit), _FALLBACK_LATE)


def _counts(pr: PullRequest) -> str:
    """Счётчики тасок PR строкой «открыто/закрыто»; пусто — Bitbucket их не отдал."""
    counts = pr.task_counts
    return "" if counts is None else f"{counts[0]}/{counts[1]}"


def _label(pr: PullRequest) -> str:
    """``PROJECT/repo#id`` для журнала."""
    return f"{pr.project}/{pr.repo}#{pr.id}"
