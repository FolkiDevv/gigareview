"""Вход в Web Dashboard по токену из конфигурации (``dashboard.token``).

Браузер входит на странице ``/login`` и получает cookie сессии, скрипты передают токен в
заголовке ``Authorization: Bearer``. Без токена в конфигурации вход не нужен.
"""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated, Final

from litestar import Response, get, post
from litestar.datastructures import Cookie
from litestar.di import NamedDependency
from litestar.enums import MediaType, RequestEncodingType
from litestar.params import Body, FromQuery
from litestar.response import Redirect, Template

from gigareview.web.deps import AnyRequest, redirect_with

__all__ = ["GuardDep", "LoginForm", "TokenGuard", "login", "login_page"]

_COOKIE: Final = "gigareview_session"
_SESSION_DAYS: Final = 30
_SECONDS_IN_DAY: Final = 86_400
_UNAUTHORIZED: Final = 401
_SEE_OTHER: Final = 303
_BEARER: Final = "Bearer "
#: Пути без входа: страница входа, статика и иконка.
_OPEN_PATHS: Final = ("/login", "/static/", "/favicon.ico")


class TokenGuard:
    """Проверка входа; пустой ``token`` — вход выключен."""

    def __init__(self, token: str) -> None:
        """Проверка по токену ``token``."""
        self._token = token
        # В cookie — производное от токена, а не он сам: из cookie токен не восстановить.
        self._session = hmac.new(
            token.encode(),
            b"gigareview-dashboard-session",
            hashlib.sha256,
        ).hexdigest()

    @property
    def enabled(self) -> bool:
        """Вход включён."""
        return bool(self._token)

    def check_token(self, candidate: str) -> bool:
        """``candidate`` — верный токен."""
        return self.enabled and hmac.compare_digest(candidate.encode(), self._token.encode())

    def session_cookie(self, *, secure: bool) -> Cookie:
        """Cookie сессии после входа."""
        return Cookie(
            key=_COOKIE,
            value=self._session,
            max_age=_SESSION_DAYS * _SECONDS_IN_DAY,
            httponly=True,
            secure=secure,
            samesite="lax",
        )

    def authorized(self, request: AnyRequest) -> bool:
        """Запрос с cookie сессии или с токеном в ``Authorization``."""
        if not self.enabled:
            return True
        header = request.headers.get("authorization", "")
        if header.startswith(_BEARER) and self.check_token(header.removeprefix(_BEARER)):
            return True
        cookie = request.cookies.get(_COOKIE, "")
        return hmac.compare_digest(cookie.encode(), self._session.encode())

    def before_request(self, request: AnyRequest) -> Response[str] | None:
        """Хук приложения: без входа GET-страница ведёт на ``/login``, остальное — 401."""
        path = request.url.path
        if path.startswith(_OPEN_PATHS) or self.authorized(request):
            return None
        if request.method == "GET" and not path.startswith("/api/"):
            return redirect_with("/login", target=_local_path(path))
        return Response(
            "Unauthorized",
            status_code=_UNAUTHORIZED,
            media_type=MediaType.TEXT,
            headers={"WWW-Authenticate": "Bearer"},
        )


#: Проверка входа — зависимость обработчиков (см. ``create_app``).
GuardDep = NamedDependency[TokenGuard]


@dataclass(frozen=True, slots=True)
class LoginForm:
    """Форма входа: токен и страница, на которую вернуться."""

    token: str
    target: str = "/"


@get("/login", name="login_page", sync_to_thread=False)
def login_page(target: FromQuery[str] = "/", error: FromQuery[str] = "") -> Template:
    """Страница входа."""
    return Template(
        template_name="login.html",
        context={"target": _local_path(target), "error": error},
    )


@post("/login", name="login", sync_to_thread=False)
def login(
    request: AnyRequest,
    guard: GuardDep,
    # Litestar передаёт тело запроса только в параметр с именем data.
    data: Annotated[LoginForm, Body(media_type=RequestEncodingType.URL_ENCODED)],  # noqa: WPS110
) -> Redirect:
    """Вход: верный токен — cookie сессии и возврат на страницу, неверный — снова форма."""
    target = _local_path(data.target)
    if not guard.check_token(data.token.strip()):
        return redirect_with("/login", error="Wrong token", target=target)
    return Redirect(
        path=target,
        status_code=_SEE_OTHER,
        cookies=[guard.session_cookie(secure=request.url.scheme == "https")],
    )


def _local_path(path: str) -> str:
    """Путь на этом же сервере; адрес другого хоста (``//host`` и подобные) — ``/``."""
    if not path.startswith("/") or path.startswith(("//", "/\\")):
        return "/"
    return path
