"""Асинхронный клиент Bitbucket Server REST API 1.0 (порт ``bb.py`` на httpx)."""

import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx
import truststore

from gigareview.bitbucket.models import Anchor, Comment, PullRequest
from gigareview.errors import BitbucketError
from gigareview.statuses import ReviewStatus

__all__ = ["BitbucketClient"]

_API = "/rest/api/1.0"
_PAGE_LIMIT = 100
_TIMEOUT = 30.0
_ERROR_BODY_CHARS = 500

type _Json = dict[str, Any]
#: Запрос с JSON-ответом (:meth:`BitbucketClient._json`).
type _Fetch = Callable[..., Awaitable[_Json]]


class BitbucketClient:
    """Клиент Bitbucket от имени владельца токена; ``transport`` подменяется в тестах."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        user: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Клиент ``base_url`` с Personal Access Token; ``user`` — slug, если он известен."""
        self._slug = user
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=_TIMEOUT,
            transport=transport,
            # Корпоративный CA обычно лежит в системном хранилище, а не в certifi.
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            # По HTTP (только localhost) токен не должен уйти через прокси из окружения.
            trust_env=urlsplit(base_url).scheme.lower() == "https",
        )

    async def __aenter__(self) -> Self:
        """Клиент на время блока ``async with``."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Закрывает соединения по выходе из блока."""
        await self.aclose()

    async def aclose(self) -> None:
        """Закрывает HTTP-клиент."""
        await self._http.aclose()

    async def current_slug(self) -> str:
        """Slug пользователя: заданный явно или из заголовка ``X-AUSERNAME``."""
        if not self._slug:
            response = await self._send("GET", f"{_API}/application-properties")
            self._slug = response.headers.get("X-AUSERNAME", "")
        if not self._slug:
            raise BitbucketError(
                "Не удалось определить пользователя Bitbucket: задайте bitbucket.user",
            )
        return self._slug

    async def list_review_prs(self) -> list[PullRequest]:
        """Открытые PR со всего сервера, где текущий пользователь — ревьюер (все страницы)."""
        pages = _pages(self._json, f"{_API}/dashboard/pull-requests", role="REVIEWER", state="OPEN")
        return [PullRequest.model_validate(raw) async for raw in pages]

    async def get_pull_request(self, project: str, repo: str, pr_id: int) -> PullRequest:
        """PR по номеру."""
        raw = await self._json("GET", _pr_path(project, repo, pr_id))
        return PullRequest.model_validate(raw)

    async def add_comment(
        self,
        pr: PullRequest,
        text: str,
        *,
        task: bool = False,
        anchor: Anchor | None = None,
    ) -> Comment:
        """Комментарий к PR; ``task`` — таска (BLOCKER), ``anchor`` — к строке файла.

        Строка вне диффа PR не принимается Bitbucket (HTTP 400/409): тогда комментарий
        публикуется без привязки, с путём и строкой в начале текста.
        """
        body: dict[str, Any] = {"text": text, "severity": "BLOCKER" if task else "NORMAL"}
        path = f"{_pr_path(pr.project, pr.repo, pr.id)}/comments"
        if anchor is None:
            return Comment.model_validate(await self._json("POST", path, json=body))
        try:
            raw = await self._json("POST", path, json={**body, "anchor": _anchor(anchor)})
        except BitbucketError as error:
            if error.status_code not in {HTTPStatus.BAD_REQUEST, HTTPStatus.CONFLICT}:
                raise
            body["text"] = f"`{_location(anchor)}`\n\n{text}"
            raw = await self._json("POST", path, json=body)
        return Comment.model_validate(raw)

    async def list_tasks(self, pr: PullRequest) -> list[Comment]:
        """Открытые таски PR одним обходом страниц ``blocker-comments``.

        Сбой на любой странице — ошибка: по неполному списку открытые таски сочлись бы
        закрытыми. Если сервер не учтёт фильтр, придут и закрытые — у них ``state``.
        """
        path = f"{_pr_path(pr.project, pr.repo, pr.id)}/blocker-comments"
        return [Comment.model_validate(raw) async for raw in _pages(self._json, path, state="OPEN")]

    async def resolve_task(self, pr: PullRequest, comment_id: int, version: int) -> Comment:
        """Помечает таску выполненной; устаревшая версия — перечитывает и повторяет."""
        return await self._put_comment(pr, comment_id, version, {"state": "RESOLVED"})

    async def update_comment(
        self,
        pr: PullRequest,
        comment_id: int,
        version: int,
        text: str,
    ) -> Comment:
        """Меняет текст комментария; устаревшая версия — перечитывает и повторяет."""
        return await self._put_comment(pr, comment_id, version, {"text": text})

    async def set_review_status(self, pr: PullRequest, status: ReviewStatus) -> None:
        """Статус ревью текущего пользователя: APPROVED, NEEDS_WORK или UNAPPROVED."""
        slug = await self.current_slug()
        await self._json(
            "PUT",
            f"{_pr_path(pr.project, pr.repo, pr.id)}/participants/{slug}",
            json={
                "user": {"name": slug},
                "approved": status is ReviewStatus.APPROVED,
                "status": status.value,
            },
        )

    async def _put_comment(
        self,
        pr: PullRequest,
        comment_id: int,
        version: int,
        changes: dict[str, Any],
    ) -> Comment:
        """Изменение комментария; при HTTP 409 (версия устарела) — с версией из Bitbucket."""
        path = _comment_path(pr, comment_id)
        try:
            raw = await self._json("PUT", path, json={"version": version, **changes})
        except BitbucketError as error:
            if error.status_code != HTTPStatus.CONFLICT:
                raise
            fresh = Comment.model_validate(await self._json("GET", path))
            raw = await self._json("PUT", path, json={"version": fresh.version, **changes})
        return Comment.model_validate(raw)

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Запрос с JSON-ответом (пустой ответ — пустой словарь)."""
        response = await self._send(method, path, **kwargs)
        if not response.content:
            return {}
        try:
            parsed: dict[str, Any] = response.json()
        except ValueError as error:
            body = response.text[:_ERROR_BODY_CHARS]
            raise BitbucketError(
                f"{method} {path}: ответ не JSON: {body}",
                status_code=response.status_code,
            ) from error
        return parsed

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Запрос; сетевая ошибка или неуспешный статус — :class:`BitbucketError`."""
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise BitbucketError(f"Ошибка сети: {error}") from error
        if response.is_error:
            code = response.status_code
            body = response.text[:_ERROR_BODY_CHARS]
            raise BitbucketError(f"{method} {path}: HTTP {code}: {body}", status_code=code)
        return response


async def _pages(
    fetch: _Fetch,
    path: str,
    **filters: str,
) -> AsyncIterator[_Json]:
    """Записи постраничного ответа ``path`` с фильтрами ``filters`` — все страницы."""
    start = 0
    while True:
        query = {**filters, "limit": str(_PAGE_LIMIT), "start": str(start)}
        page = await fetch("GET", path, params=query)
        for raw in page.get("values", []):
            yield raw
        if page.get("isLastPage", True):
            return
        start = int(page["nextPageStart"])


def _pr_path(project: str, repo: str, pr_id: int) -> str:
    """Путь API к PR."""
    return f"{_API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}"


def _comment_path(pr: PullRequest, comment_id: int) -> str:
    """Путь API к комментарию PR."""
    return f"{_pr_path(pr.project, pr.repo, pr.id)}/comments/{comment_id}"


def _anchor(anchor: Anchor) -> dict[str, Any]:
    """Тело ``anchor`` комментария: файл целиком или добавленная строка диффа."""
    body: dict[str, Any] = {"path": anchor.path, "diffType": "EFFECTIVE", "fileType": "TO"}
    if anchor.line is not None:
        body.update(line=anchor.line, lineType="ADDED")
    return body


def _location(anchor: Anchor) -> str:
    """``путь:строка`` для текста комментария."""
    return anchor.path if anchor.line is None else f"{anchor.path}:{anchor.line}"
