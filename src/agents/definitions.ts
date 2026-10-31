/**
 * Agent definitions from `.hx/agents/*.md` and `~/.hx/agents/*.md`. Same
 * frontmatter shape as skills, plus `tools` and `model`.
 */

import { FrontmatterError, pyStr, read, require, stringTuple } from "../frontmatter.ts";
import { glob } from "../glob.ts";
import { compareCodePoints } from "../json.ts";
import { isDir } from "../pathlib.ts";
import { projectAgentsDir, userAgentsDir } from "../paths.ts";
import { strip } from "../py.ts";

export interface AgentDefinition {
  readonly name: string;
  /** Shown to the parent model in the Task tool schema, so it picks the right agent. */
  readonly description: string;
  readonly systemPrompt: string;
  /** Allowlist. Empty means all tools except Task. */
  readonly tools: readonly string[];
  /** Defaults to `settings.models.subagentModel`. */
  readonly model: string | null;
  readonly path: string | null;
}

const EXPLORE: AgentDefinition = {
  name: "explore",
  description:
    "Read-only search across the codebase. Use when finding where something lives would cost the main conversation many tool results.",
  systemPrompt:
    "You are a search agent. Locate the relevant code and report back concisely.\n\n" +
    "Read only what you need - excerpts, not whole files. Report file paths with " +
    "line numbers and a one-line note on what each contains. Do not review or " +
    "critique the code, and do not modify anything.\n\n" +
    "Your reply is the entire result: the caller cannot see your tool output.",
  tools: ["Read", "Glob", "Grep"],
  model: null,
  path: null,
};

const PLAN: AgentDefinition = {
  name: "plan",
  description: "Design an implementation approach for a task, without writing code.",
  systemPrompt:
    "You are a software architect. Produce a concrete implementation plan.\n\n" +
    "Read the relevant code first. Name the files to change and what changes in " +
    "each, reuse what already exists rather than inventing parallel machinery, " +
    "and state the trade-offs you rejected. Do not modify anything.\n\n" +
    "Your reply is the entire result: the caller cannot see your tool output.",
  tools: ["Read", "Glob", "Grep"],
  model: null,
  path: null,
};

const GENERAL: AgentDefinition = {
  name: "general",
  description: "A multi-step task that does not fit the other agents.",
  systemPrompt:
    "Carry out the task described and report what you did.\n\n" +
    "You cannot ask follow-up questions, so make reasonable decisions and state " +
    "the assumptions you made. Your reply is the entire result: the caller " +
    "cannot see your tool output.",
  tools: [],
  model: null,
  path: null,
};

/** Always available: read-only search, planning, and a catch-all. */
export const BUILTIN_AGENTS: readonly AgentDefinition[] = [EXPLORE, PLAN, GENERAL];

/** Builtins plus user and project definitions, project last. */
export function discover(cwd: string): AgentDefinition[] {
  const found = new Map(BUILTIN_AGENTS.map((agent) => [agent.name, agent]));
  for (const directory of [userAgentsDir(), projectAgentsDir(cwd)]) {
    if (!isDir(directory)) continue;
    for (const path of glob(directory, "*.md").sort(compareCodePoints)) {
      try {
        const agent = parseAgentFile(path);
        found.set(agent.name, agent);
      } catch (error) {
        if (!(error instanceof FrontmatterError)) throw error;
        process.stderr.write(`skipping agent: ${error.message}\n`);
      }
    }
  }
  return [...found.values()].sort((a, b) => compareCodePoints(a.name, b.name));
}

export function parseAgentFile(path: string): AgentDefinition {
  const document = read(path);
  require(document, "name", "description");
  const model = document.metadata.model;
  return {
    name: strip(pyStr(document.metadata.name)),
    description: strip(pyStr(document.metadata.description)),
    systemPrompt: document.body,
    tools: stringTuple(document.metadata.tools),
    model: model ? strip(pyStr(model)) : null,
    path,
  };
}
