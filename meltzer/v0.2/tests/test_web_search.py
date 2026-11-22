from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from meltzer.approval.policy import (
    ApprovalPolicy,
)
from meltzer.config.loader import ConfigError, load_config
from meltzer.tools import web_search
from meltzer.tools.registry import ToolError
from meltzer.tools.web_search import (
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS,
    WebSearchTool,
    web_search_tool,
)

from conftest import RecordingReviewer, make_orchestrator


class SearchService:
    """Stands in for the search service: records each request and answers with the configured response."""

    url = "https://search.example.com/v1/search"

    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.body: bytes = json.dumps({"results": []}).encode()
        self.headers: dict[str, str] = {}
        self.error: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"path": request.url.path, "headers": dict(request.headers), "body": body})
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, headers=self.headers, content=self.body)

    def respond(self, results: list[dict[str, Any]]) -> None:
        self.body = json.dumps({"results": results}).encode()


@pytest.fixture
def server(monkeypatch):
    service = SearchService()
    client = httpx.Client
    monkeypatch.setattr(web_search.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(service), **kwargs))
    return service


def make_tool(root: Path, endpoint: str, reviewer: RecordingReviewer, **kwargs: Any) -> WebSearchTool:
    return WebSearchTool(root, make_orchestrator(root, reviewer), endpoint, **kwargs)


def test_search_sends_query_and_formats_results(work: Path, server: SearchService):
    server.respond(
        [
            {
                "title": "Python 3.12 release notes",
                "url": "https://docs.python.org/3.12/whatsnew/",
                "snippet": "New features.",
            },
            {"title": "PEP 695", "url": "https://peps.python.org/pep-0695/", "snippet": "Type parameter syntax."},
        ]
    )
    tool = make_tool(work, server.url, RecordingReviewer())

    output = tool.run({"query": "python 3.12 changes", "max_results": 3})

    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["path"] == "/v1/search"
    assert request["body"] == {"query": "python 3.12 changes", "max_results": 3}
    assert request["headers"]["content-type"] == "application/json"
    assert "authorization" not in request["headers"]
    assert "1. Python 3.12 release notes" in output
    assert "https://docs.python.org/3.12/whatsnew/" in output
    assert "New features." in output
    assert "2. PEP 695" in output
    assert output.index("Python 3.12 release notes") < output.index("PEP 695")


def test_result_counts(work: Path, server: SearchService):
    server.respond([{"title": f"result {i}", "url": f"https://example.com/{i}", "snippet": ""} for i in range(6)])
    tool = make_tool(work, server.url, RecordingReviewer())

    assert "result 2" not in tool.run({"query": "a", "max_results": 2})
    tool.run({"query": "b"})
    tool.run({"query": "c", "max_results": MAX_RESULTS + 50})

    assert [request["body"]["max_results"] for request in server.requests] == [2, DEFAULT_MAX_RESULTS, MAX_RESULTS]


def test_no_results(work: Path, server: SearchService):
    tool = make_tool(work, server.url, RecordingReviewer())
    assert tool.run({"query": "nothing matches this"}) == 'No results for "nothing matches this".'


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 3},
        {"query": "q", "max_results": 0},
        {"query": "q", "max_results": "5"},
    ],
)
def test_invalid_arguments(work: Path, server: SearchService, arguments: dict[str, Any]):
    reviewer = RecordingReviewer()
    with pytest.raises(ToolError):
        make_tool(work, server.url, reviewer).run(arguments)
    assert reviewer.requests == [] and server.requests == []


def test_every_search_is_reviewed(work: Path, server: SearchService):
    reviewer = RecordingReviewer()
    tool = make_tool(work, server.url, reviewer)

    tool.run({"query": "same query"})
    tool.run({"query": "same query"})

    assert len(reviewer.requests) == 2
    action = reviewer.requests[0].planned_action()
    assert action["tool"] == "web_search"
    assert action["query"] == "same query"
    assert action["cwd"] == str(work)
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    "approved, policy, message",
    [(False, ApprovalPolicy.ON_REQUEST, "denied by user"), (True, ApprovalPolicy.NEVER, "approval_policy = never")],
)
def test_a_refused_search_is_not_sent(work: Path, server: SearchService, approved, policy, message):
    tool = WebSearchTool(work, make_orchestrator(work, RecordingReviewer(approved), policy=policy), server.url)
    with pytest.raises(ToolError, match=message):
        tool.run({"query": "q"})
    assert server.requests == []


def test_search_service_on_a_denied_domain_is_refused(work: Path, server: SearchService):
    reviewer = RecordingReviewer()
    tool = make_tool(work, server.url, reviewer, denied_domains=("*.example.com",))

    with pytest.raises(ToolError, match="network.denied_domains"):
        tool.run({"query": "q"})
    assert reviewer.requests == []
    assert server.requests == []


@pytest.mark.parametrize("status", [404, 500, 302])
def test_unsuccessful_status_is_a_tool_error(work: Path, server: SearchService, status: int):
    server.status = status
    server.headers = {"Location": "http://elsewhere.example.com/"} if status == 302 else {}
    tool = make_tool(work, server.url, RecordingReviewer())

    with pytest.raises(ToolError, match=f"HTTP {status}"):
        tool.run({"query": "q"})
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"items": []}', b'{"results": {}}', b'{"results": ["text"]}'],
)
def test_invalid_response_is_a_tool_error(work: Path, server: SearchService, body: bytes):
    server.body = body
    tool = make_tool(work, server.url, RecordingReviewer())

    with pytest.raises(ToolError, match="invalid response"):
        tool.run({"query": "q"})


def test_missing_result_fields_are_left_empty(work: Path, server: SearchService):
    server.respond([{"url": "https://example.com/a"}, {"title": "only a title", "snippet": None}])
    tool = make_tool(work, server.url, RecordingReviewer())

    output = tool.run({"query": "q"})

    assert "1. (no title)" in output
    assert "https://example.com/a" in output
    assert "2. only a title" in output


@pytest.mark.parametrize(
    "error, message",
    [(httpx.ReadTimeout("slow"), "did not respond within"), (httpx.ConnectError("refused"), "web search failed")],
)
def test_timeout_and_unreachable_service_are_tool_errors(work: Path, server: SearchService, error, message):
    server.error = error
    with pytest.raises(ToolError, match=message):
        make_tool(work, server.url, RecordingReviewer(), timeout=0.5).run({"query": "q"})


ENABLED, ENDPOINT = ("web_search.enabled", True), ("web_search.endpoint", "https://search.example.com/v1/search")


@pytest.mark.parametrize(
    "overrides, built", [([], False), ([ENABLED], False), ([ENDPOINT], False), ([ENABLED, ENDPOINT], True)]
)
def test_tool_is_built_only_when_enabled_with_an_endpoint(work: Path, home: Path, overrides, built):
    config = load_config(work, home=home, overrides=overrides)
    tool = web_search_tool(config, work, make_orchestrator(work, RecordingReviewer()))
    assert isinstance(tool, WebSearchTool) == built
    assert tool is None or tool.endpoint == ENDPOINT[1]


def search_config(work: Path, home: Path, *overrides: tuple[str, Any]):
    base = [("web_search.enabled", True), ("web_search.api_key_env", "SEARCH_KEY")]
    return load_config(work, home=home, overrides=[*base, *overrides])


def test_tool_reads_api_key_and_denied_domains_from_config(work: Path, home: Path, server: SearchService):
    denied = ("network.denied_domains", ["blocked.example.com"])
    config = search_config(work, home, ("web_search.endpoint", server.url), denied)
    tool = web_search_tool(config, work, make_orchestrator(work, RecordingReviewer()), environ={"SEARCH_KEY": "k1"})

    assert tool is not None
    assert tool.denied_domains == ("blocked.example.com",)
    tool.run({"query": "q"})
    assert server.requests[0]["headers"]["authorization"] == "Bearer k1"


def test_missing_api_key_variable_is_a_tool_error(work: Path, home: Path, server: SearchService):
    config = search_config(work, home, ("web_search.endpoint", server.url))
    reviewer = RecordingReviewer()
    tool = web_search_tool(config, work, make_orchestrator(work, reviewer), environ={})

    assert tool is not None
    with pytest.raises(ToolError, match="SEARCH_KEY"):
        tool.run({"query": "q"})
    assert reviewer.requests == []
    assert server.requests == []


@pytest.mark.parametrize("endpoint", ["ftp://search.example.com/", "search.example.com/v1", "https:///v1"])
def test_invalid_endpoint_is_a_config_error(work: Path, home: Path, endpoint: str):
    config = load_config(
        work, home=home, overrides=[("web_search.enabled", True), ("web_search.endpoint", endpoint)]
    )

    with pytest.raises(ConfigError, match="web_search.endpoint"):
        web_search_tool(config, work, make_orchestrator(work, RecordingReviewer()))
