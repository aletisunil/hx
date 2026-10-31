/**
 * Web search and page extraction, over the Tavily API.
 *
 * Every model route in HX is chosen by the model id. Search deliberately is
 * not: it is one ordinary tool that behaves identically wherever the turn is
 * being served. The cost of that choice is a third credential and a bill HX
 * cannot fold into the status bar, so `CreditLedger` accumulates the spend for
 * the session and every tool result reports it.
 *
 * Tavily documents a `usage.credits` field that live responses do not carry,
 * so every figure HX prints is `publishedPrice` arithmetic and is marked `~`.
 * The documented field still wins if it ever appears.
 *
 * Both tools are registered only when a key resolves at startup: an advertised
 * tool that always fails is worse than an absent one.
 */

import { type AuthResolver, ExpiredCredential, MissingCredential } from "../auth/resolve.ts";
import { TAVILY } from "../auth/store.ts";
import { describe, request } from "../net.ts";
import { pyLen, repr, splitWhitespace, strip, toInt } from "../py.ts";
import { count } from "../text.ts";
import {
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "./base.ts";
import { capOutput } from "./output.ts";

const API_BASE = "https://api.tavily.com";
const SEARCH_URL = `${API_BASE}/search`;
const EXTRACT_URL = `${API_BASE}/extract`;
const TIMEOUT_SECONDS = 30;
const DEFAULT_MAX_RESULTS = 5;
const MAX_RESULTS_CAP = 20;
const SEARCH_DEPTHS = ["basic", "advanced", "fast", "ultra-fast"];
const TOPICS = ["general", "news"];
const TIME_RANGES = ["day", "week", "month", "year"];
const EXTRACT_DEPTHS = ["basic", "advanced"];
/** Tavily bills extraction per five URLs, so five is one credit's worth. */
const MAX_URLS_PER_FETCH = 5;
const SNIPPET_CHARS = 500;

const SEARCH_DESCRIPTION = `Search the web and return ranked results with a snippet of each page.

Use this for anything outside the repository and outside your training data:
current library versions, release notes, error messages, API changes, docs.
Prefer specific queries over broad ones. Use \`include_domains\` when you already
know the authoritative source (e.g. ["docs.python.org"]).

Snippets are extracts, not whole pages. Call WebFetch on a result URL when you
need the full text.`;

const FETCH_DESCRIPTION = `Fetch one or more web pages and return their content as markdown.

Takes up to 5 URLs at a time. Use it after WebSearch to read a promising
result in full, or directly when you already have the URL.`;

/** Tavily credits spent this session, shared by both tools: one bill. */
export class CreditLedger {
  credits = 0;
  calls = 0;
  /** Calls whose cost Tavily did not report and HX had to price itself. */
  estimates = 0;

  record(spent: number, estimated: boolean): void {
    this.credits += spent;
    this.calls++;
    this.estimates += Number(estimated);
  }

  get approximate(): boolean {
    return this.estimates > 0;
  }
}

/** What Tavily's price list says a call costs. `units` is the number of
 * five-URL blocks for extraction, and 1 for a search. Advanced doubles either. */
export const publishedPrice = (depth: string, units = 1) => units * (depth === "advanced" ? 2 : 1);

type Body = Record<string, unknown>;

/** Shared auth, transport and error translation for the Tavily endpoints. */
abstract class TavilyTool extends Tool {
  constructor(
    private readonly auth: AuthResolver,
    readonly ledger: CreditLedger = new CreditLedger(),
  ) {
    super();
  }

  /** One authenticated request, the key resolved at call time so a rotated
   * key needs no restart. Returns the body and what it cost. */
  protected async post(
    url: string,
    payload: Body,
    expectedCredits: number,
    signal: AbortSignal,
  ): Promise<[Body, number]> {
    const token = this.token();
    let status: number;
    let text: string;
    try {
      const response = await request(url, {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        timeout: TIMEOUT_SECONDS,
        signal,
      });
      status = response.status;
      text = await response.text();
    } catch (error) {
      if (signal.aborted) throw error;
      throw new ToolError(`Tavily: ${describe(error)}`);
    }
    if (status >= 400) throw new ToolError(errorMessage(status, text));
    let body: unknown;
    try {
      body = JSON.parse(text);
    } catch (error) {
      throw new ToolError(`Tavily returned a non-JSON response (${(error as Error).message}).`);
    }
    if (typeof body !== "object" || body === null || Array.isArray(body)) {
      throw new ToolError("Tavily returned an unexpected response shape.");
    }
    const reported = reportedCredits(body as Body);
    const spent = reported > 0 ? reported : expectedCredits;
    this.ledger.record(spent, reported === 0);
    return [body as Body, spent];
  }

  private token(): string {
    try {
      return String(this.auth.resolveStatic(TAVILY).token);
    } catch (error) {
      if (error instanceof MissingCredential || error instanceof ExpiredCredential)
        throw new ToolError(error.message);
      throw error;
    }
  }

  /** Recorded in the transcript, not shown to the model: `request_id` is what
   * Tavily support asks for. */
  protected meta(body: Body, spent: number): Record<string, unknown> {
    const meta: Record<string, unknown> = { credits: spent, session_credits: this.ledger.credits };
    if (typeof body.request_id === "string") meta.request_id = body.request_id;
    return meta;
  }

  /** The credit footnote appended to every result; `~` marks a priced guess. */
  protected footer(spent: number, estimated: boolean): string {
    const call = `${estimated ? "~" : ""}${spent} credit${spent === 1 ? "" : "s"}`;
    const total = `${this.ledger.approximate ? "~" : ""}${this.ledger.credits}`;
    return `[Tavily: ${call}, ${total} this session]`;
  }
}

export class WebSearchTool extends TavilyTool {
  readonly name = "WebSearch";
  readonly description = SEARCH_DESCRIPTION;

  schema() {
    return {
      type: "object",
      properties: {
        query: { type: "string" },
        max_results: {
          type: "integer",
          description: `1-${MAX_RESULTS_CAP} (default ${DEFAULT_MAX_RESULTS})`,
        },
        topic: { type: "string", enum: [...TOPICS] },
        search_depth: {
          type: "string",
          enum: [...SEARCH_DEPTHS],
          description: "advanced digs deeper and costs 2 credits instead of 1",
        },
        time_range: { type: "string", enum: [...TIME_RANGES] },
        include_domains: { type: "array", items: { type: "string" } },
        exclude_domains: { type: "array", items: { type: "string" } },
      },
      required: ["query"],
    };
  }

  /** The query itself, so `WebSearch(*)` can be denied: a query leaves the
   * machine carrying whatever context the model put in it. */
  override permissionSpecifier(params: Params): string | null {
    return typeof params.query === "string" ? params.query : null;
  }

  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const query = strip(String(params.query || ""));
    if (!query) throw new ToolError("WebSearch: query is empty");
    const depth = oneOf(params.search_depth, SEARCH_DEPTHS, "basic") || "basic";
    const payload: Body = {
      query,
      max_results: clamp(params.max_results, DEFAULT_MAX_RESULTS, MAX_RESULTS_CAP),
      search_depth: depth,
      topic: oneOf(params.topic, TOPICS, "general"),
    };
    const timeRange = oneOf(params.time_range, TIME_RANGES, null);
    if (timeRange !== null) payload.time_range = timeRange;
    for (const key of ["include_domains", "exclude_domains"]) {
      const domains = stringList(params[key]);
      if (domains.length) payload[key] = domains;
    }
    ctx.emitProgress(`Searching: ${query}\n`);
    const [body, spent] = await this.post(SEARCH_URL, payload, publishedPrice(depth), ctx.signal);
    const footer = this.footer(spent, reportedCredits(body) === 0);
    const results = ((body.results as unknown[]) || []).filter(isObject);
    if (results.length === 0)
      return toolResult(`No results for ${repr(query)}.\n\n${footer}`, { summary: "no results" });
    const rendered = results.map((r, i) => formatResult(i + 1, r)).join("\n\n");
    return toolResult(`${rendered}\n\n${footer}`, {
      summary: count(results.length, "result"),
      metadata: this.meta(body, spent),
    });
  }
}

export class WebFetchTool extends TavilyTool {
  readonly name = "WebFetch";
  readonly description = FETCH_DESCRIPTION;

  schema() {
    return {
      type: "object",
      properties: {
        urls: {
          type: "array",
          items: { type: "string" },
          description: `Up to ${MAX_URLS_PER_FETCH} absolute URLs`,
        },
        extract_depth: {
          type: "string",
          enum: [...EXTRACT_DEPTHS],
          description: "advanced retrieves more of the page and costs double",
        },
      },
      required: ["urls"],
    };
  }

  override permissionSpecifier(params: Params): string | null {
    return stringList(params.urls)[0] ?? null;
  }

  /** Every URL, because the call fetches every URL: matching on the first
   * alone made a deny rule something to get past by listing the URL second. */
  override permissionSpecifiers(params: Params): string[] {
    return stringList(params.urls);
  }

  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const urls = stringList(params.urls);
    if (urls.length === 0) throw new ToolError("WebFetch: no urls given");
    if (urls.length > MAX_URLS_PER_FETCH)
      throw new ToolError(`WebFetch: ${urls.length} urls, limit is ${MAX_URLS_PER_FETCH} per call`);
    const depth = oneOf(params.extract_depth, EXTRACT_DEPTHS, "basic") || "basic";
    ctx.emitProgress(`Fetching ${urls.length} page(s)\n`);
    // Billed per five-URL block, so a call is one block by construction.
    const [body, spent] = await this.post(
      EXTRACT_URL,
      { urls, format: "markdown", extract_depth: depth },
      publishedPrice(depth),
      ctx.signal,
    );
    const footer = this.footer(spent, reportedCredits(body) === 0);
    const pages = ((body.results as unknown[]) || []).filter(isObject);
    const failures = ((body.failed_results as unknown[]) || []).filter(isObject);
    const sections = [
      ...pages.map(formatPage),
      ...failures.map(
        (f) => `## ${f.url ?? "(unknown url)"}\n\nFailed: ${f.error ?? "unknown error"}`,
      ),
    ];
    if (sections.length === 0) throw new ToolError("Tavily returned no content for those URLs.");
    const capped = capOutput(sections.join("\n\n---\n\n"), {
      sessionId: ctx.sessionId,
      toolUseId: ctx.toolUseId,
      charCap: ctx.settings.context.toolOutputCharCap,
      lineCap: ctx.settings.context.toolOutputLineCap,
    });
    let summary = `${pages.length} page(s)`;
    if (failures.length) summary += `, ${failures.length} failed`;
    return toolResult(`${capped.text}\n\n${footer}`, {
      summary,
      spilledPath: capped.spilledPath,
      metadata: this.meta(body, spent),
    });
  }
}

const isObject = (value: unknown): value is Body =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function formatResult(index: number, result: Body): string {
  const title = strip(String(result.title || "(untitled)"));
  const url = strip(String(result.url || ""));
  let content = splitWhitespace(String(result.content || "")).join(" ");
  if (pyLen(content) > SNIPPET_CHARS)
    content = `${[...content].slice(0, SNIPPET_CHARS - 1).join("")}…`;
  return content ? `${index}. ${title}\n   ${url}\n   ${content}` : `${index}. ${title}\n   ${url}`;
}

function formatPage(page: Body): string {
  const url = String(page.url || "(unknown url)");
  const title = strip(String(page.title || ""));
  const content = strip(String(page.raw_content || page.content || ""));
  const heading = title ? `## ${title}\n${url}` : `## ${url}`;
  return `${heading}\n\n${content || "(empty)"}`;
}

/** What Tavily says the call cost, or 0 when it says nothing - which means
 * "not reported", never "free". */
function reportedCredits(body: Body): number {
  const usage = body.usage;
  if (!isObject(usage)) return 0;
  try {
    return toInt(usage.credits ?? 0);
  } catch {
    return 0;
  }
}

function clamp(value: unknown, fallback: number, cap: number): number {
  let number: number;
  try {
    number = toInt(value);
  } catch {
    return fallback;
  }
  return Math.max(1, Math.min(cap, number));
}

/** Fall back to the default rather than raising: a model that invents
 * `search_depth: "deep"` should get a search, not a schema lecture. */
function oneOf(value: unknown, allowed: string[], fallback: string | null): string | null {
  return typeof value === "string" && allowed.includes(value) ? value : fallback;
}

function stringList(value: unknown): string[] {
  if (typeof value === "string") return strip(value) ? [strip(value)] : [];
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is string => typeof item === "string" && Boolean(strip(item)))
    .map((item) => strip(item));
}

/** Tavily's failures, named. The status alone sends the model guessing. */
function errorMessage(status: number, text: string): string {
  const reason = detail(text);
  if (status === 401) {
    return "Tavily rejected the API key (401). Run `hx auth set tavily` with a key from https://app.tavily.com, or unset TAVILY_API_KEY if it is stale.";
  }
  if (status === 429) return `Tavily rate limit hit (429). ${reason || "Retry in a moment."}`;
  if (status === 432) {
    return "Tavily plan limit reached (432): the monthly credits are spent. Top up at https://app.tavily.com/billing.";
  }
  return strip(`Tavily request failed (${status}). ${reason}`);
}

function detail(text: string): string {
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return strip([...text].slice(0, 200).join(""));
  }
  if (isObject(body)) {
    for (const key of ["detail", "error", "message"]) {
      const value = body[key];
      if (typeof value === "string" && value) return value;
      if (isObject(value) && typeof value.error === "string") return value.error;
    }
  }
  return "";
}
