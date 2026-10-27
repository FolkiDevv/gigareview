import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from fakes import make_pr

from gigareview.bitbucket import Anchor, BitbucketClient
from gigareview.errors import BitbucketError
from gigareview.statuses import ReviewStatus

Responder = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, responder: Responder) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)

    def bodies(self) -> list[Any]:
        return [
            json.loads(request.content) if request.content else None for request in self.requests
        ]


def _run[ResultT](
    responder: Responder,
    action: Callable[[BitbucketClient], Coroutine[Any, Any, ResultT]],
    *,
    user: str = "",
) -> tuple[ResultT, Recorder]:
    recorder = Recorder(responder)

    async def scenario() -> ResultT:
        transport = httpx.MockTransport(recorder)
        async with BitbucketClient("https://bb/", "tok", user=user, transport=transport) as client:
            return await action(client)

    return asyncio.run(scenario()), recorder


def _pr_json(pr_id: int) -> dict[str, Any]:
    return make_pr(pr_id).model_dump(by_alias=True)


def test_current_slug_from_header() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-AUSERNAME": "me"}, json={})

    slug, recorder = _run(responder, lambda client: client.current_slug())
    assert slug == "me"
    assert recorder.requests[0].url.path == "/rest/api/1.0/application-properties"
    assert recorder.requests[0].headers["Authorization"] == "Bearer tok"


def test_current_slug_explicit_skips_request() -> None:
    slug, recorder = _run(
        lambda request: httpx.Response(500),
        lambda client: client.current_slug(),
        user="u",
    )
    assert slug == "u"
    assert not recorder.requests


def test_current_slug_missing_is_error() -> None:
    with pytest.raises(BitbucketError, match=r"bitbucket\.user"):
        _run(lambda request: httpx.Response(200), lambda client: client.current_slug())


def test_list_review_prs_paginates() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.params["start"] == "0":
            return httpx.Response(
                200,
                json={"values": [_pr_json(1)], "isLastPage": False, "nextPageStart": 1},
            )
        return httpx.Response(200, json={"values": [_pr_json(2)], "isLastPage": True})

    prs, recorder = _run(responder, lambda client: client.list_review_prs())
    assert [pr.id for pr in prs] == [1, 2]
    first, second = recorder.requests
    assert first.url.params["role"] == "REVIEWER"
    assert second.url.params["start"] == "1"


def test_get_pull_request_and_model_helpers() -> None:
    pr, recorder = _run(
        lambda request: httpx.Response(200, json=_pr_json(5)),
        lambda client: client.get_pull_request("PROJ", "repo", 5),
    )
    assert recorder.requests[0].url.path == "/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/5"
    assert pr.url == "https://bb.example.com/pr/5"
    assert pr.author_name == "Автор"
    assert pr.reviewer_status("me") == "UNAPPROVED"
    assert pr.reviewer_status("nobody") is None
    assert not make_pr().model_copy(update={"links": {}}).url


def test_add_comment_task() -> None:
    comment, recorder = _run(
        lambda request: httpx.Response(201, json={"id": 9, "version": 0, "severity": "BLOCKER"}),
        lambda client: client.add_comment(make_pr(), "text", task=True),
    )
    assert comment.id == 9
    assert recorder.bodies() == [{"text": "text", "severity": "BLOCKER"}]
    request = recorder.requests[0]
    assert request.url.path.endswith("/pull-requests/1/comments")


def test_add_comment_with_anchor() -> None:
    _, recorder = _run(
        lambda request: httpx.Response(201, json={"id": 1}),
        lambda client: client.add_comment(make_pr(), "t", anchor=Anchor("a.py", 4)),
    )
    assert recorder.bodies()[0]["anchor"] == {
        "path": "a.py",
        "line": 4,
        "lineType": "ADDED",
        "fileType": "TO",
        "diffType": "EFFECTIVE",
    }


def test_add_comment_anchor_fallback() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if "anchor" in json.loads(request.content):
            return httpx.Response(400, text="line not in diff")
        return httpx.Response(201, json={"id": 2})

    comment, recorder = _run(
        responder,
        lambda client: client.add_comment(make_pr(), "t", task=True, anchor=Anchor("a.py")),
    )
    assert comment.id == 2
    assert recorder.bodies()[1] == {"text": "`a.py`\n\nt", "severity": "BLOCKER"}


def test_add_comment_anchor_other_error_propagates() -> None:
    with pytest.raises(BitbucketError) as caught:
        _run(
            lambda request: httpx.Response(403, text="forbidden"),
            lambda client: client.add_comment(make_pr(), "t", anchor=Anchor("a.py", 1)),
        )
    assert caught.value.status_code == 403


def test_resolve_task_retries_on_conflict() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": 5, "version": 7})
        if json.loads(request.content)["version"] == 1:
            return httpx.Response(409, text="stale")
        return httpx.Response(200, json={"id": 5, "version": 8, "state": "RESOLVED"})

    comment, recorder = _run(responder, lambda client: client.resolve_task(make_pr(), 5, 1))
    assert comment.state == "RESOLVED"
    assert [request.method for request in recorder.requests] == ["PUT", "GET", "PUT"]
    assert recorder.bodies()[2] == {"version": 7, "state": "RESOLVED"}


def test_resolve_task_other_error_propagates() -> None:
    with pytest.raises(BitbucketError) as caught:
        _run(
            lambda request: httpx.Response(404),
            lambda client: client.resolve_task(make_pr(), 5, 1),
        )
    assert caught.value.status_code == 404


def test_set_review_status() -> None:
    _, recorder = _run(
        lambda request: httpx.Response(200, json={}),
        lambda client: client.set_review_status(make_pr(), ReviewStatus.APPROVED),
        user="me",
    )
    request = recorder.requests[0]
    assert request.method == "PUT"
    assert request.url.path.endswith("/pull-requests/1/participants/me")
    assert recorder.bodies() == [{"user": {"name": "me"}, "approved": True, "status": "APPROVED"}]


def test_empty_response_is_empty_dict() -> None:
    _, recorder = _run(
        lambda request: httpx.Response(204),
        lambda client: client.set_review_status(make_pr(), ReviewStatus.NEEDS_WORK),
        user="me",
    )
    assert recorder.bodies()[0]["approved"] is False


def test_http_error_carries_status() -> None:
    with pytest.raises(BitbucketError, match="HTTP 500: boom") as caught:
        _run(
            lambda request: httpx.Response(500, text="boom"),
            lambda client: client.list_review_prs(),
        )
    assert caught.value.status_code == 500


def test_invalid_json_is_bitbucket_error() -> None:
    with pytest.raises(BitbucketError, match="не JSON") as caught:
        _run(
            lambda request: httpx.Response(200, text="<html>"),
            lambda client: client.list_review_prs(),
        )
    assert caught.value.status_code == 200


def _client_options(monkeypatch: pytest.MonkeyPatch, url: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    real = httpx.AsyncClient

    def spy(**options: Any) -> httpx.AsyncClient:
        captured.update(options)
        return real(**options)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    client = BitbucketClient(url, "tok")
    asyncio.run(client.aclose())
    return captured


def test_https_uses_environment_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _client_options(monkeypatch, "https://bb.example.com")["trust_env"]


def test_uppercase_https_uses_environment_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _client_options(monkeypatch, "HTTPS://bb.example.com")["trust_env"]


def test_local_http_ignores_environment_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not _client_options(monkeypatch, "http://127.0.0.1:7990")["trust_env"]


def test_network_error() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(BitbucketError, match="сети") as caught:
        _run(responder, lambda client: client.list_review_prs())
    assert caught.value.status_code is None
