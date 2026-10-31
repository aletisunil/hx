/**
 * Running an interactive session.
 *
 * Events arrive on the bus and are dispatched to components. The session owns
 * the turn, the queue, the overlay, and whichever approval currently holds the
 * keyboard - and it is the object the slash commands are written against, so
 * the surface they may ask for is gathered in one block.
 */

import type { AgentDefinition } from "../agents/definitions.ts";
import { AuthResolver } from "../auth/resolve.ts";
import type { PermissionMode, Settings } from "../config.ts";
import { PERMISSION_MODES } from "../config.ts";
import type { CheckpointStore, RestoreReport } from "../core/checkpoints.ts";
import type { AssembledContext } from "../core/context.ts";
import type { EventBus } from "../core/events.ts";
import { ImageError, loadImage, loadImageFile } from "../core/images.ts";
import type { AgentLoop } from "../core/loop.ts";
import {
  type ContentBlock,
  type ImageBlock,
  interruptedOutput,
  messageImages,
  messageText,
  type ToolResultBlock,
  type ToolUseBlock,
  toolResults,
  type UserTurn,
} from "../core/messages.ts";
import {
  type Session as CoreSession,
  loadSession,
  newSession,
  type SessionMeta,
} from "../core/session.ts";
import { formatTokens } from "../core/usage.ts";
import { BranchWatcher } from "../git.ts";
import { keymap } from "../keys.ts";
import { basename } from "../pathlib.ts";
import { sessionCheckpointsDir, tilde } from "../paths.ts";
import type { PermissionAnswer, PermissionRequest } from "../permissions/engine.ts";
import type { Provider } from "../providers/base.ts";
import type { ModelRegistry } from "../providers/models.ts";
import * as registry from "../providers/registry.ts";
import { strip } from "../py.ts";
import type { Skill } from "../skills/loader.ts";
import type { Component } from "../term/component.ts";
import type { Key } from "../term/keydecode.ts";
import { TuiRunner } from "../term/loop.ts";
import { ShiftEnter } from "../term/modifiers.ts";
import { Spacer } from "../term/primitives.ts";
import type { Terminal } from "../term/terminal.ts";
import { VERSION } from "../version.ts";
import { ClipboardError, copyText, readImage } from "./clipboard.ts";
import {
  buildDefaultCommands,
  CommandContext,
  type CommandRegistry,
  type McpManager,
} from "./commands.ts";
import { toolCall } from "./renderers.ts";
import { THEME } from "./theme.ts";
import {
  AssistantMessage,
  Notice,
  ThinkingMessage,
  TodoBlock,
  ToolBlock,
  UserMessage,
} from "./views/blocks.ts";
import { PermissionPrompt } from "./views/permission.ts";
import { CommandPalette } from "./views/pickers.ts";
import { Prompt } from "./views/prompt.ts";
import type { StatusBar } from "./views/status.ts";
import { Session } from "./views/transcript.ts";

/** Milliseconds between spinner frames. */
export const SPINNER_INTERVAL = 80;

/** A branch checkout should reach the status bar promptly; the watcher only
 * stats `HEAD` between changes, so polling is cheap. */
export const BRANCH_POLL_INTERVAL = 1000;

/** Tools whose own block is suppressed because something else draws the
 * result: TodoWrite, whose plan the `TodosUpdated` block shows. A failed call
 * publishes no `TodosUpdated`, so the held block is appended then. */
const SILENT_TOOLS = new Set(["todowrite"]);

/** What is worth pressing, longest-lived first: the bar drops whole hints
 * from the end on a narrow terminal, so the order is the priority. */
const HINTS: [string, string][] = [
  ["esc", "interrupt"],
  ["ctrl+c", "clear"],
  ["ctrl+d", "exit"],
  ["ctrl+o", "expand"],
  ["ctrl+p", "palette"],
  ["ctrl+l", "model"],
  ["/", "commands"],
  ["!", "bash"],
  ["@", "files"],
];

/** What the CLI hands an interactive session. */
export interface SessionExtras {
  models?: ModelRegistry | null;
  auth?: AuthResolver | null;
  sandboxActive?: boolean;
  sandboxBackend?: string;
  skills?: Map<string, Skill> | null;
  agents?: Map<string, AgentDefinition> | null;
  mcp?: McpManager | null;
  notices?: string[];
  checkpoints?: CheckpointStore | null;
  renameLeft?: string | null;
  terminal?: Terminal | null;
  /** The model's route has no credential: what to say, on the first screen. */
  signIn?: string | null;
}

/** Work that runs beside a turn - a slash command or a shell line - and the
 * controller an interrupt aborts it with. */
interface SideTask {
  readonly done: Promise<void>;
  readonly abort: AbortController;
  finished: boolean;
}

const abortError = () => Object.assign(new Error("cancelled"), { name: "AbortError" });
const errorText = (error: unknown) => (error instanceof Error ? error.message : String(error));
const isAbort = (error: unknown) => (error as Error | null)?.name === "AbortError";

/** One interactive session, drawn by `term`. */
export class HXSession {
  readonly prompt: Prompt;
  readonly view: Session;
  readonly runner: TuiRunner;
  commands: CommandRegistry | null = null;
  /** A session from another directory to start again on, once this exits. */
  relaunch: string | null = null;

  private readonly branch: BranchWatcher;
  private readonly tools = new Map<string, ToolBlock>();
  /** Calls held at an approval, by id: the approval is asked before the
   * block that will show the call exists. */
  private readonly awaiting = new Set<string>();
  /** Approvals waiting on the user, oldest first. The keyboard goes to the
   * oldest unanswered one. */
  private readonly pending: PermissionPrompt[] = [];
  /** Settles `ask` when whatever is on the overlay reports that it is done,
   * or abandons it on the way out. */
  private overlayFinished: (() => void) | null = null;
  private overlayAbandoned: ((reason: Error) => void) | null = null;
  private queuedTurns: UserTurn[] = [];
  /** Detached work - session renaming - and what stops it on the way out. */
  private readonly background = new Set<Promise<void>>();
  private readonly backgroundAbort = new AbortController();
  private mouse = false;
  /** Set by one ctrl+c on an empty, idle prompt; a second one exits. */
  private clearArmed = false;
  /** Tells a shift+enter the terminal sent as enter from a plain enter. */
  private readonly shiftEnter = new ShiftEnter();
  private commandContext: CommandContext | null = null;
  private readonly silent = new Set<string>();
  private assistant: AssistantMessage | null = null;
  private thinking: ThinkingMessage | null = null;
  private turnStarted = 0;
  /** The running model turn, and nothing else. */
  private turn: Promise<void> | null = null;
  /** The user stopped the running turn; it is winding down. */
  private turnStopping = false;
  /** A slash command or shell line, which runs beside a turn. */
  private side: SideTask | null = null;
  private readonly extra: SessionExtras;
  private resolver: AuthResolver | null;

  constructor(
    readonly loop: AgentLoop,
    readonly bus: EventBus,
    readonly settings: Settings,
    extra: SessionExtras = {},
  ) {
    this.extra = extra;
    this.resolver = extra.auth ?? null;
    this.branch = new BranchWatcher(settings.cwd);
    // The configured palette, before anything is drawn.
    THEME.use(settings.theme);
    this.prompt = new Prompt(settings.cwd, {
      rowsAvailable: () => this.terminalRows(),
      onSubmit: (text, images) => this.submit(text, images),
      onSteer: (text, images) => this.steer(text, images),
      onImageRequest: (paths) => this.requestImages(paths),
    });
    this.view = new Session(VERSION, settings.quietStartup, this.prompt);
    this.runner = new TuiRunner(
      this.view,
      extra.terminal ?? null,
      (key) => this.onKey(key),
      settings.tui.fullscreen,
    );
  }

  // -- lifecycle -------------------------------------------------------------

  async run(): Promise<void> {
    this.commands = buildDefaultCommands();
    this.prompt.commands = this.commands;
    this.commandContext = new CommandContext(this, this.settings, this.commands);

    this.view.dock.hints.setHints(HINTS);
    this.refreshStatus();
    // `hx resume <id>` loads the session before this object exists.
    if (this.loop.session.activeMessages().length) this.replayTranscript();
    if (this.extra.signIn) this.showSignIn(this.extra.signIn);
    if (this.loop.permissions !== null)
      this.loop.permissions.asker = (request) => this.askPermission(request);

    const events = this.consumeEvents();
    const spinner = setInterval(() => this.spin(), SPINNER_INTERVAL);
    const status = setInterval(() => this.watchStatus(), BRANCH_POLL_INTERVAL);
    if (this.extra.renameLeft) this.detach(this.renameLeftBehind(this.extra.renameLeft));
    try {
      await this.runner.run();
    } finally {
      clearInterval(spinner);
      clearInterval(status);
      // Detached work included: renaming a session left with /clear is not
      // worth the user waiting for their shell prompt.
      this.backgroundAbort.abort();
      if (this.turn !== null) this.loop.cancel();
      for (const prompt of [...this.pending]) prompt.abandon();
      this.side?.abort.abort();
      this.overlayAbandoned?.(abortError());
      await Promise.allSettled(
        [this.turn, this.side?.done].filter((p) => p !== null && p !== undefined),
      );
      void events;
    }
  }

  private spin(): void {
    if (!this.turnStarted) return;
    const elapsed = (performance.now() - this.turnStarted) / 1000;
    // A frame only when something moved. Held at an approval nothing does.
    let moved = this.view.dock.working.tick(elapsed);
    for (const block of this.tools.values()) moved = block.tick() || moved;
    if (moved) this.runner.requestRender();
  }

  /** Keep the status bar current while nothing else redraws it: the branch
   * across external checkouts, and a usage window that resets. */
  private watchStatus(): void {
    const previous = this.branch.branch;
    const current = this.branch.poll();
    const status = this.view.dock.status;
    if (current !== previous || status.branch !== current) {
      status.setLocation(tilde(this.settings.cwd), current);
      this.runner.requestRender();
    } else if (status.redrawDue()) {
      status.invalidate();
      this.runner.requestRender();
    }
  }

  /** Say how to sign in, a row below the header, under the logo - unless a
   * resumed conversation is already on screen. */
  private showSignIn(hint: string): void {
    if (!this.view.transcript.blocks.length) this.view.header.logo = true;
    this.view.transcript.add(new Spacer(1));
    this.view.transcript.add(new Notice(hint, "warning"));
  }

  // -- the surface the slash commands expect ---------------------------------

  get status(): StatusBar {
    return this.view.dock.status;
  }

  get models(): ModelRegistry | null {
    return this.extra.models ?? null;
  }

  /** The credential resolver, defaulted so a session started without one
   * can still switch routes. */
  get auth(): AuthResolver {
    this.resolver ??= new AuthResolver();
    return this.resolver;
  }

  get skills(): Map<string, Skill> {
    return this.extra.skills ?? new Map();
  }

  get agents(): Map<string, AgentDefinition> {
    return this.extra.agents ?? new Map();
  }

  get mcp(): McpManager | null {
    return this.extra.mcp ?? null;
  }

  get sandboxActive(): boolean {
    return this.extra.sandboxActive ?? true;
  }

  get sandboxBackend(): string {
    return this.extra.sandboxBackend || "";
  }

  get mode(): string {
    return this.loop.permissions === null ? "default" : this.loop.permissions.mode;
  }

  get queued(): string[] {
    return this.queuedTurns.map((turn) => turn.text);
  }

  /** The newest answer - not the newest block, which after `/copy` is the
   * command's own echo. */
  lastReplyText(): string {
    for (const block of [...this.view.transcript.blocks].reverse())
      if (block instanceof AssistantMessage && strip(block.text)) return block.text;
    return "";
  }

  /** What the last model call was sent, section by section - `/context`. */
  get lastContext(): AssembledContext | null {
    return this.loop.lastContext;
  }

  notice(text: string, level = "info"): void {
    this.addNotice(text, level);
  }

  /** Draw soon, for a component that changes without a keystroke. */
  repaint(): void {
    this.runner.requestRender();
  }

  /** Put something on the overlay without waiting for it, for a flow that
   * drives its own screen. */
  show(component: Component): void {
    this.sizeOverlay(component);
    this.view.show(component);
    this.runner.requestImmediateRender();
  }

  /** Tell an overlay how much room it has. */
  private sizeOverlay(component: Component): void {
    const setter = (component as { setRowsAvailable?: (rows: () => number) => void })
      .setRowsAvailable;
    setter?.call(component, () => this.overlayRows());
  }

  /** Rows an overlay may use, asked of the view that lays it out. */
  private overlayRows(): number {
    const [width, rows] = this.runner.terminal.size;
    return this.view.overlayRows(width, rows);
  }

  /** Take the overlay down. The component is accepted and ignored: only one
   * thing is ever on the overlay. */
  dismissModal(_component: Component | null = null): void {
    this.view.dismiss();
    this.runner.requestImmediateRender();
  }

  exit(): void {
    this.runner.stop();
  }

  /** Leave, so HX can start again on a session from another directory. The
   * CLI relaunches once the terminal is restored. */
  relaunchInto(meta: SessionMeta): void {
    this.relaunch = meta.sessionId;
    this.addNotice(`Opening ${meta.title || meta.sessionId} in ${tilde(meta.cwd)}…`, "info");
    this.runner.requestImmediateRender();
    this.runner.stop();
  }

  setMode(mode: PermissionMode): void {
    if (this.loop.permissions !== null) this.loop.permissions.mode = mode;
    this.status.setMode(mode, this.sandboxActive, this.sandboxBackend);
    this.runner.requestRender();
  }

  applyTheme(name: string): void {
    THEME.use(name);
    // Every cached line holds the old colours, so the document is redrawn
    // from scratch rather than diffed against them.
    invalidateAll(this.view);
    this.runner.requestImmediateRender();
  }

  /** Copy, returning which mechanism took it. OSC 52 is the terminal itself,
   * which works over ssh where no local clipboard command exists. */
  copy(text: string): Promise<string> {
    return copyText(text, (payload) => this.runner.terminal.write(payload));
  }

  get fullscreen(): boolean {
    return this.runner.fullscreen;
  }

  /** Move the session between the alternate screen and the scrollback. */
  setFullscreen(enabled: boolean): void {
    this.runner.setFullscreen(enabled);
  }

  /** Wipe the screen and the scrollback above it, then draw again. */
  clearScreen(): void {
    this.runner.clearScreen();
  }

  /**
   * Mouse reporting on or off. Answers false, as the Python build's did, so
   * `/mouse` then says the driver has none; kept for parity until cutover.
   */
  setMouseReporting(enabled: boolean): boolean {
    this.runner.terminal.setMouse(enabled);
    this.mouse = enabled;
    return false;
  }

  get mouseReporting(): boolean {
    return this.mouse;
  }

  submitToModel(text: string): void {
    this.submit(text);
  }

  get checkpoints(): CheckpointStore | null {
    return this.extra.checkpoints ?? null;
  }

  /** Apply a new credential to the running provider, now. */
  setApiKey(key: string): void {
    const provider = this.loop.provider as Provider & { setApiKey?: (key: string) => void };
    provider.setApiKey?.(key);
  }

  /** Re-read the model in force from a catalogue that has just changed. */
  catalogueRefreshed(): void {
    this.loop.catalogueRefreshed();
    const usage = this.loop.session.usage;
    this.status.setContext(usage.contextTokens, usage.contextWindow);
    this.runner.requestRender();
  }

  /** Point the session at whichever provider serves `modelId`. Routes are
   * compared by model id, so an injected provider is left alone as long as
   * the route has not changed. */
  async useRouteFor(modelId: string): Promise<void> {
    const spec = registry.providerFor(modelId);
    if (spec.id === registry.providerFor(this.loop.model).id) return;
    await this.auth.checkExternalLogins();
    const provider = registry.buildProvider(modelId, this.auth, {
      sessionId: this.loop.session.meta.sessionId,
      models: this.models,
    });
    const previous = this.loop.setProvider(provider);
    this.retargetSubagents(provider);
    try {
      await previous.aclose();
    } catch {
      // The old route is going away either way.
    }
  }

  /** Subagents share the parent's provider; they must follow the switch. */
  private retargetSubagents(provider: Provider): void {
    try {
      const task = this.loop.tools.get("Task") as { setProvider?: (provider: Provider) => void };
      task.setProvider?.(provider);
    } catch {
      // No Task tool in this session.
    }
  }

  /** Name the session being left behind for what it ended up being. Detached:
   * the user asked for a new session, not to wait on a name for the old one. */
  renameOutgoingSession(session: CoreSession): void {
    if (!session.messages.length) return;
    this.detach(this.loop.retitleSession(session, this.backgroundAbort.signal));
  }

  /** Rename the session a `/resume` into another directory left behind. */
  private async renameLeftBehind(sessionId: string): Promise<void> {
    let session: CoreSession;
    try {
      session = loadSession(sessionId);
    } catch {
      return;
    }
    if (session.messages.length)
      await this.loop.retitleSession(session, this.backgroundAbort.signal);
  }

  private detach(work: Promise<void>): void {
    const task = work.catch(() => {});
    this.background.add(task);
    void task.finally(() => this.background.delete(task));
  }

  /** Fresh transcript, same directory. The old session stays on disk. */
  startNewSession(): void {
    this.renameOutgoingSession(this.loop.session);
    this.loop.session = newSession(this.settings.cwd, this.loop.model);
    // The session id is the prompt-cache key on routes that use one.
    this.setProviderSession(this.loop.session.meta.sessionId);
    this.attachCheckpoints();
    this.view.transcript.clear();
    this.addNotice("New session started.", "success");
    const window = this.loop.modelInfo ? this.loop.modelInfo.contextWindow : 0;
    this.status.setTokens(0, 0);
    this.status.setCache(0, 0, 0);
    this.status.update({ costUsd: 0 });
    this.status.setContext(0, window);
    this.runner.requestImmediateRender();
  }

  private setProviderSession(sessionId: string): void {
    const provider = this.loop.provider as Provider & { setSessionId?: (id: string) => void };
    provider.setSessionId?.(sessionId);
  }

  resumeSession(sessionId: string): void {
    let session: CoreSession;
    try {
      session = loadSession(sessionId);
    } catch (error) {
      this.addNotice(`Could not resume ${sessionId}: ${errorText(error)}`, "error");
      return;
    }
    this.renameOutgoingSession(this.loop.session);
    this.loop.session = session;
    this.setProviderSession(session.meta.sessionId);
    this.attachCheckpoints();
    this.replayTranscript();
    this.addNotice(`Resumed ${sessionId}.`, "success");
    this.runner.requestImmediateRender();
  }

  /**
   * Take the session back to just before message `index`. Files first, then
   * the transcript. The prompt that was cut is handed back to the input: a
   * rewind is nearly always the first half of "say that differently".
   */
  rewindTo(index: number): RestoreReport | null {
    const session = this.loop.session;
    const cut = session.messages[index] ?? null;
    const prompt = cut !== null ? messageText(cut) : "";
    const report = this.checkpoints?.restoreTo(index) ?? null;
    session.rewindTo(index);
    this.replayTranscript();
    if (prompt) {
      if (cut !== null) this.prompt.rememberImages(messageImages(cut));
      this.prompt.text = prompt;
      this.prompt.buffer.cursor = prompt.length;
    }
    this.runner.requestImmediateRender();
    return report;
  }

  /** Point the store at whichever session the tools are now writing to. */
  private attachCheckpoints(): void {
    const session = this.loop.session;
    this.checkpoints?.attach(session, sessionCheckpointsDir(session.meta.sessionId));
  }

  /**
   * Redraw the transcript from the session: everything the turn produced,
   * reasoning, tool calls and their results - the session file is the whole
   * record of a resumed conversation.
   */
  private replayTranscript(): void {
    const transcript = this.view.transcript;
    transcript.clear();
    this.assistant = null;
    this.thinking = null;
    const messages = this.loop.session.activeMessages();
    const results = new Map<string, ToolResultBlock>();
    for (const message of messages)
      for (const result of toolResults(message)) results.set(result.toolUseId, result);
    for (const message of messages) {
      if (message.role === "user") {
        // Tool results ride on a user-role message with no text of its own;
        // each is drawn with the call that produced it.
        const images = messageImages(message);
        this.prompt.rememberImages(images);
        const text = messageText(message);
        if (text || images.length) transcript.append(new UserMessage(text, images));
        continue;
      }
      for (const block of message.content) this.replayBlock(block, results);
    }
  }

  /** Draw one stored content block the way the live turn drew it. */
  private replayBlock(block: ContentBlock, results: Map<string, ToolResultBlock>): void {
    const transcript = this.view.transcript;
    if (block.type === "text") {
      if (strip(block.text)) transcript.append(new AssistantMessage(block.text));
    } else if (block.type === "thinking") {
      if (strip(block.text)) transcript.append(new ThinkingMessage(block.text));
    } else if (block.type === "tool_use")
      transcript.append(this.replayedCall(block, results.get(block.id) ?? null));
  }

  /**
   * The block one stored tool call should be drawn as. A silent tool is
   * redrawn as whatever drew it the first time, and falls back to its own
   * block when the call failed. The summary, timing and metadata were
   * live-only; the result content stands in.
   */
  private replayedCall(call: ToolUseBlock, result: ToolResultBlock | null): Component {
    const params = { ...(call.input ?? {}) };
    // No result at all is a call cut off by an older HX.
    const printed = result === null ? "" : interruptedOutput(result);
    const interrupted = printed !== null;
    const failed = interrupted || (result?.isError ?? false);
    if (SILENT_TOOLS.has(call.name.toLowerCase()) && !failed)
      return new TodoBlock(params.todos || []);
    return new ToolBlock(
      toolCall({
        name: call.name,
        params,
        cwd: this.settings.cwd,
        output: result !== null && printed === null ? result.content : printed || "",
        isError: failed,
        finished: true,
        interrupted,
      }),
    );
  }

  /** Re-emit the current plan as a transcript block. The session holds no
   * plan of its own, so this says there is none, as the Python build did. */
  showTodos(): void {
    const todos = (this.loop.session as CoreSession & { todos?: unknown[] }).todos;
    if (!todos?.length) {
      this.addNotice("No plan yet.", "warning");
      return;
    }
    this.view.transcript.append(new TodoBlock(todos));
    this.runner.requestRender();
  }

  /** Drop everything queued, returning how much was dropped. */
  clearQueue(): number {
    const dropped = this.queuedTurns.length;
    this.queuedTurns = [];
    this.status.update({ queued: 0 });
    return dropped;
  }

  /** Promote one queued message into the running turn. */
  steerQueued(index = 0): void {
    if (!(index >= 0 && index < this.queuedTurns.length)) return;
    const [turn] = this.queuedTurns.splice(index, 1) as [UserTurn];
    this.loop.steer(turn.text, turn.images);
    this.status.update({ queued: this.queuedTurns.length });
  }

  /** Send the next message typed while the last turn ran: one at a time, in
   * order. Its text is already in the transcript. */
  private drainQueue(): void {
    const turn = this.queuedTurns.shift();
    if (turn === undefined) return;
    this.status.update({ queued: this.queuedTurns.length });
    this.startTurn(turn.text, turn.images);
    this.runner.requestImmediateRender();
  }

  /** Clear the draft; on an empty prompt, interrupt; then exit - announced
   * first, because one keystroke should not end a session. */
  private clearOrExit(): void {
    if (this.prompt.value) {
      this.prompt.clear();
      this.clearArmed = false;
      this.runner.requestImmediateRender();
      return;
    }
    if (this.isBusy) {
      this.interrupt();
      return;
    }
    if (this.clearArmed) {
      this.runner.stop();
      return;
    }
    this.clearArmed = true;
    this.addNotice(`Press ${keymap().text("app.clear")} again to exit.`);
  }

  /**
   * Stop the running turn, both halves, and whatever command or shell line
   * runs beside it. Said in the transcript: a reply cut off mid-sentence
   * otherwise reads as a reply that ended there.
   */
  private interrupt(): void {
    const turnLive = this.isBusy;
    const sideLive = this.side !== null && !this.side.finished;
    if (!turnLive && !sideLive) {
      this.addNotice("Interrupted");
      this.loop.cancel();
      return;
    }
    // Already told to stop and still winding down: pressing again changes
    // nothing, and must not say it did.
    const turnStopping = !turnLive || this.turnStopping;
    const sideStopping = !sideLive || (this.side as SideTask).abort.signal.aborted;
    if (turnStopping && sideStopping) return;
    this.addNotice("Interrupted");
    this.loop.cancel();
    if (turnLive) this.turnStopping = true;
    if (sideLive) (this.side as SideTask).abort.abort();
    // Whoever waits on an approval is released with a refusal.
    for (const prompt of [...this.pending]) prompt.abandon();
  }

  /** Rows a page key moves. Half a screen keeps a line of context. */
  private page(): number {
    return Math.max(1, Math.floor(this.terminalRows() / 2));
  }

  /** Go to the top or the bottom: the viewport in fullscreen, the reading
   * cursor on the normal screen, where the terminal owns the scrollback. */
  private jump(toStart: boolean): void {
    if (this.runner.fullscreen) {
      if (toStart) this.runner.scrollToTop();
      else this.runner.scrollToBottom();
      return;
    }
    const blocks = this.view.transcript.navigable();
    if (!blocks.length) return;
    this.view.transcript.cursor = (toStart ? blocks[0] : blocks.at(-1)) as Component;
    this.runner.requestImmediateRender();
  }

  private terminalRows(): number {
    return this.runner.terminal.size[1];
  }

  private get enterSteers(): boolean {
    return this.settings.tui.enterWhileBusy === "steer";
  }

  get isBusy(): boolean {
    return this.turn !== null;
  }

  // -- input -----------------------------------------------------------------

  private async openCommands(): Promise<void> {
    const chosen = await this.ask(new CommandPalette(this.commands?.all() ?? []));
    if (chosen) await this.runCommand(`/${chosen}`);
  }

  /**
   * Show `component` and wait for it to finish. The component owns the
   * keyboard until its `done` flag is set, then its `result` is returned.
   * Abandoned on the way out, it rejects with an AbortError.
   */
  async ask<T = string>(
    component: Component & { done: boolean; result: T | null },
  ): Promise<T | null> {
    this.sizeOverlay(component);
    // For one that changes without a keystroke - a refresh landing.
    (component as { setRedraw?: (redraw: () => void) => void }).setRedraw?.(() =>
      this.runner.requestRender(),
    );
    this.view.show(component);
    this.runner.requestImmediateRender();
    try {
      await new Promise<void>((resolve, reject) => {
        this.overlayFinished = resolve;
        this.overlayAbandoned = reject;
      });
      return component.result;
    } finally {
      this.overlayFinished = null;
      this.overlayAbandoned = null;
      this.view.dismiss();
      this.runner.requestImmediateRender();
    }
  }

  /**
   * Ask in the transcript and wait for the answer: inline, because the model
   * has just said what it intends to do. Answering leaves the block behind
   * as a record of what was granted.
   */
  askPermission(request: PermissionRequest): Promise<PermissionAnswer> {
    return new Promise<PermissionAnswer>((resolve) => {
      const callId = request.toolUseId ?? null;
      const prompt = new PermissionPrompt(
        request,
        (answer) => {
          const index = this.pending.indexOf(prompt);
          if (index >= 0) this.pending.splice(index, 1);
          this.holdCall(callId, false);
          // Hands the keyboard to whoever is next.
          this.keyboardOwner();
          // Only the last one out hands the indicator back, and only to a
          // turn: a `!` line asks here too, with nothing running.
          if (!this.pending.length && this.turnStarted)
            this.view.dock.working.start((performance.now() - this.turnStarted) / 1000);
          this.runner.requestImmediateRender();
          resolve(answer);
        },
        request.origin ?? null,
        this.settings.cwd,
      );
      this.view.transcript.append(prompt);
      this.pending.push(prompt);
      this.holdCall(callId, true);
      this.keyboardOwner();
      // The turn is blocked on a person, so the indicator stops claiming the
      // tool is running.
      this.view.dock.working.stop();
      this.runner.requestImmediateRender();
    });
  }

  /** Show a call as waiting on its approval, or as running again. */
  private holdCall(toolUseId: string | null, held: boolean): void {
    if (toolUseId === null) return;
    if (held) this.awaiting.add(toolUseId);
    else this.awaiting.delete(toolUseId);
    this.tools.get(toolUseId)?.setAwaiting(held);
  }

  /** The oldest approval still waiting on an answer. */
  private keyboardOwner(): PermissionPrompt | null {
    while (this.pending.length && (this.pending[0] as PermissionPrompt).answered)
      this.pending.shift();
    for (const [position, prompt] of this.pending.entries()) prompt.setWaiting(position > 0);
    return this.pending[0] ?? null;
  }

  private onKey(key: Key): void {
    const keys = keymap();
    const bound = (action: string) => keys.keysFor(action).includes(name);
    let name = key.name;
    // Any other key means the user is still working.
    if (name !== "ctrl+c") this.clearArmed = false;

    // An open approval owns the keyboard: "y" must not be typed into the prompt.
    const pending = this.keyboardOwner();
    if (pending?.handleInput(name, key.data)) {
      // Answering it promotes the next one in the same frame.
      this.keyboardOwner();
      this.runner.requestImmediateRender();
      return;
    }

    // So does an overlay, ahead of every app binding below.
    const showing = this.view.showing;
    if (showing !== null) {
      this.view.handleInput(name, key.data);
      if ((showing as { done?: boolean }).done && this.overlayFinished !== null)
        this.overlayFinished();
      this.runner.requestImmediateRender();
      return;
    }

    if (name === "ctrl+d" && !this.view.dock.prompt.value) {
      this.runner.stop();
      return;
    }
    if (name === "escape" && this.isBusy) {
      this.interrupt();
      return;
    }
    if (name === "ctrl+c") {
      this.clearOrExit();
      return;
    }
    if (name === "shift+tab") {
      this.cycleMode();
      return;
    }
    if (bound("app.transcript.previousPrompt")) {
      this.view.transcript.moveCursor(-1);
      this.runner.requestImmediateRender();
      return;
    }
    if (bound("app.transcript.nextPrompt")) {
      this.view.transcript.moveCursor(1);
      this.runner.requestImmediateRender();
      return;
    }
    if (bound("app.message.copy")) {
      void this.copyCursored();
      return;
    }
    if (bound("app.tools.expand")) {
      if (this.view.transcript.blocks.length) this.expandCursored();
      else this.view.header.toggle();
      return;
    }
    if (bound("app.transcript.top")) {
      this.jump(true);
      return;
    }
    if (bound("app.transcript.bottom")) {
      this.jump(false);
      return;
    }
    // Only in fullscreen, where this renderer owns the viewport.
    if (this.runner.fullscreen && bound("app.transcript.pageUp")) {
      this.runner.scroll(-this.page());
      return;
    }
    if (this.runner.fullscreen && bound("app.transcript.pageDown")) {
      this.runner.scroll(this.page());
      return;
    }
    if (bound("app.suspend")) {
      this.runner.suspend();
      return;
    }
    if (name === "ctrl+t") {
      this.showTodos();
      return;
    }
    if (name === "ctrl+p") {
      void this.openCommands().catch(() => {});
      return;
    }
    if (name === "ctrl+l") {
      // Tracked like a typed `/model`, so an interrupt cancels it too.
      this.startSide(() => this.runCommand("/model"));
      return;
    }
    // Only on the way to the prompt; not under the kitty protocol, which
    // gives shift+enter a code of its own.
    if (name === "text") this.shiftEnter.typed(key.data);
    else if (this.runner.terminal.keyboard !== "kitty" && this.shiftEnter.test(name))
      name = "shift+enter";
    this.view.handleInput(name, key.data);
  }

  /** Run a command or shell line beside the turn, with its own abort. */
  private startSide(work: () => Promise<void>): void {
    const abort = new AbortController();
    const task: SideTask = { abort, finished: false, done: Promise.resolve() };
    this.side = task;
    (task as { done: Promise<void> }).done = work()
      .catch((error) => {
        if (!isAbort(error)) this.addNotice(errorText(error), "error");
      })
      .finally(() => {
        task.finished = true;
        this.runner.requestImmediateRender();
      });
  }

  private submit(text: string, images: readonly ImageBlock[] = []): void {
    if (text.startsWith("!")) {
      this.startSide(() => this.runShellPassthrough(strip(text.slice(1))));
      return;
    }
    if (text.startsWith("/")) {
      this.view.transcript.append(new UserMessage(text));
      this.startSide(() => this.runCommand(text));
      return;
    }
    this.view.transcript.append(new UserMessage(text, images));
    if (this.isBusy) {
      // The setting the placeholder promises: enter steers the running turn.
      if (this.enterSteers) {
        this.loop.steer(text, images);
        return;
      }
      this.queue(text, images);
      return;
    }
    this.startTurn(text, images);
  }

  /** Hold a message until the running turn ends: held rather than refused,
   * because dropping it loses the message. */
  private queue(text: string, images: readonly ImageBlock[] = []): void {
    this.queuedTurns.push({ text, images: [...images] });
    this.status.update({ queued: this.queuedTurns.length });
  }

  /** `!command` - a shell command without a model turn, through the same
   * permission engine and sandbox as a model-issued one. */
  async runShellPassthrough(command: string): Promise<void> {
    if (!command) return;
    const signal = this.side?.abort.signal ?? new AbortController().signal;
    this.view.transcript.append(new UserMessage(`!${command}`));
    this.runner.requestImmediateRender();

    if (!this.loop.tools.has("Bash")) {
      this.addNotice("No shell is attached to this session.", "error");
      return;
    }
    const allowed = await this.permitShell(command);
    if (signal.aborted) return;
    if (!allowed) {
      this.addNotice(`Refused: !${command}`, "warning");
      return;
    }
    const block = new ToolBlock(
      toolCall({ name: "Bash", params: { command }, cwd: this.settings.cwd }),
    );
    this.view.transcript.append(block);
    this.runner.requestImmediateRender();

    const result = await this.loop.tools.call(
      "Bash",
      { command },
      {
        cwd: this.settings.cwd,
        sessionId: this.loop.session.meta.sessionId,
        toolUseId: `shell_${Math.floor(Math.random() * 2 ** 48).toString(16)}`,
        reads: this.loop.reads,
        settings: this.settings,
        emitProgress: (chunk) => {
          block.appendOutput(chunk);
          this.runner.requestRender();
        },
        signal,
      },
    );
    if (signal.aborted) return;
    block.update({
      finished: true,
      isError: result.isError,
      summary: result.summary || "",
      output: result.content || "",
    });
    this.runner.requestImmediateRender();
  }

  private async permitShell(command: string): Promise<boolean> {
    if (this.loop.permissions === null) return true;
    const [allowed] = await this.loop.permissions.request({
      toolName: "Bash",
      specifier: command,
      params: { command },
      mutating: true,
      description: `Bash(${command})`,
      detail: command,
      detailKind: "command",
    });
    return allowed;
  }

  cycleMode(): void {
    const current = PERMISSION_MODES.indexOf(this.mode as PermissionMode);
    this.setMode(PERMISSION_MODES[(current + 1) % PERMISSION_MODES.length] as PermissionMode);
  }

  /** Copy the cursored message, or the last answer when none is cursored. */
  async copyCursored(): Promise<void> {
    const text = this.view.transcript.cursoredText();
    if (!text) {
      this.addNotice("Nothing to copy yet.", "warning");
      return;
    }
    let used: string;
    try {
      used = await this.copy(text);
    } catch (error) {
      this.addNotice(`Could not copy: ${errorText(error)}`, "error");
      return;
    }
    this.addNotice(`Copied to the clipboard (${used}).`, "success");
  }

  /** Expand the cursored block, or every foldable one when none is. Reasoning
   * folds with the tool calls: both are the detail under an answer. */
  expandCursored(): void {
    const cursor = this.view.transcript.cursor as { toggle?: () => void } | null;
    if (cursor !== null && typeof cursor.toggle === "function") cursor.toggle();
    else
      for (const block of this.view.transcript.blocks)
        if (block instanceof ToolBlock || block instanceof ThinkingMessage) block.toggle();
    this.runner.requestImmediateRender();
  }

  private async runCommand(line: string): Promise<void> {
    try {
      await this.commands?.dispatch(this.commandContext as CommandContext, line);
    } catch (error) {
      if (isAbort(error)) throw error;
      this.addNotice(errorText(error), "error");
    } finally {
      this.runner.requestImmediateRender();
    }
  }

  /**
   * Alt+Enter: put this into the running turn now. With nothing in flight it
   * is a submission; with nothing typed it promotes the head of the queue.
   * With `tui.enterWhileBusy` set to steer, this key queues instead.
   */
  private steer(text: string, images: readonly ImageBlock[] = []): void {
    if (!this.isBusy) {
      if (text) this.submit(text, images);
      return;
    }
    if (!text) {
      this.steerQueued();
      return;
    }
    this.view.transcript.append(new UserMessage(text, images));
    if (this.enterSteers) {
      this.queue(text, images);
      return;
    }
    this.loop.steer(text, images);
  }

  /** Load images for the prompt: the named files, or the clipboard's. */
  private requestImages(paths: string[] | null): void {
    this.detach(
      this.attachImages(paths).catch((error) => {
        this.addNotice(`Could not attach the image: ${errorText(error)}`, "error");
        this.runner.requestImmediateRender();
      }),
    );
  }

  /** Read, normalise and attach. A model that takes no images is warned
   * about here, when the picture is attached. */
  private async attachImages(paths: string[] | null): Promise<void> {
    const images: ImageBlock[] = [];
    if (paths === null) {
      let copied: Awaited<ReturnType<typeof readImage>>;
      try {
        copied = await readImage();
      } catch (error) {
        if (!(error instanceof ClipboardError)) throw error;
        this.addNotice(`Could not paste an image: ${error.message}`, "warning");
        return;
      }
      if (copied === null) {
        this.addNotice("No image on the clipboard.", "warning");
        return;
      }
      try {
        images.push(await loadImage(copied.data, copied.name || ""));
      } catch (error) {
        if (!(error instanceof ImageError)) throw error;
        this.addNotice(`Could not paste the image: ${error.message}`, "error");
        return;
      }
    } else {
      for (const path of paths) {
        try {
          images.push(await loadImageFile(path));
        } catch (error) {
          if (!(error instanceof ImageError)) throw error;
          this.addNotice(`Could not attach ${basename(path)}: ${error.message}`, "error");
        }
      }
    }
    for (const image of images) this.prompt.attachImage(image);
    const info = this.loop.modelInfo;
    if (images.length && (info === null || !info.supportsImages)) {
      const name = info !== null ? info.name : this.loop.model;
      this.addNotice(
        `${name} does not accept images - it will be told one was attached, not shown it. /model to pick one that does.`,
        "warning",
      );
    }
    this.runner.requestImmediateRender();
  }

  private startTurn(text: string, images: readonly ImageBlock[] = []): void {
    this.turnStopping = false;
    const turn = this.runTurn(text, images);
    this.turn = turn;
    void turn.finally(() => {
      if (this.turn === turn) this.turn = null;
      // Only on a turn that ended on its own: an interrupt stops the agent
      // without draining the queue into a new turn.
      if (!this.turnStopping) this.drainQueue();
    });
  }

  /** Working from here to the end of the turn, not from one model call to
   * the next: the tools between two calls are the turn too. */
  private async runTurn(text: string, images: readonly ImageBlock[]): Promise<void> {
    this.turnStarted = performance.now();
    this.view.dock.working.start();
    this.prompt.setRunning(true, this.enterSteers);
    this.runner.requestImmediateRender();
    try {
      await this.loop.run(text, images);
    } catch (error) {
      if (!(isAbort(error) || this.turnStopping)) this.addNotice(errorText(error), "error");
    } finally {
      this.turnStarted = 0;
      this.view.dock.working.stop();
      this.prompt.setRunning(false);
      this.runner.requestImmediateRender();
    }
    // The messages are kept rather than dropped, so what is held is named.
    if (this.turnStopping && this.queuedTurns.length)
      this.addNotice(
        `${this.queuedTurns.length} message(s) still queued · /queue to see them`,
        "warning",
      );
  }

  // -- events ----------------------------------------------------------------

  private addNotice(text: string, level = "info"): void {
    this.view.transcript.append(new Notice(text, level));
    this.runner.requestRender();
  }

  private async consumeEvents(): Promise<void> {
    const transcript = this.view.transcript;
    const status = this.view.dock.status;
    for await (const event of this.bus.subscribe()) {
      switch (event.kind) {
        case "TurnStarted":
          // One model call, not the user's turn. A new call starts new blocks.
          this.assistant = null;
          this.thinking = null;
          break;
        case "TextDelta":
          if (this.assistant === null) this.assistant = transcript.append(new AssistantMessage());
          this.assistant.append(event.text);
          break;
        case "ThinkingDelta":
          if (this.thinking === null) this.thinking = transcript.append(new ThinkingMessage());
          this.thinking.append(event.text);
          break;
        case "ToolCallStarted": {
          const started = new ToolBlock(
            toolCall({
              name: event.name,
              params: { ...(event.input ?? {}) },
              cwd: this.settings.cwd,
            }),
          );
          this.tools.set(event.toolUseId, started);
          started.setAwaiting(this.awaiting.has(event.toolUseId));
          // A silent tool is held back rather than dropped: if it fails there
          // is nothing else to draw, and the block is appended then.
          if (SILENT_TOOLS.has(event.name.toLowerCase())) this.silent.add(event.toolUseId);
          else transcript.append(started);
          this.assistant = null;
          break;
        }
        case "ToolCallProgress":
          this.tools.get(event.toolUseId)?.appendOutput(event.chunk);
          break;
        case "ToolCallFinished": {
          const finished = this.tools.get(event.toolUseId);
          this.tools.delete(event.toolUseId);
          const held = this.silent.delete(event.toolUseId);
          if (finished !== undefined) {
            // A success's output is what the tool streamed; a failure's
            // detail is the reason, and replaces it.
            finished.update({
              finished: true,
              isError: event.isError,
              summary: event.summary || "",
              output: event.detail || finished.call.output,
              metadata: event.metadata,
              durationMs: event.durationMs || 0,
              interrupted: event.interrupted,
            });
            if (held && event.isError) transcript.append(finished);
          }
          break;
        }
        case "UsageUpdated":
          status.setTokens(event.inputTokens, event.outputTokens);
          status.setContext(event.contextTokens, event.contextWindow);
          status.setCache(
            event.cacheReadTokens,
            event.cacheWriteTokens,
            this.loop.session.usage.cacheHitRate,
          );
          status.update({
            costUsd: event.costUsd,
            latencyMs: this.loop.session.usage.lastLatencyMs,
          });
          break;
        case "PlanUsageUpdated":
          status.setPlan(event.route, event.windows);
          break;
        case "TodosUpdated":
          transcript.append(new TodoBlock(event.todos));
          break;
        case "CompactionStarted":
          this.addNotice(`Compacting (${event.reason})…`);
          break;
        case "CompactionFinished":
          // Reported whatever it saved: the cache is discarded either way.
          this.addNotice(
            `Compacted ${formatTokens(event.tokensBefore)} → ${formatTokens(event.tokensAfter)} tokens. The cached conversation is discarded, so the next turn re-reads it at full price.`,
          );
          status.setContext(this.loop.session.usage.contextTokens, status.contextWindow);
          break;
        case "ErrorRaised":
          this.addNotice(event.message, "error");
          break;
        default:
          break;
      }
      this.runner.requestRender();
    }
  }

  private refreshStatus(): void {
    const status = this.view.dock.status;
    status.setLocation(tilde(this.settings.cwd), this.branch.poll());
    status.setMode(this.settings.permissions.mode, this.sandboxActive, this.sandboxBackend);
    const model = this.loop.modelInfo;
    if (model !== null) {
      const modelId = model.id || "";
      status.setModel(modelId, {
        subscription: model.isSubscription,
        route: model.providerId || "",
      });
      status.setContext(0, model.contextWindow || 0);
      // The saved effort is in force from the first turn.
      if (this.models !== null) status.setEffort(this.models.displayedEffort(modelId));
    }
  }
}

function invalidateAll(component: Component): void {
  (component as { invalidate?: () => void }).invalidate?.();
  for (const child of (component as { children?: Component[] }).children ?? [])
    invalidateAll(child);
}

/**
 * Run one interactive session to completion. Returns the id of a session to
 * relaunch into, when `/resume` picked one from another directory.
 */
export async function runSession(
  loop: AgentLoop,
  bus: EventBus,
  settings: Settings,
  extra: SessionExtras = {},
): Promise<string | null> {
  if (process.platform === "win32")
    throw new Error(
      "HX's terminal interface needs a POSIX terminal. On Windows, run it under WSL.",
    );
  const session = new HXSession(loop, bus, settings, extra);
  await session.run();
  return session.relaunch;
}
