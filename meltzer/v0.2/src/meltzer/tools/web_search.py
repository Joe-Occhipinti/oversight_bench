"""The `web_search` tool: send a query to the configured search service and return the results.

The search service is set by `[web_search] endpoint` in the user config. meltzer
sends `POST <endpoint>` with the JSON body `{"query": ..., "max_results": N}`
and expects `{"results": [{"title": ..., "url": ..., "snippet": ...}, ...]}`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import httpx

from meltzer import __version__
from meltzer.approval.orchestrator import ApprovalOrchestrator, WebSearchRequest
from meltzer.config.loader import ConfigError
from meltzer.network.policy import NetworkPolicy
from meltzer.tools.registry import Tool, ToolError, ToolSpec, optional_int, require_string
from meltzer.utils.truncate import truncate_output

if TYPE_CHECKING:
    from meltzer.config.schema import Config

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS = 10
REQUEST_TIMEOUT = 30.0

WEB_SEARCH_SPEC = ToolSpec(
    name="web_search",
    description=(
        "Search the web. Returns the title, URL and a short text of each result. "
        "Each search needs approval before the query is sent."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "max_results": {
                "type": "integer",
                "description": f"Number of results to return, 1 to {MAX_RESULTS}. Defaults to {DEFAULT_MAX_RESULTS}.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)


class WebSearchTool(Tool):
    spec = WEB_SEARCH_SPEC

    def __init__(
        self,
        root: Path,
        orchestrator: ApprovalOrchestrator,
        endpoint: str,
        *,
        api_key_env: str | None = None,
        environ: Mapping[str, str] | None = None,
        denied_domains: tuple[str, ...] = (),
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.root = root
        self.orchestrator = orchestrator
        self.endpoint = endpoint
        self.api_key_env = api_key_env
        self.environ = os.environ if environ is None else environ
        self.denied_domains = denied_domains
        self.timeout = timeout

    def run(self, arguments: dict[str, Any]) -> str:
        query = require_string(arguments, "query").strip()
        max_results = min(optional_int(arguments, "max_results", positive=True) or DEFAULT_MAX_RESULTS, MAX_RESULTS)

        host = httpx.URL(self.endpoint).host
        try:
            denied = NetworkPolicy(denied=self.denied_domains).is_denied(host)
        except ValueError as exc:
            raise ToolError(f"search not sent: {exc}") from None
        if denied:
            raise ToolError(f"search not sent: the search service host {host} is in network.denied_domains")
        headers = {"User-Agent": f"meltzer/{__version__}"}
        if self.api_key_env:
            api_key = self.environ.get(self.api_key_env)
            if not api_key:
                raise ToolError(
                    f"search not sent: environment variable {self.api_key_env} "
                    "(web_search.api_key_env) is not set"
                )
            headers["Authorization"] = f"Bearer {api_key}"

        decision = self.orchestrator.decide_web_search(WebSearchRequest(query=query, cwd=self.root))
        if not decision.run:
            raise ToolError(f"search not sent: {decision.reason}")

        results = self._search(query, max_results, headers)
        return truncate_output(format_results(query, results[:max_results]))

    def _search(self, query: str, max_results: int, headers: dict[str, str]) -> list[dict[str, str]]:
        try:
            with httpx.Client(timeout=self.timeout, headers=headers) as client:
                response = client.post(self.endpoint, json={"query": query, "max_results": max_results})
        except httpx.TimeoutException:
            raise ToolError(
                f"web search failed: the search service did not respond within {self.timeout:g} seconds"
            ) from None
        except httpx.HTTPError as exc:
            raise ToolError(f"web search failed: {exc}") from None
        if not response.is_success:
            raise ToolError(f"web search failed: the search service returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise ToolError("web search failed: invalid response from the search service: not JSON") from None
        return parse_results(data)


def parse_results(data: Any) -> list[dict[str, str]]:
    """The results of a search service response; missing or non-string fields become empty strings."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ToolError("web search failed: invalid response from the search service: expected a 'results' list")
    results = []
    for entry in data["results"]:
        if not isinstance(entry, dict):
            raise ToolError("web search failed: invalid response from the search service: a result is not an object")
        results.append({key: _text(entry.get(key)) for key in ("title", "url", "snippet")})
    return results


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def format_results(query: str, results: list[dict[str, str]]) -> str:
    if not results:
        return f'No results for "{query}".'
    lines = [f'Results for "{query}":']
    for number, result in enumerate(results, start=1):
        lines.append("")
        lines.append(f"{number}. {result['title'] or '(no title)'}")
        if result["url"]:
            lines.append(f"   {result['url']}")
        if result["snippet"]:
            lines.append(f"   {result['snippet']}")
    return "\n".join(lines)


def web_search_tool(
    config: Config,
    root: Path,
    orchestrator: ApprovalOrchestrator,
    environ: Mapping[str, str] | None = None,
) -> WebSearchTool | None:
    """The `web_search` tool when web search is enabled and has an endpoint, otherwise None."""
    if not config.web_search_enabled or not config.web_search_endpoint:
        return None
    endpoint = config.web_search_endpoint
    try:
        url = httpx.URL(endpoint)
    except httpx.InvalidURL as exc:
        raise ConfigError(f"web_search.endpoint: invalid URL {endpoint!r}: {exc}") from None
    if url.scheme not in ("http", "https") or not url.host:
        raise ConfigError(f"web_search.endpoint: expected an http or https URL, got {endpoint!r}")
    return WebSearchTool(
        root,
        orchestrator,
        endpoint,
        api_key_env=config.web_search_api_key_env,
        environ=environ,
        denied_domains=config.network_denied_domains,
    )
