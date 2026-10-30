import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import FakeBitbucket, make_config, make_pr
from httpx import Response
from litestar import Litestar
from litestar.testing import TestClient

from gigareview.daemon import Daemon
from gigareview.pipeline import new_run
from gigareview.statuses import EventLevel, Outcome, RunStatus, Trigger
from gigareview.store import CommentKind, ScenarioRecord, Store
from gigareview.web import create_app
from gigareview.web.auth import TokenGuard

TEMPLATES = Path(__file__).parent.parent / "gigareview" / "web" / "templates"
Client = TestClient[Litestar]
TOKEN = "dashboard-token-0123456789"


@pytest.fixture
def api() -> FakeBitbucket:
    return FakeBitbucket(prs=[make_pr(1), make_pr(2, state="DECLINED")])


@pytest.fixture
def daemon(tmp_path: Path, store: Store, api: FakeBitbucket) -> Daemon:
    return Daemon(make_config(tmp_path / "home", "unused"), store, api)


@pytest.fixture
def client(daemon: Daemon, store: Store) -> Iterator[Client]:
    with TestClient(app=create_app(daemon, store, csrf_secret="test")) as test_client:
        yield test_client


def _seed(store: Store) -> int:
    run_id = store.runs.create(new_run(make_pr(1), Trigger.AUTO, fingerprint="fp", dry_run=False))
    store.runs.mark_running(run_id)
    store.runs.append_setup_log(run_id, "$ git fetch\n")
    store.runs.add_scenario(
        run_id,
        0,
        ScenarioRecord(
            name="mypy",
            title="Ошибки mypy",
            kind="simple",
            outcome=Outcome.ISSUES,
            run_exit=1,
            run_output="app.py:1: error <script>",
            duration=2.0,
            validate_exit=1,
            validate_output="validated",
            format_exit=0,
            format_output="formatted",
            tasks=("task text",),
            error="",
        ),
    )
    store.runs.finish(run_id, RunStatus.ISSUES)
    store.comments.add(run_id, 77, 0, CommentKind.TASK, "mypy")
    store.events.add(EventLevel.ERROR, "что-то сломалось", run_id)
    return run_id


def _csrf(client: Client) -> dict[str, str]:
    client.get("/")
    return {"x-csrftoken": client.cookies["csrftoken"]}


def test_overview(client: Client, store: Store) -> None:
    run_id = _seed(store)
    response = client.get("/?notice=Готово")
    assert response.status_code == 200
    assert "Готово" in response.text
    assert f"/runs/{run_id}" in response.text
    assert "что-то сломалось" in response.text
    assert 'hx-trigger="every 3s"' in response.text
    assert "_csrf_token" in response.text


def test_runs_page_and_filters(client: Client, store: Store) -> None:
    _seed(store)
    assert "PROJ/repo#1" in client.get("/runs").text
    filtered = client.get("/runs", params={"status": "passed"})
    assert "No runs yet" in filtered.text
    by_scenario = client.get(
        "/runs",
        params={"scenario": "mypy", "outcome": "issues", "repo": "PROJ/repo", "pr": "1"},
    )
    assert "PROJ/repo#1" in by_scenario.text
    assert client.get("/runs", params={"status": "bogus"}).status_code == 400


def test_run_page(client: Client, store: Store) -> None:
    run_id = _seed(store)
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    for fragment in ("Ошибки mypy", "task text", "validated", "formatted", "#77", "git fetch"):
        assert fragment in page.text
    assert "&lt;script&gt;" in page.text
    assert "hx-trigger" not in page.text
    assert client.get("/runs/999").status_code == 404


def test_other_pages(client: Client, store: Store) -> None:
    _seed(store)
    assert "PROJ/repo#1" in client.get("/prs").text
    events = client.get("/events", params={"level": "error"})
    assert "что-то сломалось" in events.text
    store.events.add(EventLevel.DEBUG, "подробность опроса")
    debug = client.get("/events", params={"level": "debug"})
    assert "подробность опроса" in debug.text
    assert "что-то сломалось" not in debug.text
    assert '<option value="debug" selected>debug</option>' in debug.text
    config = client.get("/config")
    assert "$GR_TEST_TOKEN" in config.text
    assert "PROJ/repo" in config.text


def test_config_page_hides_clone_credentials(tmp_path: Path, store: Store) -> None:
    config = make_config(tmp_path / "home", "https://user:s3cret@bb.example.com/scm/p/r.git")
    daemon = Daemon(config, store, FakeBitbucket())
    with TestClient(app=create_app(daemon, store, csrf_secret="test")) as test_client:
        page = test_client.get("/config").text
    assert "s3cret" not in page
    assert "https://***@bb.example.com/scm/p/r.git" in page


def test_api_status(client: Client, store: Store) -> None:
    _seed(store)
    status = client.get("/api/status").json()
    assert status["paused"] is False
    assert status["runs"] == {"issues": 1}


def test_static_htmx_is_served(client: Client) -> None:
    response = client.get("/static/htmx.js")
    assert response.status_code == 200
    assert "htmx" in response.text
    assert client.get("/static/app.css").status_code == 200


def test_favicon(client: Client) -> None:
    redirect = client.get("/favicon.ico", follow_redirects=False)
    assert redirect.is_redirect
    assert redirect.headers["location"] == "/static/favicon.svg"
    icon = client.get("/static/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")


@pytest.fixture
def secured(tmp_path: Path, store: Store, api: FakeBitbucket) -> Iterator[Client]:
    config = make_config(tmp_path / "home", "unused", dashboard_extra={"token": TOKEN})
    app = create_app(Daemon(config, store, api), store, csrf_secret="test")
    with TestClient(app=app) as test_client:
        yield test_client


def _login(client: Client, token: str, target: str = "/runs") -> Response:
    page = client.get(f"/login?target={target}")
    assert page.status_code == 200
    csrf = re.search(r'name="_csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    return client.post(
        "/login",
        data={"_csrf_token": csrf.group(1), "token": token, "target": target},
        follow_redirects=False,
    )


def test_open_dashboard_needs_no_login(client: Client) -> None:
    assert client.get("/", follow_redirects=False).status_code == 200


def test_secured_dashboard_redirects_to_login(secured: Client) -> None:
    response = secured.get("/runs", follow_redirects=False)
    assert response.is_redirect
    assert response.headers["location"] == "/login?target=%2Fruns"
    assert secured.get("/api/status").status_code == 401
    assert secured.get("/static/app.css").status_code == 200
    assert secured.get("/favicon.ico", follow_redirects=False).is_redirect


def test_secured_dashboard_login(secured: Client) -> None:
    wrong = _login(secured, "wrong-token")
    assert wrong.headers["location"].startswith("/login?error=Wrong+token")
    assert "gigareview_session" not in secured.cookies
    right = _login(secured, TOKEN)
    assert right.headers["location"] == "/runs"
    assert TOKEN not in right.headers["set-cookie"]
    assert "HttpOnly" in right.headers["set-cookie"]
    assert secured.get("/runs", follow_redirects=False).status_code == 200


def test_secured_login_ignores_foreign_target(secured: Client) -> None:
    assert _login(secured, TOKEN, "//evil.example").headers["location"] == "/"


def test_secured_api_accepts_bearer(secured: Client) -> None:
    response = secured.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert secured.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_session_expires_on_server() -> None:
    now = [1_000_000.0]
    guard = TokenGuard(TOKEN, clock=lambda: now[0])
    session = guard.new_session()
    assert guard.valid_session(session)
    now[0] += 30 * 86_400
    assert guard.valid_session(session)
    now[0] += 1
    assert not guard.valid_session(session)


def test_session_cookie_secure_behind_https_proxy() -> None:
    assert TokenGuard(TOKEN, https=True).session_cookie(secure=False).secure
    assert not TokenGuard(TOKEN).session_cookie(secure=False).secure
    assert TokenGuard(TOKEN).session_cookie(secure=True).secure


def test_session_rejects_forged_values() -> None:
    guard = TokenGuard(TOKEN)
    issued = guard.new_session().partition(".")[0]
    forged = TokenGuard("other-token-0123456789").new_session()
    assert not guard.valid_session(forged)
    assert not guard.valid_session(f"{issued}.bad")
    assert not guard.valid_session("")
    assert not guard.valid_session("x.y")


def test_post_requires_csrf(client: Client) -> None:
    assert client.post("/actions/poll").status_code == 403


def test_pause_resume_poll(client: Client, daemon: Daemon) -> None:
    headers = _csrf(client)
    response = client.post(
        "/actions/pause",
        headers={**headers, "referer": "http://testserver/config"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/config?notice=")
    assert daemon.status().paused
    client.post("/actions/resume", headers=headers, follow_redirects=False)
    assert not daemon.status().paused
    response = client.post(
        "/actions/poll",
        headers={**headers, "referer": "//evil.example.com/x"},
        follow_redirects=False,
    )
    assert response.headers["location"].startswith("/x?notice=")


def test_reload_without_file_shows_error(client: Client) -> None:
    response = client.post("/actions/reload", headers=_csrf(client), follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_rerun_actions(client: Client, store: Store) -> None:
    run_id = _seed(store)
    headers = _csrf(client)
    response = client.post(f"/runs/{run_id}/rerun", headers=headers, follow_redirects=False)
    match = re.match(r"/runs/(\d+)\?notice=", response.headers["location"])
    assert match is not None
    new_run_record = store.runs.get(int(match.group(1)))
    assert new_run_record is not None
    assert new_run_record.trigger is Trigger.MANUAL
    missing = client.post("/runs/999/rerun", headers=headers, follow_redirects=False)
    assert "error=" in missing.headers["location"]
    declined = client.post("/prs/PROJ/repo/2/rerun", headers=headers, follow_redirects=False)
    assert "error=" in declined.headers["location"]
    page = client.post("/prs/PROJ/repo/1/rerun", headers=headers)
    assert page.status_code == 200
    assert "Check queued" in page.text


def test_templates_load_nothing_external_but_fonts() -> None:
    external = re.compile(r'(?:src|href)="(https?:)?//([^/"]+)')
    for template in TEMPLATES.glob("*.html"):
        hosts = {match.group(2) for match in external.finditer(template.read_text())}
        assert hosts <= {"fonts.googleapis.com", "fonts.gstatic.com"}, template.name
