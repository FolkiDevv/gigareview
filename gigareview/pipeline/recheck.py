"""Перезапуск упавших сценариев из PR: ответ «/recheck» на статусный комментарий.

ИИ-сценарии нередко не укладываются в таймаут. Автор PR или ревьюер отвечает на статусный
комментарий gigareview командой ``/recheck`` — демон ставит прогон, который выполняет только
упавшие сценарии текущего коммита (итоги остальных переносятся), не больше
``max_rechecks`` раз на коммит. Принятая команда получает реакцию 👍, отклонённая — 👎, причина
пишется в журнал.

Запросов в покое нет: новый ответ виден по счётчику комментариев PR в списке опроса; для
страховки PR, чьи проверки можно перезапустить, сверяется и раз в 30 минут. Без счётчика
статусный комментарий перечитывается раз в 5 минут, и только у таких PR: команда в остальных
PR остаётся без ответа (👎 и записи в журнале нет) — иначе сверка стоила бы запроса раз в
5 минут на каждый PR.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from http import HTTPStatus

from gigareview.bitbucket import BitbucketApi, Comment, PullRequest
from gigareview.config import Config
from gigareview.errors import BitbucketError, GigaReviewError
from gigareview.scenarios import RECHECK_COMMAND
from gigareview.statuses import EventLevel, RunStatus
from gigareview.store import RecheckWatchRecord, Store
from gigareview.store.clock import clock_time, utc_now

__all__ = ["ACCEPTED", "REJECTED", "Enqueue", "RecheckWatch"]

#: Реакции на ответ с командой: перезапуск принят или отклонён.
ACCEPTED = "thumbsup"
REJECTED = "thumbsdown"

#: Ставит прогон-перезапуск PR после прогона ``base_run_id``; возвращает номер нового прогона.
type Enqueue = Callable[[PullRequest, int], int]

#: Пауза после ошибки Bitbucket и период сверки без счётчика комментариев.
_RETRY = timedelta(minutes=5)
_FALLBACK = timedelta(minutes=5)
#: Страховочная сверка со счётчиком: он не меняется, если комментарий удалили и добавили.
_RECHECK = timedelta(hours=0.5)
#: Прогон упал целиком — перезапускается полностью.
_CRASHED = frozenset((RunStatus.ERROR, RunStatus.INTERRUPTED))
_MILLISECONDS = 1000


@dataclass(frozen=True, slots=True)
class _Verdict:
    """Решение по команде: прогон, чьи упавшие сценарии повторить, или причина отказа."""

    base_run_id: int | None
    reason: str = ""


class RecheckWatch:
    """Разбирает ответы на статусный комментарий PR, которые опрос пропустил (коммит тот же)."""

    def __init__(self, store: Store, api: BitbucketApi) -> None:
        """Сверка над базой ``store`` и Bitbucket ``api``."""
        self._store = store
        self._api = api

    async def check(self, prs: Sequence[PullRequest], config: Config, enqueue: Enqueue) -> None:
        """PR из опроса без нового коммита; сбой по одному PR не мешает остальным."""
        if config.daemon.dry_run or not config.daemon.max_rechecks:
            return
        for pr in prs:
            # PR по очереди: запросов почти никогда нет, а ошибку каждого PR — в журнал.
            await self._check(pr, config, enqueue)  # noqa: WPS476

    async def _check(self, pr: PullRequest, config: Config, enqueue: Enqueue) -> None:
        """Один PR: нужна ли сверка, затем — ответы на статусный комментарий."""
        summary = self._store.comments.latest_summary(pr.project, pr.repo, pr.id)
        if summary is None:
            return
        watch = self._store.watch.recheck(pr.project, pr.repo, pr.id)
        now = utc_now()
        if watch.retry_after is not None and now < watch.retry_after:
            return
        if not self._due(pr, watch, now, config):
            return
        try:
            watch = await self._scan(pr, summary.comment_id, watch, config, enqueue)
        except GigaReviewError as error:
            watch = replace(watch, retry_after=now + _RETRY)
            retry = clock_time(now + _RETRY)
            message = f"ответы на статусный комментарий: {error}; повтор после {retry}"
            self._event(pr, EventLevel.WARNING, message, None)
        self._store.watch.save_recheck(watch)

    def _due(
        self,
        pr: PullRequest,
        watch: RecheckWatchRecord,
        now: datetime,
        config: Config,
    ) -> bool:
        """Сверка нужна: счётчик комментариев изменился; иначе — по расписанию.

        Счётчик может и не измениться (удалили один комментарий, добавили ответ), поэтому PR,
        чьи проверки можно перезапустить, сверяется и раз в 30 минут; без счётчика — раз в 5.
        """
        count = pr.comment_count
        if count is not None and count != watch.comment_count:
            return True
        period = _FALLBACK if count is None else _RECHECK
        if watch.checked_at is not None and now - watch.checked_at < period:
            return False
        return self._verdict(pr, config).base_run_id is not None

    async def _scan(
        self,
        pr: PullRequest,
        comment_id: int,
        watch: RecheckWatchRecord,
        config: Config,
        enqueue: Enqueue,
    ) -> RecheckWatchRecord:
        """Новые ответы на статусный комментарий: команды — по порядку; ход сверки."""
        checked = replace(
            watch,
            comment_count=pr.comment_count,
            checked_at=utc_now(),
            retry_after=None,
        )
        try:
            summary = await self._api.get_comment(pr, comment_id)
        except BitbucketError as error:
            # Статусный комментарий удалили вручную — отвечать не на что.
            if error.status_code == HTTPStatus.NOT_FOUND:
                return checked
            raise
        replies = sorted(
            (reply for reply in summary.all_replies() if reply.id > watch.last_reply_id),
            key=lambda reply: reply.id,
        )
        slug = await self._api.current_slug()
        since = self._since(pr)
        for reply in replies:
            # Команда старше последнего прогона коммита: он её уже учёл.
            fresh = reply.created_date >= since
            if fresh and _is_command(reply) and _may_recheck(pr, reply, slug):
                # Команды — по очереди: принятая ставит прогон, следующая его увидит.
                await self._answer(pr, reply, config, enqueue)  # noqa: WPS476
        if not replies:
            return checked
        return replace(checked, last_reply_id=replies[-1].id)

    async def _answer(
        self,
        pr: PullRequest,
        reply: Comment,
        config: Config,
        enqueue: Enqueue,
    ) -> None:
        """Команда ``reply``: перезапуск и 👍 либо 👎 и причина в журнал."""
        who = reply.author.display_name or reply.author.name
        verdict = self._verdict(pr, config)
        if verdict.base_run_id is None:
            message = f"{RECHECK_COMMAND} от {who} отклонён: {verdict.reason}"
            self._event(pr, EventLevel.NOTE, message, None)
            await self._react(pr, reply, REJECTED)
            return
        base_run_id = verdict.base_run_id
        run_id = enqueue(pr, base_run_id)
        message = f"{RECHECK_COMMAND} от {who}: перезапуск упавших сценариев прогона #{base_run_id}"
        self._event(pr, EventLevel.NOTE, message, run_id)
        await self._react(pr, reply, ACCEPTED)

    def _since(self, pr: PullRequest) -> int:
        """С какого времени (мс эпохи) ответы — новые команды: после запуска прогона коммита."""
        latest = self._store.watch.latest_on_commit(pr.project, pr.repo, pr.id, pr.commit)
        return 0 if latest is None else _milliseconds(latest.created_at)

    def _verdict(self, pr: PullRequest, config: Config) -> _Verdict:
        """Можно ли перезапустить проверки текущего коммита PR и после какого прогона."""
        active = self._store.watch.active_run(pr.project, pr.repo, pr.id)
        if active is not None:
            return _Verdict(None, f"проверка уже идёт (прогон #{active.id})")
        latest = self._store.watch.latest_on_commit(pr.project, pr.repo, pr.id, pr.commit)
        if latest is None:
            return _Verdict(None, "текущий коммит ещё не проверялся")
        used = self._store.runs.count_rechecks(pr.project, pr.repo, pr.id, pr.commit)
        if used >= config.daemon.max_rechecks:
            return _Verdict(None, f"лимит перезапусков коммита исчерпан ({used})")
        if latest.status in _CRASHED or self._store.runs.has_errors(latest.id):
            return _Verdict(latest.id)
        return _Verdict(None, f"в прогоне #{latest.id} нет упавших сценариев")

    async def _react(self, pr: PullRequest, reply: Comment, emoticon: str) -> None:
        """Реакция на ответ с командой; не удалась — только предупреждение в журнал."""
        try:
            await self._api.add_reaction(pr, reply.id, emoticon)
        except BitbucketError as error:
            message = f"не удалось поставить реакцию на #{reply.id}: {error}"
            self._event(pr, EventLevel.WARNING, message, None)

    def _event(self, pr: PullRequest, level: EventLevel, message: str, run_id: int | None) -> None:
        """Событие по PR в журнал."""
        self._store.events.add(level, f"{pr.project}/{pr.repo}#{pr.id}: {message}", run_id)


def _is_command(reply: Comment) -> bool:
    """Ответ — команда перезапуска (первое слово ``/recheck``, регистр не важен)."""
    words = reply.text.split()
    return bool(words) and words[0].casefold() == RECHECK_COMMAND


def _may_recheck(pr: PullRequest, reply: Comment, slug: str) -> bool:
    """Ответ написал автор PR или ревьюер, но не пользователь демона."""
    login = reply.author.slug or reply.author.name
    if reply.author.matches(slug) or not login:
        return False
    return any(participant.user.matches(login) for participant in (pr.author, *pr.reviewers))


def _milliseconds(moment: datetime) -> int:
    """Время в миллисекундах эпохи, как ``createdDate`` в Bitbucket."""
    return int(moment.timestamp() * _MILLISECONDS)
