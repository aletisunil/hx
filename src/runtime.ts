/**
 * Everything one HX run needs, wired together: settings, provider, session,
 * sandbox, tools, loop. The CLI's print mode and the TUI both start here.
 */

import type { AgentDefinition } from "./agents/definitions.ts";
import { discover as discoverAgents } from "./agents/definitions.ts";
import { SubagentRunner } from "./agents/subagent.ts";
import { AuthResolver } from "./auth/resolve.ts";
import type { Layer } from "./config.ts";
import { liftProjectGrants, loadSettings, type Settings } from "./config.ts";
import { CheckpointStore } from "./core/checkpoints.ts";
import { Compactor } from "./core/compaction.ts";
import {
  buildProjectContext,
  ContextBuilder,
  loadInstructions,
  loadSystemPrompt,
} from "./core/context.ts";
import { EventBus } from "./core/events.ts";
import { type Injection, InjectionRegistry, type Injector } from "./core/lateinject.ts";
import { AgentLoop } from "./core/loop.ts";
import { loadSession, newSession, type Session } from "./core/session.ts";
import { GitWatcher, gitInjector } from "./git.ts";
import { HookEngine } from "./hooks/engine.ts";
import { describe, tlsNotice } from "./net.ts";
import { exists, join } from "./pathlib.ts";
import {
  ensureUserDirs,
  projectLocalSettingsFile,
  projectMcpFile,
  projectSettingsFile,
  sessionCheckpointsDir,
  sessionOutputsDir,
  userHome,
} from "./paths.ts";
import { loadRules, migrateLegacyRules, PermissionEngine } from "./permissions/engine.ts";
import { defaultPolicy, Sandbox } from "./permissions/sandbox.ts";
import type { Provider } from "./providers/base.ts";
import { ModelRegistry } from "./providers/models.ts";
import { buildProvider } from "./providers/registry.ts";
import { buildIndex, discover as discoverSkills, type Skill } from "./skills/loader.ts";
import { ActiveSkills, SkillTool } from "./skills/runtime.ts";
import { BackgroundJobs, PersistentShell } from "./tools/bash.ts";
import { buildDefaultRegistry, type ToolRegistry } from "./tools/registry.ts";
import { TaskTool } from "./tools/task.ts";
import { TodoList, todoInjector } from "./tools/todo.ts";

export class Runtime {
  /** Startup messages for the user - shown once, in the transcript. */
  readonly notices: string[];

  constructor(
    readonly settings: Settings,
    readonly session: Session,
    readonly bus: EventBus,
    readonly loop: AgentLoop,
    readonly models: ModelRegistry,
    /** Every route's credentials. */
    readonly auth: AuthResolver,
    public provider: Provider,
    readonly shell: PersistentShell | null,
    readonly jobs: BackgroundJobs | null,
    readonly sandbox: Sandbox | null,
    readonly skills: Map<string, Skill>,
    readonly agents: Map<string, AgentDefinition>,
    readonly tools: ToolRegistry,
    /** Pre-images for `/rewind`. */
    readonly checkpoints: CheckpointStore,
    /** What `/hooks` reports on. */
    readonly hooks: HookEngine,
    notices: string[],
  ) {
    this.notices = notices;
  }

  get sandboxActive(): boolean {
    return this.sandbox?.active ?? false;
  }

  get sandboxBackend(): string {
    return this.sandbox !== null ? this.sandbox.backend : "none";
  }

  /**
   * Connect MCP servers and register their tools. The MCP client is still to
   * come across from the Python build; a configured server is said to be
   * unavailable rather than left out without a word.
   */
  async connectMcp(): Promise<
    { name: string; connected: boolean; needsLogin: boolean; error: string }[]
  > {
    const configured = [projectMcpFile(this.settings.cwd), join(userHome(), "mcp.json")].some(
      exists,
    );
    if (!configured) return [];
    return [
      {
        name: "mcp",
        connected: false,
        needsLogin: false,
        error: "MCP servers are not ported to the TypeScript build yet",
      },
    ];
  }

  /**
   * Re-fetch the model catalogue once a day. A failure is not worth
   * interrupting the session for - the cached catalogue still works - but it
   * is worth one line in the transcript.
   */
  async refreshModelsIfStale(): Promise<void> {
    if (!this.models.isStale) return;
    try {
      await this.models.refresh(this.auth);
    } catch (error) {
      this.notices.push(`Model catalogue refresh failed: ${describe(error)}`);
      return;
    } finally {
      this.loop.catalogueRefreshed();
    }
    for (const [label, error] of this.models.subscriptionErrors()) {
      this.notices.push(`Could not list this account's ${label} models: ${error}`);
    }
    this.notices.push(...this.models.subscriptionAdvice());
  }

  async aclose(): Promise<void> {
    await this.jobs?.closeAll();
    await this.shell?.close();
    this.sandbox?.cleanup();
    await this.provider.aclose();
  }
}

/**
 * Say what the one-time settings cleanups moved, and where. Silently
 * relocating someone's permission grants would be worse than leaving them.
 */
function settingsMigrationNotices(cwd: string): string[] {
  const moved = liftProjectGrants(cwd);
  if (!moved?.length) return [];
  const listed = moved.map((rule) => `  ${rule}`).join("\n");
  return [
    `Moved ${moved.length} permission grant(s) out of ${projectSettingsFile(cwd)}, which is your repository's file, into ${projectLocalSettingsFile(cwd)}:\n` +
      `${listed}\n` +
      "  They still apply to this project on this machine. Earlier versions of HX wrote them into the checkout; it no longer writes there at all.",
  ];
}

/** What builds a run: the parsed command line's part of it. */
export interface RuntimeArgs {
  readonly cwd: string | null;
  readonly overrides: Layer | null;
}

/** Boot the stack: settings, provider, session, sandbox, tools, loop. */
export function buildRuntime(parsed: RuntimeArgs, resume: string | null = null): Runtime {
  ensureUserDirs();
  const settings = loadSettings(parsed.cwd, parsed.overrides);
  const bus = new EventBus();
  const auth = new AuthResolver();

  const models = new ModelRegistry();
  // The built-in Codex list is stale as soon as OpenAI ships a model.
  models.addCodexModels(settings.models.codexModels);
  models.setReasoningEffort(settings.models.reasoningEffort);
  models.loadCache();
  const modelInfo = models.getOrDefault(settings.models.model);

  const session = resume ? loadSession(resume) : newSession(settings.cwd, settings.models.model);
  // Built after the session because a subscription route keys its prompt
  // cache on the session id.
  const provider = buildProvider(settings.models.model, auth, {
    sessionId: session.meta.sessionId,
    models,
  });

  const sandbox = settings.permissions.sandbox
    ? new Sandbox(defaultPolicy(settings.cwd, settings.permissions.allowNetwork))
    : null;
  const shell = new PersistentShell(settings.cwd, settings.bash.shell, {}, sandbox);
  const jobs = new BackgroundJobs(join(sessionOutputsDir(session.meta.sessionId), "jobs"));

  const todos = new TodoList();
  const injections = new InjectionRegistry();
  injections.register("todos", todoInjector(todos));

  // Rules come from the settings files only, read once here.
  const notices = settingsMigrationNotices(settings.cwd);
  notices.push(...migrateLegacyRules(settings.cwd));
  const tls = tlsNotice();
  if (tls !== null) notices.push(tls);
  const permissions = new PermissionEngine(
    settings.permissions.mode,
    loadRules(settings.cwd),
    settings.cwd,
  );

  const contextBuilder = new ContextBuilder(
    loadSystemPrompt(settings.cwd, settings.prompt),
    settings.cwd,
    settings.context.keepRecentTurns,
  );
  const instructions = loadInstructions(settings.cwd);
  const projectContext = buildProjectContext(settings.cwd, instructions);

  const checkpoints = new CheckpointStore(session, sessionCheckpointsDir(session.meta.sessionId));
  const tools = buildDefaultRegistry({ shell, jobs, todos, bus, auth, checkpoints });

  const skills = new Map(discoverSkills(settings.cwd).map((skill) => [skill.name, skill]));
  if (skills.size) tools.register(new SkillTool(skills, new ActiveSkills()));

  const hooks = HookEngine.load(settings.cwd, session.meta.sessionId);
  if (hooks.ignored.length) {
    notices.push(
      `Ignored ${hooks.ignored.length} hook(s) declared in the project's shared settings - hooks run shell commands, so they are only loaded from your own settings. See /hooks.`,
    );
  }

  const agents = new Map(discoverAgents(settings.cwd).map((agent) => [agent.name, agent]));
  const subagents = new SubagentRunner({
    definitions: agents,
    provider,
    tools,
    permissions,
    bus,
    settings,
    models,
    parentSessionId: session.meta.sessionId,
    parentUsage: session.usage,
    hooks,
    projectContext,
  });
  tools.register(new TaskTool(subagents));

  const loop = new AgentLoop({
    provider,
    session,
    tools,
    permissions,
    context: contextBuilder,
    compactor: new Compactor(
      provider,
      settings.models.model,
      settings.context.keepRecentTurns,
      contextBuilder,
    ),
    injections,
    bus,
    settings,
    modelInfo,
    skillsIndex: buildIndex([...skills.values()]) || null,
    projectContext,
    instructions,
    hooks,
    models,
  });
  // After the loop, because both ask it: what counts as read belongs to
  // whichever session it holds now.
  injections.register("stale_files", staleFilesInjector(loop));
  if (settings.context.gitNotices) {
    // Registered even outside a repository: the watcher disables itself on its
    // first call.
    injections.register(
      "git",
      gitInjector(new GitWatcher(settings.cwd), (path) => loop.reads.wasRead(path)),
    );
  }

  return new Runtime(
    settings,
    session,
    bus,
    loop,
    models,
    auth,
    provider,
    shell,
    jobs,
    sandbox,
    skills,
    agents,
    tools,
    checkpoints,
    hooks,
    notices,
  );
}

/**
 * Warn when a file changed on disk after HX last read it. Late injection, not
 * the system prompt: the set changes constantly.
 */
function staleFilesInjector(loop: AgentLoop): Injector {
  return (): Injection | null => {
    const stale = loop.reads.staleFiles();
    if (stale.length === 0) return null;
    const listed = stale
      .slice(0, 20)
      .map((path) => `- ${path}`)
      .join("\n");
    return {
      source: "stale_files",
      text: `These files changed on disk since you last read them. Re-read before editing:\n${listed}`,
      priority: 20,
    };
  };
}
