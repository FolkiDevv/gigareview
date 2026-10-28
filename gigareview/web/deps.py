"""Зависимости обработчиков и общие помощники страниц."""

from typing import Any, Final
from urllib.parse import urlencode, urlsplit

from litestar import Request
from litestar.datastructures import State
from litestar.di import NamedDependency
from litestar.response import Redirect

from gigareview.daemon import Daemon
from gigareview.store import Store

__all__ = ["PAGE_SIZE", "AnyRequest", "DaemonDep", "StoreDep", "back", "redirect_with"]

#: Записей на странице списков.
PAGE_SIZE = 50

#: Запрос с любым пользователем и сессией (их у Web Dashboard нет).
AnyRequest = Request[Any, Any, State]

#: Демон и база — зависимости обработчиков (см. ``create_app``).
DaemonDep = NamedDependency[Daemon]
StoreDep = NamedDependency[Store]

# После POST — переход на страницу методом GET (Post/Redirect/Get).
_SEE_OTHER: Final = 303


def back(request: AnyRequest, fallback: str = "/") -> str:
    """Путь страницы, с которой пришёл запрос (только путь — без чужого хоста)."""
    referer = urlsplit(request.headers.get("referer", ""))
    path = referer.path or fallback
    if not path.startswith("/") or path.startswith("//"):
        return fallback
    return path


def redirect_with(path: str, *, notice: str = "", error: str = "", target: str = "") -> Redirect:
    """Переход на ``path`` с сообщением для пользователя; ``target`` — куда вернуться."""
    pairs = (("notice", notice), ("error", error), ("target", target))
    query = {key: text for key, text in pairs if text}
    target = f"{path}?{urlencode(query)}" if query else path
    return Redirect(path=target, status_code=_SEE_OTHER)
