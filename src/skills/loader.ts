/**
 * Skill discovery.
 *
 * A skill is a directory containing `SKILL.md` with YAML frontmatter, searched
 * in `~/.hx/skills/*\/` then `<cwd>/.hx/skills/*\/`; project skills shadow user
 * skills of the same name.
 *
 * Progressive disclosure is the whole point: only `name` and `description`
 * enter the cached prefix. The body is injected only when the model calls
 * `Skill(name)`, so a hundred installed skills cost a hundred one-line entries.
 */

import { readdirSync } from "node:fs";
import { FrontmatterError, pyStr, read, require, stringTuple } from "../frontmatter.ts";
import { compareCodePoints } from "../json.ts";
import { isDir, isFile, join, parent } from "../pathlib.ts";
import { projectSkillsDir, userSkillsDir } from "../paths.ts";
import { pyLen, strip } from "../py.ts";

export const SKILL_FILE = "SKILL.md";
const MAX_DESCRIPTION_CHARS = 400;

export interface Skill {
  readonly name: string;
  readonly description: string;
  readonly body: string;
  readonly path: string;
  /** Restricts which tools may run while the skill is active. Empty means no restriction. */
  readonly allowedTools: readonly string[];
  /** Scripts and reference files shipped alongside SKILL.md. */
  readonly resources: readonly string[];
  readonly source: string;
}

/** Find every skill. Malformed skills are skipped with a warning rather than
 * aborting startup. */
export function discover(cwd: string): Skill[] {
  const found = new Map<string, Skill>();
  // User first, so a project skill of the same name overwrites it.
  for (const [directory, source] of [
    [userSkillsDir(), "user"],
    [projectSkillsDir(cwd), "project"],
  ] as const) {
    if (!isDir(directory)) continue;
    for (const entry of readdirSync(directory).sort(compareCodePoints)) {
      const skillFile = join(directory, entry, SKILL_FILE);
      if (!isFile(skillFile)) continue;
      try {
        const skill = parseSkillFile(skillFile);
        found.set(skill.name, { ...skill, source });
      } catch (error) {
        if (!(error instanceof FrontmatterError)) throw error;
        process.stderr.write(`skipping skill: ${error.message}\n`);
      }
    }
  }
  return [...found.values()].sort((a, b) => compareCodePoints(a.name, b.name));
}

/** Parse frontmatter + body. Throws on missing `name`/`description`. */
export function parseSkillFile(path: string): Skill {
  const document = read(path);
  require(document, "name", "description");
  let description = strip(pyStr(document.metadata.description));
  // Descriptions live in the cached prefix for every skill installed.
  if (pyLen(description) > MAX_DESCRIPTION_CHARS) {
    description = `${[...description].slice(0, MAX_DESCRIPTION_CHARS - 1).join("")}…`;
  }
  const directory = parent(path);
  const resources = readdirSync(directory)
    .filter((name) => name !== SKILL_FILE)
    .map((name) => join(directory, name))
    .sort(compareCodePoints);
  return {
    name: strip(pyStr(document.metadata.name)),
    description,
    body: document.body,
    path,
    allowedTools: stringTuple(document.metadata["allowed-tools"]),
    resources,
    source: "",
  };
}

/** Render the prefix index: one `- name: description` line per skill, sorted
 * so the block is byte-stable across runs. */
export function buildIndex(skills: Skill[]): string {
  if (skills.length === 0) return "";
  return [
    "Available skills. Call Skill(name) to load one when the task matches it:",
    ...skills.map((skill) => `- ${skill.name}: ${skill.description}`).sort(compareCodePoints),
  ].join("\n");
}
