"""Web search and page extraction, over the Tavily API.

Every model route in HX is chosen by the model id. Search deliberately is not.
A provider-native search tool would have to be wired per route and is not on
offer at all on the ChatGPT-subscription route, so search is one ordinary tool
that behaves identically wherever the turn is being served.

The cost of that choice is a third credential and a bill HX cannot fold into
the status bar, which prices tokens. :class:`CreditLedger` accumulates the
spend for the session and every tool result reports it, so the number is at
least never invisible.

Tavily's docs describe a ``usage.credits`` field. It does not exist: probing
the live API returned ``request_id`` and ``response_time`` and no usage block
at all, on basic search, advanced search and extract alike. So every figure HX
prints is :func:`published_price` arithmetic and is marked ``~`` to say so.
``_reported_credits`` still looks for the documented field and would win if it
ever appears - the same order :mod:`hx.core.usage` uses for OpenRouter, where
the provider's own cost beats local arithmetic.

Both tools are registered only when a key resolves at startup. An advertised
tool that always fails is worse than an absent one - its schema sits in the
cached prefix, and the model keeps calling it and keeps being told no.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from hx import net
from hx.auth.store import TAVILY
from hx.tools.base import Tool, ToolContext, ToolError, ToolResult
from hx.tools.output import cap_output

API_BASE = "https://api.tavily.com"
SEARCH_URL = f"{API_BASE}/search"
EXTRACT_URL = f"{API_BASE}/extract"

TIMEOUT_SECONDS = 30.0

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 20
SEARCH_DEPTHS = ("basic", "advanced", "fast", "ultra-fast")
TOPICS = ("general", "news")
TIME_RANGES = ("day", "week", "month", "year")
EXTRACT_DEPTHS = ("basic", "advanced")

MAX_URLS_PER_FETCH = 5
"""Tavily bills extraction per five URLs, so five is one credit's worth."""

SNIPPET_CHARS = 500

SEARCH_DESCRIPTION = """Search the web and return ranked results with a snippet of each page.

Use this for anything outside the repository and outside your training data:
current library versions, release notes, error messages, API changes, docs.
Prefer specific queries over broad ones. Use `include_domains` when you already
know the authoritative source (e.g. ["docs.python.org"]).

Snippets are extracts, not whole pages. Call WebFetch on a result URL when you
need the full text."""

FETCH_DESCRIPTION = """Fetch one or more web pages and return their content as markdown.

Takes up to 5 URLs at a time. Use it after WebSearch to read a promising
result in full, or directly when you already have the URL."""


@dataclass(slots=True)
class CreditLedger:
    """Tavily credits spent this session.

    Shared by both tools, the way ``FileTracker`` is shared by the file tools -
    a per-tool counter would report two unrelated halves of one bill.
    """

    credits: int = 0
    calls: int = 0
    estimates: int = 0
    """Calls whose cost Tavily did not report and HX had to price itself."""

    def record(self, spent: int, *, estimated: bool) -> None:
        self.credits += spent
        self.calls += 1
        self.estimates += int(estimated)

    @property
    def approximate(self) -> bool:
        return self.estimates > 0


def published_price(*, depth: str, units: int = 1) -> int:
    """What Tavily's price list says a call costs, for when it does not say.

    ``units`` is the number of five-URL blocks for extraction, and 1 for a
    search. Advanced doubles either. Documented at
    https://docs.tavily.com/documentation/api-credits.
    """
    return units * (2 if depth == "advanced" else 1)


class _TavilyTool(Tool):
    """Shared auth, transport and error translation for the Tavily endpoints."""

    mutating = False

    def __init__(self, auth: Any, ledger: CreditLedger | None = None) -> None:
        self._auth = auth
        """:class:`~hx.auth.resolve.AuthResolver` - untyped to avoid an import cycle."""
        self.ledger = ledger if ledger is not None else CreditLedger()

    async def _post(
        self, url: str, payload: dict[str, Any], *, expected_credits: int
    ) -> tuple[dict[str, Any], int]:
        """One authenticated request, with the key resolved at call time.

        Returns the body and what it cost. ``expected_credits`` is the price
        list's answer, used only when the response reports none.

        Resolving here rather than in ``__init__`` means a key saved from
        another terminal mid-session is picked up on the next call. It cannot
        make an unregistered tool appear, but it does stop a rotated key from
        needing a restart.
        """
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with net.async_client(timeout=TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ToolError(f"Tavily: {net.describe(exc)}") from exc

        if response.status_code >= 400:
            raise ToolError(_error_message(response))

        try:
            body = response.json()
        except ValueError as exc:
            raise ToolError(f"Tavily returned a non-JSON response ({exc}).") from exc
        if not isinstance(body, dict):
            raise ToolError("Tavily returned an unexpected response shape.")

        reported = _reported_credits(body)
        spent = reported if reported > 0 else expected_credits
        self.ledger.record(spent, estimated=reported == 0)
        return body, spent

    def _token(self) -> str:
        from hx.auth.resolve import ExpiredCredential, MissingCredential

        try:
            return str(self._auth.resolve_static(TAVILY).token)
        except (MissingCredential, ExpiredCredential) as exc:
            raise ToolError(str(exc)) from exc

    def _meta(self, body: dict[str, Any], spent: int) -> dict[str, Any]:
        """Recorded in the transcript, not shown to the model.

        ``request_id`` is what Tavily support asks for, and a session file is
        where you go looking once a search has already gone wrong.
        """
        meta: dict[str, Any] = {"credits": spent, "session_credits": self.ledger.credits}
        if isinstance(request_id := body.get("request_id"), str):
            meta["request_id"] = request_id
        return meta

    def _footer(self, spent: int, *, estimated: bool) -> str:
        """The credit footnote appended to every result.

        ``~`` means HX priced the call from the published rates because the
        response carried no ``usage`` block - a guess labelled as one.
        """
        call = f"{'~' if estimated else ''}{spent} credit{'' if spent == 1 else 's'}"
        total = f"{'~' if self.ledger.approximate else ''}{self.ledger.credits}"
        return f"[Tavily: {call}, {total} this session]"


class WebSearchTool(_TavilyTool):
    name = "WebSearch"
    description = SEARCH_DESCRIPTION

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {
                    "type": "integer",
                    "description": f"1-{MAX_RESULTS_CAP} (default {DEFAULT_MAX_RESULTS})",
                },
                "topic": {"type": "string", "enum": list(TOPICS)},
                "search_depth": {
                    "type": "string",
                    "enum": list(SEARCH_DEPTHS),
                    "description": "advanced digs deeper and costs 2 credits instead of 1",
                },
                "time_range": {"type": "string", "enum": list(TIME_RANGES)},
                "include_domains": {"type": "array", "items": {"type": "string"}},
                "exclude_domains": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        }

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        """The query itself, so ``WebSearch(*)`` can be denied.

        The tool is non-mutating and therefore allowed by default, but a query
        leaves the machine carrying whatever context the model put in it. A
        deny or ask rule has to have something to match on.
        """
        query = params.get("query")
        return str(query) if isinstance(query, str) else None

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(params.get("query") or "").strip()
        if not query:
            raise ToolError("WebSearch: query is empty")

        depth = _one_of(params.get("search_depth"), SEARCH_DEPTHS, "basic") or "basic"
        payload: dict[str, Any] = {
            "query": query,
            "max_results": _clamp(params.get("max_results"), DEFAULT_MAX_RESULTS, MAX_RESULTS_CAP),
            "search_depth": depth,
            "topic": _one_of(params.get("topic"), TOPICS, "general"),
        }
        if (time_range := _one_of(params.get("time_range"), TIME_RANGES, None)) is not None:
            payload["time_range"] = time_range
        for key in ("include_domains", "exclude_domains"):
            if domains := _string_list(params.get(key)):
                payload[key] = domains

        ctx.emit_progress(f"Searching: {query}\n")
        body, spent = await self._post(
            SEARCH_URL, payload, expected_credits=published_price(depth=depth)
        )
        footer = self._footer(spent, estimated=_reported_credits(body) == 0)

        results = [r for r in body.get("results") or [] if isinstance(r, dict)]
        if not results:
            return ToolResult(
                content=f"No results for {query!r}.\n\n{footer}",
                summary="no results",
            )
        rendered = "\n\n".join(_format_result(i, r) for i, r in enumerate(results, start=1))
        return ToolResult(
            content=f"{rendered}\n\n{footer}",
            summary=f"{len(results)} results",
            metadata=self._meta(body, spent),
        )


class WebFetchTool(_TavilyTool):
    name = "WebFetch"
    description = FETCH_DESCRIPTION

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Up to {MAX_URLS_PER_FETCH} absolute URLs",
                },
                "extract_depth": {
                    "type": "string",
                    "enum": list(EXTRACT_DEPTHS),
                    "description": "advanced retrieves more of the page and costs double",
                },
            },
            "required": ["urls"],
        }

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        urls = _string_list(params.get("urls"))
        return urls[0] if urls else None

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        urls = _string_list(params.get("urls"))
        if not urls:
            raise ToolError("WebFetch: no urls given")
        if len(urls) > MAX_URLS_PER_FETCH:
            raise ToolError(f"WebFetch: {len(urls)} urls, limit is {MAX_URLS_PER_FETCH} per call")

        depth = _one_of(params.get("extract_depth"), EXTRACT_DEPTHS, "basic") or "basic"
        payload = {"urls": urls, "format": "markdown", "extract_depth": depth}
        ctx.emit_progress(f"Fetching {len(urls)} page(s)\n")
        # Billed per five-URL block, so a call is one block by construction.
        body, spent = await self._post(
            EXTRACT_URL, payload, expected_credits=published_price(depth=depth)
        )
        footer = self._footer(spent, estimated=_reported_credits(body) == 0)

        pages = [p for p in body.get("results") or [] if isinstance(p, dict)]
        failures = [f for f in body.get("failed_results") or [] if isinstance(f, dict)]

        sections = [_format_page(p) for p in pages]
        sections += [
            f"## {f.get('url', '(unknown url)')}\n\nFailed: {f.get('error', 'unknown error')}"
            for f in failures
        ]
        if not sections:
            raise ToolError("Tavily returned no content for those URLs.")

        capped = cap_output(
            "\n\n---\n\n".join(sections),
            session_id=ctx.session_id,
            tool_use_id=ctx.tool_use_id,
            char_cap=ctx.settings.context.tool_output_char_cap,
            line_cap=ctx.settings.context.tool_output_line_cap,
        )
        summary = f"{len(pages)} page(s)"
        if failures:
            summary += f", {len(failures)} failed"
        return ToolResult(
            content=f"{capped.text}\n\n{footer}",
            summary=summary,
            spilled_path=capped.spilled_path,
            metadata=self._meta(body, spent),
        )


def _format_result(index: int, result: dict[str, Any]) -> str:
    title = str(result.get("title") or "(untitled)").strip()
    url = str(result.get("url") or "").strip()
    content = " ".join(str(result.get("content") or "").split())
    if len(content) > SNIPPET_CHARS:
        content = content[: SNIPPET_CHARS - 1] + "…"
    return f"{index}. {title}\n   {url}\n   {content}" if content else f"{index}. {title}\n   {url}"


def _format_page(page: dict[str, Any]) -> str:
    url = str(page.get("url") or "(unknown url)")
    title = str(page.get("title") or "").strip()
    content = str(page.get("raw_content") or page.get("content") or "").strip()
    heading = f"## {title}\n{url}" if title else f"## {url}"
    return f"{heading}\n\n{content or '(empty)'}"


def _reported_credits(body: dict[str, Any]) -> int:
    """What Tavily says the call cost, or 0 when it says nothing.

    In practice always 0: the field is documented but has never been observed
    on a live response. 0 means "not reported", never "free" - there are no
    free Tavily calls - so the caller substitutes :func:`published_price`.
    """
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0
    try:
        return int(usage.get("credits", 0))
    except (TypeError, ValueError):
        return 0


def _clamp(value: Any, default: int, cap: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(cap, number))


def _one_of(value: Any, allowed: tuple[str, ...], default: str | None) -> str | None:
    """Fall back to the default rather than raising.

    A model that invents ``search_depth: "deep"`` should get a search, not a
    schema lecture that costs a turn.
    """
    return str(value) if isinstance(value, str) and value in allowed else default


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _error_message(response: httpx.Response) -> str:
    """Tavily's failures, named. The status alone sends the model guessing."""
    detail = _detail(response)
    status = response.status_code
    if status == 401:
        return (
            "Tavily rejected the API key (401). Run `hx auth set tavily` with a key "
            "from https://app.tavily.com, or unset TAVILY_API_KEY if it is stale."
        )
    if status == 429:
        return f"Tavily rate limit hit (429). {detail or 'Retry in a moment.'}"
    if status == 432:
        return (
            "Tavily plan limit reached (432): the monthly credits are spent. "
            "Top up at https://app.tavily.com/billing."
        )
    return f"Tavily request failed ({status}). {detail}".strip()


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200].strip()
    if isinstance(body, dict):
        for key in ("detail", "error", "message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("error"), str):
                return str(value["error"])
    return ""
