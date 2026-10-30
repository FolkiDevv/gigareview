"""Вход в Web Dashboard по токену из конфигурации (``dashboard.token``).

Браузер входит на странице ``/login`` и получает cookie сессии, скрипты передают токен в
заголовке ``Authorization: Bearer``. Без токена в конфигурации вход не нужен.
"""

import hashlib
import hmac
import time
from collections.abc import Callable
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
_SESSION_SECONDS: Final = _SESSION_DAYS * _SECONDS_IN_DAY
_UNAUTHORIZED: Final = 401
_SEE_OTHER: Final = 303
_BEARER: Final = "Bearer "
#: Пути без входа: страница входа, статика и иконка.
_OPEN_PATHS: Final = ("/login", "/static/", "/favicon.ico")


class TokenGuard:
    """Проверка входа; пустой ``token`` — вход выключен."""

    def __init__(
        self,
        token: str,
        *,
        https: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Проверка по токену ``token``.

        ``https`` — дашборд открывают по HTTPS (``public_url``): cookie всегда с ``Secure``, даже
        если прокси не сообщил схему. ``clock`` — текущее время (подменяется в тестах).
        """
        self._token = token
        self._https = https
        self._clock = clock

    @property
    def enabled(self) -> bool:
        """Вход включён."""
        return bool(self._token)

    def check_token(self, candidate: str) -> bool:
        """``candidate`` — верный токен."""
        return self.enabled and hmac.compare_digest(candidate.encode(), self._token.encode())

    def new_session(self) -> str:
        """Значение cookie сессии: время входа и его подпись токеном (сам токен не виден)."""
        issued = str(int(self._clock()))
        return f"{issued}.{self._sign(issued)}"

    def valid_session(self, session: str) -> bool:
        """Подпись сессии верна, и ей не больше 30 дней (срок проверяет сервер, а не браузер)."""
        issued, _, signature = session.partition(".")
        if not issued.isdigit() or not hmac.compare_digest(signature, self._sign(issued)):
            return False
        return 0 <= self._clock() - int(issued) <= _SESSION_SECONDS

    def session_cookie(self, *, secure: bool) -> Cookie:
        """Cookie сессии после входа."""
        return Cookie(
            key=_COOKIE,
            value=self.new_session(),
            max_age=_SESSION_SECONDS,
            httponly=True,
            secure=secure or self._https,
            samesite="lax",
        )

    def authorized(self, request: AnyRequest) -> bool:
        """Запрос с cookie сессии или с токеном в ``Authorization``."""
        if not self.enabled:
            return True
        header = request.headers.get("authorization", "")
        if header.startswith(_BEARER) and self.check_token(header.removeprefix(_BEARER)):
            return True
        return self.valid_session(request.cookies.get(_COOKIE, ""))

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

    def _sign(self, issued: str) -> str:
        """Подпись времени входа токеном."""
        message = f"gigareview-dashboard-session:{issued}".encode()
        return hmac.new(self._token.encode(), message, hashlib.sha256).hexdigest()


#: Проверка входа — зависимость обработчиков (см. ``create_app``).
GuardDep = NamedDependency[TokenGuard]


@dataclass(frozen=True, slots=True)
class LoginForm:
    """Форма входа: токен и страница, на которую вернуться.

    ``username`` — постоянное скрытое поле для менеджеров паролей; сервер его не проверяет.
    """

    token: str
    target: str = "/"
    username: str = ""


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
    if not guard.check_token(data.token):
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
