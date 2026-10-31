/**
 * Todo and plan tracking.
 *
 * The list is state, not conversation: it is rendered into context by a late
 * injector every turn rather than accumulating as tool results, so an updated
 * list does not push the previous six copies of itself through the cache.
 */

import type { EventBus } from "../core/events.ts";
import type { Injection, Injector } from "../core/lateinject.ts";
import { repr, strip } from "../py.ts";
import {
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "./base.ts";

const DESCRIPTION = `Create and update a structured task list for the current work.

Use it for multi-step tasks. Exactly one item may be \`in_progress\` at a time;
mark items completed as soon as they are done, not in a batch at the end.`;

export const TODO_STATUSES = ["pending", "in_progress", "completed"] as const;
export type TodoStatus = (typeof TODO_STATUSES)[number];

export interface Todo {
  readonly content: string;
  readonly status: TodoStatus;
  /** Present-continuous phrasing shown while the item is in progress. */
  readonly activeForm: string;
}

export const todoToDict = (item: Todo) => ({
  content: item.content,
  status: item.status,
  active_form: item.activeForm,
});

export class TodoList {
  items: Todo[] = [];

  /** Throws `ToolError` if more than one item is in progress. */
  replace(items: Todo[]): void {
    const inProgress = items.filter((item) => item.status === "in_progress");
    if (inProgress.length > 1) {
      throw new ToolError(
        `${inProgress.length} items are in_progress. Exactly one may be in progress at a time.`,
      );
    }
    this.items = [...items];
  }

  /** Markdown checklist for late injection. */
  render(): string {
    const marks: Record<TodoStatus, string> = {
      completed: "[x]",
      in_progress: "[~]",
      pending: "[ ]",
    };
    return [
      "Current todo list (keep it up to date as you work):",
      ...this.items.map((item) => `${marks[item.status]} ${item.content}`),
    ].join("\n");
  }

  isEmpty(): boolean {
    return this.items.length === 0;
  }

  asDicts(): Record<string, unknown>[] {
    return this.items.map(todoToDict);
  }
}

export class TodoWriteTool extends Tool {
  readonly name = "TodoWrite";
  readonly description = DESCRIPTION;

  constructor(
    private readonly todos: TodoList,
    private readonly bus: EventBus | null = null,
  ) {
    super();
  }

  schema() {
    return {
      type: "object",
      properties: {
        todos: {
          type: "array",
          items: {
            type: "object",
            properties: {
              content: { type: "string" },
              status: { type: "string", enum: [...TODO_STATUSES] },
              active_form: {
                type: "string",
                description: "Present continuous, e.g. 'Running the tests'",
              },
            },
            required: ["content", "status"],
          },
        },
      },
      required: ["todos"],
    };
  }

  /** Replaces the list wholesale and publishes `TodosUpdated` for the sidebar. */
  async run(params: Params, _ctx: ToolContext): Promise<ToolResult> {
    const items = (params.todos as Record<string, unknown>[]).map(parseTodo);
    this.todos.replace(items);
    this.bus?.publish({ kind: "TodosUpdated", todos: this.todos.asDicts() });
    const done = items.filter((item) => item.status === "completed").length;
    // The list itself is late-injected every turn, so echoing it back here
    // would put a second, immediately stale copy in the transcript.
    return toolResult(`Todo list updated: ${done}/${items.length} complete.`, {
      summary: `${done}/${items.length} done`,
    });
  }
}

function parseTodo(raw: Record<string, unknown>): Todo {
  const status = String(raw.status ?? "pending");
  if (!TODO_STATUSES.includes(status as TodoStatus)) {
    throw new ToolError(`unknown todo status ${repr(raw.status ?? null)}`);
  }
  const content = strip(String(raw.content ?? ""));
  if (!content) throw new ToolError("every todo needs content");
  return { content, status: status as TodoStatus, activeForm: String(raw.active_form ?? "") };
}

/** Late injector emitting the current list, or `null` when empty. */
export function todoInjector(todos: TodoList): Injector {
  return (): Injection | null =>
    todos.isEmpty() ? null : { source: "todos", text: todos.render(), priority: 10 };
}
