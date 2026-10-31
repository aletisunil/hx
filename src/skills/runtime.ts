/** The `Skill` tool - loads a skill body into the conversation on demand. */

import { compareCodePoints } from "../json.ts";
import { repr } from "../py.ts";
import {
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "../tools/base.ts";
import type { Skill } from "./loader.ts";

const DESCRIPTION = `Load a skill's full instructions.

Skills are listed in the system context by name and description. Call this when
the current task matches one; its instructions then apply for the rest of the turn.`;

export class SkillTool extends Tool {
  readonly name = "Skill";
  readonly description = DESCRIPTION;

  constructor(
    private readonly skills: Map<string, Skill>,
    readonly active: ActiveSkills = new ActiveSkills(),
  ) {
    super();
  }

  /** `name` is an enum of installed skills, so an invalid name fails at
   * validation rather than after a round trip. */
  schema() {
    return {
      type: "object",
      properties: {
        name: { type: "string", enum: [...this.skills.keys()].sort(compareCodePoints) },
      },
      required: ["name"],
    };
  }

  /**
   * Returns the skill body as the tool result. Skill bodies are content, not
   * privileged directives: an `allowed-tools` restriction narrows permissions
   * and never widens them.
   */
  async run(params: Params, _ctx: ToolContext): Promise<ToolResult> {
    const name = String(params.name);
    const skill = this.skills.get(name);
    if (skill === undefined) {
      const available = [...this.skills.keys()].sort(compareCodePoints).join(", ") || "none";
      throw new ToolError(`unknown skill ${repr(name)}. Installed skills: ${available}`);
    }
    this.active.activate(skill);
    let body = skill.body;
    if (skill.resources.length)
      body += `\n\nFiles shipped with this skill:\n${skill.resources.map((item) => `- ${item}`).join("\n")}`;
    if (skill.allowedTools.length)
      body += `\n\nWhile this skill is active, use only: ${skill.allowedTools.join(", ")}.`;
    return toolResult(body, { summary: `loaded ${name}` });
  }
}

/** Tracks skills loaded this session, for the status bar and tool gating. */
export class ActiveSkills {
  private readonly active = new Map<string, Skill>();

  activate(skill: Skill): void {
    this.active.set(skill.name, skill);
  }

  names(): string[] {
    return [...this.active.keys()].sort(compareCodePoints);
  }

  /** Intersection of every active skill's `allowed-tools`, or `null` when
   * unrestricted. Intersection, not union: loading a second skill must never
   * widen the first one's restriction. */
  toolAllowlist(): Set<string> | null {
    const allowlists = [...this.active.values()]
      .filter((s) => s.allowedTools.length)
      .map((s) => new Set(s.allowedTools));
    if (allowlists.length === 0) return null;
    const [first, ...rest] = allowlists as [Set<string>, ...Set<string>[]];
    return new Set([...first].filter((name) => rest.every((set) => set.has(name))));
  }
}
