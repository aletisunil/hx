/**
 * Permission rule engine.
 *
 * Rules are `Tool(specifier)` strings - `Bash(git commit:*)`, `Edit(src/**)`,
 * `Read(~/.ssh/**)`. Evaluation order is deny, then ask, then allow: an explicit
 * deny can never be overridden by a broader allow, including in bypass mode.
 */

import {
  ConfigError,
  type PermissionMode,
  readSettingsFile,
  writeSettingsFile,
} from "../config.ts";
import { expandUser, join, resolve } from "../pathlib.ts";
import { projectLocalSettingsFile, projectSettingsFile, userSettingsFile } from "../paths.ts";
import { purePath, repr, strip } from "../py.ts";
import { type CommandSegment, isReadOnly, matchSpecifier, parse } from "./parser.ts";
import { credentialPathOf } from "./sandbox.ts";

export type Decision = "allow" | "ask" | "deny";

/** `always` is persisted to `~/.hx/projects/<project>/settings.local.json`. */
export type GrantScope = "once" | "session" | "always";

/** Tools whose specifiers are file paths. */
const FILE_TOOLS = new Set(["Read", "Write", "Edit", "Grep", "Glob", "Symbols"]);

/** Folders inside a project whose files run code or grant permissions: git
 * hooks and config, HX's own settings, MCP servers, skills and agents. */
const PROTECTED_DIRS = new Set([".git", ".hx"]);

/** Fallback when the caller does not supply the registry's own metadata. */
export const DEFAULT_MUTATING_TOOLS = new Set(["Bash", "Write", "Edit", "Task"]);

export interface Rule {
  readonly tool: string;
  readonly specifier: string | null;
  readonly decision: Decision;
  /** Where the rule came from, so `/permissions` can show it. */
  readonly source: string;
}

export interface PermissionRequest {
  readonly toolName: string;
  readonly specifier: string | null;
  readonly params: Record<string, unknown>;
  readonly mutating: boolean;
  readonly description: string;
  /**
   * Every specifier this one call covers, when it covers more than one. A tool
   * that acts on a list acts on all of it, so a rule has to see all of it:
   * deny and ask trigger on any element, allow requires every one covered.
   */
  readonly specifiers?: readonly string[];
  /** Rendered diff or command text shown in the approval prompt. */
  readonly detail?: string;
  /** What `detail` is: `text`, `diff` or `command`, stated by whoever built the
   * request rather than sniffed from the string. */
  readonly detailKind?: "text" | "diff" | "command";
  /** The file an edit really changes, when the path it names is a symlink. */
  readonly linkTarget?: string | null;
  /** Which subagent asked, when it was not the main conversation. */
  readonly origin?: string | null;
  /** The call waiting on this, so the UI can show that call as waiting. */
  readonly toolUseId?: string | null;
}

/** Everything a rule has to be matched against for this one call. */
export function allSpecifiers(request: PermissionRequest): readonly string[] {
  if (request.specifiers?.length) return request.specifiers;
  return request.specifier ? [request.specifier] : [];
}

export interface PermissionOutcome {
  readonly decision: Decision;
  readonly reason: string;
  readonly matchedRule: Rule | null;
}

export interface PermissionAnswer {
  readonly allowed: boolean;
  readonly scope: GrantScope;
}

/** Supplied by the frontend. Absent means nothing can answer an ASK. */
export type Asker = (request: PermissionRequest) => Promise<PermissionAnswer>;

const outcome = (
  decision: Decision,
  reason: string,
  matchedRule: Rule | null = null,
): PermissionOutcome => ({
  decision,
  reason,
  matchedRule,
});

export class PermissionEngine {
  /** Tools acceptEdits never prompts for. */
  static readonly EDIT_TOOLS = new Set(["Write", "Edit"]);
  rules: Rule[];
  private sessionGrants = new Set<string>();

  constructor(
    public mode: PermissionMode,
    rules: Rule[],
    public cwd: string,
    public asker: Asker | null = null,
  ) {
    this.rules = [...rules];
  }

  private grantKey(request: PermissionRequest): string {
    return `${request.toolName}\u0000${request.specifier ?? "\u0001"}`;
  }

  /**
   * Pure decision - no prompting, no I/O. Ordering: deny, ask, allow, then the
   * mode default. For Bash the specifier is decomposed and every segment must
   * pass; an unparseable command degrades to ASK.
   */
  evaluate(request: PermissionRequest): PermissionOutcome {
    const denial = this.match(request, "deny");
    if (denial) return outcome("deny", "matched a deny rule", denial);

    // Before the decomposition check: an exact grant is the only thing that can
    // cover a command that cannot be decomposed, and it is safe precisely
    // because the approved string is byte-identical to what runs.
    if (this.sessionGrants.has(this.grantKey(request)))
      return outcome("allow", "granted for this session");

    // The shell's sandbox denies these outright. A file tool runs inside HX,
    // outside the sandbox, so it asks - in every mode - unless an allow rule
    // names the file.
    const credential = this.credentialTouched(request);
    if (credential !== null) {
      const named = this.matchAllow(request, true);
      if (named) return outcome("allow", "matched an allow rule", named);
      return outcome("ask", `${credential} holds credentials`);
    }

    if (request.toolName === "Bash" && request.specifier) {
      const parsed = parse(request.specifier);
      if (parsed.unparseable) {
        // A rule holding the whole command verbatim grants exactly what it
        // names, so it covers what cannot be decomposed. Ask still wins.
        const exact = this.match(request, "ask") ? null : this.exactAllow(request);
        if (exact) return outcome("allow", "matched an allow rule", exact);
        // Bypass runs whatever no rule stops, so only a deny or ask rule for
        // the shell can be hiding behind what cannot be decomposed.
        if (this.mode === "bypass" && !this.restricted(request))
          return outcome("allow", "bypass mode");
        return outcome("ask", "command could not be decomposed; approving it blind is not safe");
      }
    }

    const askRule = this.match(request, "ask");
    if (askRule) return outcome("ask", "matched an ask rule", askRule);
    const allowRule = this.matchAllow(request);
    if (allowRule) return outcome("allow", "matched an allow rule", allowRule);
    return this.modeDefault(request);
  }

  private modeDefault(request: PermissionRequest): PermissionOutcome {
    if (this.mode === "bypass") return outcome("allow", "bypass mode");
    if (this.mode === "plan") {
      if (request.mutating) return outcome("deny", "plan mode is read-only; no writes or commands");
      return outcome("allow", "read-only tool in plan mode");
    }
    if (!request.mutating) return outcome("allow", "read-only tool");
    if (
      this.mode === "acceptEdits" &&
      PermissionEngine.EDIT_TOOLS.has(request.toolName) &&
      this.editsProject(request)
    ) {
      return outcome("allow", "acceptEdits mode");
    }
    if (request.toolName === "Bash" && request.specifier) {
      const parsed = parse(request.specifier);
      if (parsed.segments.length && parsed.segments.every(isReadOnly)) {
        if (parsed.hasRedirectOut) return outcome("ask", "command redirects output to a file");
        return outcome("allow", "read-only command");
      }
    }
    return outcome("ask", "mutating tool");
  }

  /** Where `specifier` really is: absolute, with `..` and symlinks resolved. */
  private target(specifier: string): string {
    return realPath(purePath(expandUser(specifier)), resolve(this.cwd));
  }

  /** The credential file or folder a file tool's call reaches, as written in
   * `CREDENTIAL_PATHS`, or `null`. */
  private credentialTouched(request: PermissionRequest): string | null {
    if (!FILE_TOOLS.has(request.toolName)) return null;
    for (const specifier of allSpecifiers(request)) {
      const credential = credentialPathOf(this.target(specifier));
      if (credential !== null) return credential;
    }
    return null;
  }

  /**
   * Whether every file an edit changes is the project's own, which is all
   * acceptEdits covers: inside the working directory once symlinks are
   * resolved, and not in a folder whose files run code or grant permissions.
   */
  private editsProject(request: PermissionRequest): boolean {
    const root = resolve(this.cwd);
    const prefix = root === "/" ? "/" : `${root}/`;
    const targets = allSpecifiers(request).map((specifier) => this.target(specifier));
    return (
      targets.length > 0 &&
      targets.every(
        (target) =>
          target.startsWith(prefix) &&
          !target
            .slice(prefix.length)
            .split("/")
            .some((part) => PROTECTED_DIRS.has(part)),
      )
    );
  }

  private applies(rule: Rule, request: PermissionRequest): boolean {
    return rule.tool === request.toolName || rule.tool === "*";
  }

  /** Whether any deny or ask rule could match this tool at all. */
  private restricted(request: PermissionRequest): boolean {
    return this.rules.some((rule) => rule.decision !== "allow" && this.applies(rule, request));
  }

  private match(request: PermissionRequest, decision: Decision): Rule | null {
    for (const rule of this.rules) {
      if (rule.decision !== decision || !this.applies(rule, request)) continue;
      if (rule.specifier === null || this.specifierMatches(request, rule.specifier)) return rule;
    }
    return null;
  }

  /**
   * Allow requires *every* segment to be covered, each as it is written.
   * Any-segment matching is right for deny (`safe && danger` must be caught)
   * but catastrophic for allow: `allow: Bash(git status:*)` would green-light
   * `git status && rm -rf /`.
   */
  private matchAllow(request: PermissionRequest, named = false): Rule | null {
    // `named`: only a rule that names what it allows, not a bare `Read`.
    const candidates = this.rules.filter(
      (rule) =>
        rule.decision === "allow" &&
        this.applies(rule, request) &&
        (!named || rule.specifier !== null),
    );
    if (candidates.length === 0) return null;

    if (request.toolName !== "Bash" || !request.specifier) {
      // Every specifier needs its own cover: a call that fetches three URLs is
      // allowed only if all three are.
      const covers = allSpecifiers(request);
      if (covers.length > 1) return this.matchAllowEach(candidates, covers);
      for (const rule of candidates) {
        if (rule.specifier === null || this.specifierMatches(request, rule.specifier)) return rule;
      }
      return null;
    }

    // A rule holding the whole command verbatim: exact, so it grants no more
    // than the string it names.
    const exact = this.exactAllow(request);
    if (exact) return exact;

    const parsed = parse(request.specifier);
    if (parsed.segments.length === 0 || parsed.unparseable) return null;
    let first: Rule | null = null;
    for (const segment of parsed.segments) {
      const covering = candidates.find(
        (rule) => rule.specifier === null || matchSpecifier(segment, rule.specifier, true),
      );
      if (covering === undefined) return null;
      first ??= covering;
    }
    return first;
  }

  /** An allow rule whose specifier is this request's, character for character, stripped. */
  private exactAllow(request: PermissionRequest): Rule | null {
    const specifier = strip(request.specifier ?? "");
    return (
      this.rules.find(
        (rule) =>
          rule.decision === "allow" &&
          this.applies(rule, request) &&
          rule.specifier !== null &&
          rule.specifier === specifier,
      ) ?? null
    );
  }

  /** Every specifier in `covers` must find an allow rule of its own. */
  private matchAllowEach(candidates: Rule[], covers: readonly string[]): Rule | null {
    let first: Rule | null = null;
    for (const specifier of covers) {
      const covering = candidates.find(
        (rule) => rule.specifier === null || this.oneSpecifierMatches(specifier, rule.specifier),
      );
      if (covering === undefined) return null;
      first ??= covering;
    }
    return first;
  }

  /** Deny and ask: one specifier matching is enough to trigger. */
  private specifierMatches(request: PermissionRequest, pattern: string): boolean {
    if (request.toolName === "Bash") {
      if (!request.specifier) return false;
      return parse(request.specifier).segments.some((segment) => matchSpecifier(segment, pattern));
    }
    return allSpecifiers(request).some((specifier) => this.oneSpecifierMatches(specifier, pattern));
  }

  /**
   * Match a single specifier, under the reading its shape calls for: a URL on
   * either side is compared as literal globbed text, anything else as a path.
   * Deliberately one reading or the other, never both: `Edit(src/**)` read as
   * text matches `src/../../etc/passwd`, the traversal path matching stops.
   */
  private oneSpecifierMatches(specifier: string, pattern: string): boolean {
    if (isUrl(specifier) || isUrl(pattern)) return globRegex(pattern).test(specifier);
    return matchPath(specifier, pattern, this.cwd);
  }

  /**
   * Evaluate and, on ASK, prompt through `asker`. `onAsk` fires only when the
   * call actually stops for approval. Returns `[allowed, reason]`; the reason
   * is handed to the model on a denial so it can adapt.
   */
  async request(request: PermissionRequest, onAsk?: () => void): Promise<[boolean, string]> {
    const result = this.evaluate(request);
    if (result.decision === "allow") return [true, result.reason];
    if (result.decision === "deny") return [false, result.reason];
    if (this.asker === null) {
      // Nothing can answer, so the only safe reading of ASK is "no".
      return [
        false,
        "this tool call needs approval, but this session has no way to ask (non-interactive). Run hx interactively, or add a permission rule.",
      ];
    }
    onAsk?.();
    const answer = await this.asker(request);
    if (answer.allowed) {
      this.grant(request, answer.scope);
      return [true, "approved by the user"];
    }
    return [false, "the user declined"];
  }

  /**
   * Record an approval. `session` keeps the rules in memory; `always` also
   * writes them to the project's local settings. Both record the exact request
   * as well, so a command that could not be decomposed is still covered.
   */
  grant(request: PermissionRequest, scope: GrantScope): void {
    if (scope === "once") return;
    this.sessionGrants.add(this.grantKey(request));
    const source = scope === "session" ? "session grant" : "local settings";
    for (const ruleText of persistableRules(request.toolName, request.specifier)) {
      this.rules.push(parseRule(ruleText, source, "allow"));
      if (scope === "always") persistAllowRule(ruleText, this.cwd);
    }
  }

  setMode(mode: PermissionMode): void {
    this.mode = mode;
  }

  /** Tools exposed in the current mode - plan mode hides mutating tools
   * entirely rather than letting the model call them and be refused. */
  allowedTools(allTools: string[], mutating?: Set<string>): Set<string> {
    const mutatingSet = mutating ?? DEFAULT_MUTATING_TOOLS;
    if (this.mode !== "plan") return new Set(allTools);
    return new Set(allTools.filter((name) => !mutatingSet.has(name)));
  }
}

/**
 * Generalise one approval into the rule texts worth keeping. A Bash approval
 * becomes one prefix rule per executed segment, never the exact argument
 * vector. Widening is only safe where there is a subcommand to anchor it to; a
 * command that cannot be decomposed, and one whose segments carry nothing but
 * flags, keep their exact text.
 */
export function persistableRules(toolName: string, specifier: string | null): string[] {
  if (!specifier) return [toolName];
  if (toolName !== "Bash") return [`${toolName}(${specifier})`];
  const parsed = parse(specifier);
  if (parsed.unparseable || parsed.segments.length === 0) return [`Bash(${specifier})`];
  const rules: string[] = [];
  for (const segment of parsed.segments) {
    const prefix = rulePrefix(segment);
    if (prefix === null) return [`Bash(${specifier})`];
    const text = `Bash(${prefix}:*)`;
    if (!rules.includes(text)) rules.push(text);
  }
  return rules;
}

/** `git commit -m x` -> `git commit`; `rm -rf build` -> `null`. Approving
 * `rm -rf build` must not write `Bash(rm:*)` and quietly cover every `rm`. */
function rulePrefix(segment: CommandSegment): string | null {
  // `FOO=1 make` is not `make`: a rule for it would have to carry the assignment.
  if (segment.assignments.length || !segment.executable) return null;
  const first = segment.args[0];
  if (first !== undefined && !first.startsWith("-")) return `${segment.executable} ${first}`;
  return null;
}

const RULE = /^(?<tool>[A-Za-z_][\p{L}\p{N}_-]*|\*)(?:\((?<spec>.*)\))?$/su;

/** Parse `Tool(specifier)` or bare `Tool`. Throws `InvalidRule` on malformed
 * syntax - a rule that silently fails to parse protects nothing. */
export function parseRule(text: string, source: string, decision: Decision): Rule {
  const match = RULE.exec(strip(text));
  if (match === null) throw new InvalidRule(`malformed permission rule: ${repr(text)}`);
  const specifier = match.groups?.spec;
  return {
    tool: match.groups?.tool as string,
    specifier: specifier ? strip(specifier) : null,
    decision,
    source,
  };
}

/**
 * Glob-match a path specifier, with `**` spanning directory separators. Both
 * sides are resolved before they are compared, because a rule names a *file*:
 * `deny: Read(~/.ssh/**)` has to stop `../../.ssh/id_rsa` and a planted symlink.
 * The pattern is resolved only as far as its literal head, so `Edit(/tmp/**)`
 * still covers a file under `/private/tmp` on macOS.
 */
export function matchPath(candidate: string, pattern: string, cwd: string): boolean {
  const root = resolve(cwd);
  const absolutePath = realPath(purePath(expandUser(candidate)), root);
  const expanded = purePath(expandUser(pattern));
  const target = expanded.startsWith("/") ? expanded : purePath(join(purePath(cwd), expanded));
  if (globRegex(resolvedPattern(target)).test(absolutePath)) return true;
  // Also match against the cwd-relative form, so `Edit(src/**)` works.
  const prefix = root === "/" ? "/" : `${root}/`;
  if (absolutePath !== root && !absolutePath.startsWith(prefix)) return false;
  const relative = absolutePath === root ? "." : absolutePath.slice(prefix.length);
  return globRegex(pattern).test(relative);
}

const URL_SCHEME = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//;

/** Whether a specifier or pattern names a URL rather than a path. */
const isUrl = (text: string) => URL_SCHEME.test(text);

/** Absolute, with `..` and symlinks taken out. */
function realPath(path: string, base?: string): string {
  return resolve(path.startsWith("/") ? path : join(base ?? process.cwd(), path));
}

const GLOB_CHARS = "*?[";

/** Resolve the literal head of a glob, leaving the glob part untouched. */
function resolvedPattern(target: string): string {
  const cuts = [...GLOB_CHARS]
    .filter((char) => target.includes(char))
    .map((char) => target.indexOf(char));
  if (cuts.length === 0) return realPath(target);
  const cut = Math.min(...cuts);
  const before = target.slice(0, cut);
  const slash = before.lastIndexOf("/");
  const head = slash < 0 ? "" : before.slice(0, slash);
  const tail = slash < 0 ? before : before.slice(slash + 1);
  if (!head) return target;
  return `${realPath(head)}/${tail}${target.slice(cut)}`;
}

/**
 * Translate a glob to a regex. `**` crosses directory separators and `*` does
 * not, and `**\/` also matches *zero* directories - so `**\/.ssh/**` protects
 * `.ssh/id_rsa` sitting directly in the project, not only nested copies of it.
 */
export function globRegex(pattern: string): RegExp {
  let out = "";
  let index = 0;
  while (index < pattern.length) {
    if (pattern.startsWith("**/", index)) {
      out += "(?:[\\s\\S]*/)?";
      index += 3;
    } else if (pattern.startsWith("**", index)) {
      out += "[\\s\\S]*";
      index += 2;
    } else if (pattern[index] === "*") {
      out += "[^/]*";
      index++;
    } else if (pattern[index] === "?") {
      out += "[^/]";
      index++;
    } else if (pattern[index] === "[") {
      const close = pattern.indexOf("]", index + 1);
      if (close === -1) {
        out += "\\[";
        index++;
      } else {
        out += pattern.slice(index, close + 1);
        index = close + 1;
      }
    } else {
      out += (pattern[index] as string).replace(/[.*+?^${}()|[\]\\/-]/g, "\\$&");
      index++;
    }
  }
  return new RegExp(`^(?:${out})$`);
}

/**
 * Collect rules from every settings layer, narrowest last. Evaluation is
 * deny-then-ask-then-allow across the lot, so a local allow can never quietly
 * override a project deny.
 */
export function loadRules(cwd: string): Rule[] {
  const rules: Rule[] = [];
  for (const [path, source] of [
    [userSettingsFile(), "user settings"],
    [projectSettingsFile(cwd), "project settings"],
    [projectLocalSettingsFile(cwd), "local settings"],
  ] as const) {
    const permissions = (readSettingsFile(path).permissions as Record<string, unknown>) || {};
    for (const [key, decision] of [
      ["deny", "deny"],
      ["ask", "ask"],
      ["allow", "allow"],
    ] as const) {
      for (const text of (permissions[key] as unknown[]) || [])
        rules.push(parseRule(String(text), source, decision));
    }
  }
  return rules;
}

/**
 * Widen allow rules that only ever match one exact command string: until
 * grants were generalised, "always allow" on a compound command stored the
 * whole command. Only the two files HX owns are rewritten; the project's shared
 * `settings.json` is reported on, never edited. Returns one line per rule
 * rewritten, naming the rule before and after.
 */
export function migrateLegacyRules(cwd: string): string[] {
  const shared = projectSettingsFile(cwd);
  const notices: string[] = [];
  for (const path of [userSettingsFile(), shared, projectLocalSettingsFile(cwd)]) {
    let data: Record<string, unknown>;
    try {
      data = readSettingsFile(path);
    } catch (error) {
      if (error instanceof ConfigError) continue; // reported elsewhere, not repaired here
      throw error;
    }
    const permissions = data.permissions;
    if (typeof permissions !== "object" || permissions === null || Array.isArray(permissions))
      continue;
    const allow = (permissions as Record<string, unknown>).allow;
    if (!Array.isArray(allow)) continue;

    const widened: string[] = [];
    const changes: string[] = [];
    for (const entry of allow) {
      let replacements = widenRule(String(entry));
      if (replacements === null) replacements = [String(entry)];
      else changes.push(`${entry} -> ${replacements.join(", ")}`);
      for (const text of replacements) if (!widened.includes(text)) widened.push(text);
    }
    if (changes.length === 0) continue;
    const listed = changes.map((change) => `  ${change}`).join("\n");
    if (path === shared) {
      notices.push(
        `${path} has whole-command permission rules, which match one exact command and nothing else:\n${listed}\n` +
          "  It is your repository's file, so HX has not touched it - rewrite them yourself, or remove them and grant again.",
      );
      continue;
    }
    (permissions as Record<string, unknown>).allow = widened;
    try {
      writeSettingsFile(path, data);
    } catch {
      continue; // read-only settings are not worth failing startup over
    }
    notices.push(`Rewrote whole-command permission rules in ${path}:\n${listed}`);
  }
  return notices;
}

/** The per-segment rewrite for a whole-command rule, or `null` to keep it. */
function widenRule(text: string): string[] | null {
  let rule: Rule;
  try {
    rule = parseRule(text, "settings", "allow");
  } catch (error) {
    if (error instanceof InvalidRule) return null;
    throw error;
  }
  if (rule.tool !== "Bash" || !rule.specifier) return null;
  const parsed = parse(rule.specifier);
  if (parsed.unparseable || parsed.segments.length < 2) return null;
  const replacements = persistableRules("Bash", rule.specifier);
  // A rule with an unwidenable segment comes back as itself: not a change.
  return replacements.length === 1 && replacements[0] === text ? null : replacements;
}

/**
 * Append an allow rule to this machine's settings for this project - not the
 * project's shared `settings.json`, and not inside the repository at all.
 */
export function persistAllowRule(ruleText: string, cwd: string): void {
  const path = projectLocalSettingsFile(cwd);
  const data = readSettingsFile(path);
  if (typeof data.permissions !== "object" || data.permissions === null) data.permissions = {};
  const permissions = data.permissions as Record<string, unknown>;
  if (!Array.isArray(permissions.allow)) permissions.allow = [];
  const allow = permissions.allow as unknown[];
  if (!allow.includes(ruleText)) allow.push(ruleText);
  writeSettingsFile(path, data);
}

export class InvalidRule extends Error {
  override name = "InvalidRule";
}
