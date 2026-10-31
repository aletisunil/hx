/**
 * HX's prompt: the editor, plus everything the app expects of it - history,
 * emacs keys, the kill ring, image and paste tokens, and completion.
 */

import { statSync } from "node:fs";
import { pastedImagePaths } from "../../core/images.ts";
import type { ImageBlock } from "../../core/messages.ts";
import { glob, parts as pathParts } from "../../glob.ts";
import { keymap } from "../../keys.ts";
import { isDir, relativeTo } from "../../pathlib.ts";
import { isSpace, purePath, pyLen, splitLines, strip } from "../../py.ts";
import { Editor } from "../../term/editor.ts";
import { plainText } from "../../term/sanitize.ts";
import { stepBack, stepForward } from "../../term/word_nav.ts";
import { columns } from "../format.ts";
import { filterItems } from "../fuzzy.ts";
import { CURSOR, GUTTER } from "../glyphs.ts";
import { KillRing } from "../killring.ts";
import { LIST_VISIBLE } from "../limits.ts";
import { fg, rule } from "../paint.ts";

export const MAX_HISTORY = 200;

/** What an attached image looks like in the draft. The number is the image's
 * for the whole session, so a recalled message still means the same picture. */
export const IMAGE_TOKEN = /\[Image #(\d+)\]/g;
const IMAGE_LABEL = /^\[Image #(\d+)\]$/;

/** What a large paste looks like in the draft. It stands for the text, which
 * goes out in its place on submit. */
export const PASTE_TOKEN = /\[Pasted text #(\d+) (?:\+\d+ lines|\d+ chars)\]/g;

/** A paste over either limit is collapsed to a paste token. */
export const PASTE_COLLAPSE_LINES = 10;
export const PASTE_COLLAPSE_CHARS = 1000;

const CSI_U_CONTROL = /\x1b\[(\d+);5u|\x1b\[27;5;(\d+)~/g;

/**
 * Pasted text as the user copied it: real newlines, nothing that can act.
 * Terminals send a pasted line break as a carriage return; inside tmux with
 * extended keys, control bytes can arrive re-encoded as `ctrl+<letter>` key
 * sequences, so those are decoded back to the byte before sanitizing.
 */
export function cleanPaste(data: string): string {
  let text = data;
  if (text.includes("\x1b")) {
    text = text.replace(CSI_U_CONTROL, (whole, a: string | undefined, b: string | undefined) => {
      const code = Number(a ?? b);
      if (code >= 0x61 && code <= 0x7a) return String.fromCharCode(code - 0x60);
      if (code >= 0x40 && code <= 0x5f) return String.fromCharCode(code - 0x40);
      return whole;
    });
  }
  return plainText(text.replaceAll("\r\n", "\n").replaceAll("\r", "\n"));
}

/** One completion row: what gets inserted, and what the row says. */
export interface Candidate {
  readonly value: string;
  readonly label: string;
  readonly detail: string;
}

/** An open completion session over the prompt's current token. */
export class Completion {
  constructor(
    /** Offset in the prompt text where the replaced token starts. */
    readonly start: number,
    /** Text inserted before the value: `/` or `@`. */
    readonly prefix: string,
    readonly candidates: Candidate[] = [],
    public index = 0,
  ) {}

  get current(): Candidate | null {
    if (!this.candidates.length) return null;
    return this.candidates[this.index % this.candidates.length] as Candidate;
  }

  move(delta: number): void {
    if (this.candidates.length) {
      const length = this.candidates.length;
      this.index = (((this.index + delta) % length) + length) % length;
    }
  }
}

/** `@`-triggered path completion, ranked by recency. */
export class FileCompleter {
  static readonly IGNORED = new Set([
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
  ]);
  static readonly LIMIT = 20;

  constructor(readonly cwd: string) {}

  complete(prefix: string): string[] {
    const pattern = prefix ? `${prefix}*` : "*";
    // `pathlib` refuses an absolute pattern.
    if (pattern.startsWith("/")) return [];
    let candidates: string[];
    try {
      candidates = glob(this.cwd, pattern)
        .map(purePath)
        .filter(
          (path) =>
            !pathParts(path).some(
              (part) => FileCompleter.IGNORED.has(part) || part.startsWith("."),
            ),
        );
    } catch {
      return [];
    }
    const mtime = (path: string) => {
      try {
        return statSync(path).mtimeMs;
      } catch {
        return 0;
      }
    };
    const times = new Map(candidates.map((path) => [path, mtime(path)]));
    candidates.sort((a, b) => (times.get(b) as number) - (times.get(a) as number));
    return candidates
      .slice(0, FileCompleter.LIMIT)
      .map((path) => (relativeTo(path, this.cwd) ?? path) + (isDir(path) ? "/" : ""));
  }
}

/** What the empty prompt says, and what Enter will do to it. */
export function placeholderText(running = false, enterSteers = false): string {
  if (!running) return "Ask HX…  (/ for commands)";
  const steer = keymap().primary("tui.input.steer");
  if (enterSteers) return `Ask HX…  (Enter steers · ${steer} queues)`;
  return `Ask HX…  (Enter queues · ${steer} steers)`;
}

/** What completion needs from the command registry. */
export interface CommandSource {
  all(): Iterable<{ readonly name: string; readonly summary: string }>;
}

export interface PromptOptions {
  commands?: CommandSource | null;
  rowsAvailable?: (() => number) | null;
  onSubmit?: ((text: string, images: ImageBlock[]) => void) | null;
  onSteer?: ((text: string, images: ImageBlock[]) => void) | null;
  /** Called with image paths a paste named, or `null` for whatever image is
   * on the clipboard. The app loads it and answers with `attachImage`. */
  onImageRequest?: ((paths: string[] | null) => void) | null;
}

/** The prompt, with HX's keys, history, kill ring and completion. */
export class Prompt extends Editor {
  commands: CommandSource | null;
  readonly completer: FileCompleter;
  readonly killRing = new KillRing();
  onSubmit: PromptOptions["onSubmit"];
  onSteer: PromptOptions["onSteer"];
  onImageRequest: PromptOptions["onImageRequest"];
  private readonly images = new Map<number, ImageBlock>();
  private lastImage = 0;
  /** Every collapsed paste this session. Kept after submit, so a recalled
   * draft still expands to what it stood for. */
  private readonly pastes = new Map<number, string>();
  private lastPaste = 0;

  completion: Completion | null = null;
  private completionDismissed = false;
  private history: string[] = [];
  private historyIndex: number | null = null;
  private draft = "";
  private lastYank: [number, number, string] | null = null;
  private running = false;
  private enterSteers = false;

  constructor(
    readonly cwd: string,
    options: PromptOptions = {},
  ) {
    super(1, rule("border_muted"), options.rowsAvailable ?? (() => 24));
    this.commands = options.commands ?? null;
    this.completer = new FileCompleter(cwd);
    this.onSubmit = options.onSubmit ?? null;
    this.onSteer = options.onSteer ?? null;
    this.onImageRequest = options.onImageRequest ?? null;
    this.placeholder = placeholderText(false, false);
  }

  get value(): string {
    return this.text;
  }

  /** Insert an image's token at the cursor. Returns the image as labelled.
   * The token is the attachment: deleting it drops the image. */
  attachImage(image: ImageBlock): ImageBlock {
    this.lastImage++;
    const number = this.lastImage;
    const labelled: ImageBlock = {
      ...image,
      label: `Image #${number}`,
      source: image.source || image.label,
    };
    this.images.set(number, labelled);
    let token = `[${labelled.label}]`;
    const before = this.text.slice(0, this.buffer.cursor);
    const after = this.text.slice(this.buffer.cursor);
    if (before && !isSpace(before.at(-1) as string)) token = ` ${token}`;
    if (!after || !isSpace(after[0] as string)) token += " ";
    this.insert(token);
    this.syncCompletion();
    return labelled;
  }

  /** `text` with every paste token replaced by the text it stands for, in
   * one pass. */
  expandPastes(text: string): string {
    if (!text.includes("[Pasted text #")) return text;
    return text.replace(
      PASTE_TOKEN,
      (whole, number: string) => this.pastes.get(Number(number)) ?? whole,
    );
  }

  /** Insert a paste - collapsed to a token when it is large. */
  private paste(data: string): void {
    const text = cleanPaste(data);
    // splitlines, so the newline most copied text ends with is not a line.
    const lines = splitLines(text).length;
    const chars = pyLen(text);
    if (lines <= PASTE_COLLAPSE_LINES && chars <= PASTE_COLLAPSE_CHARS) {
      this.insert(text);
      return;
    }
    this.lastPaste++;
    const number = this.lastPaste;
    this.pastes.set(number, text);
    const size = lines > PASTE_COLLAPSE_LINES ? `+${lines} lines` : `${chars} chars`;
    // Not padded: the token expands to exactly what was pasted.
    this.insert(`[Pasted text #${number} ${size}]`);
  }

  /** The images whose tokens `text` holds, in the order it mentions them. */
  imagesIn(text: string): ImageBlock[] {
    const numbers = new Set([...text.matchAll(IMAGE_TOKEN)].map((match) => Number(match[1])));
    return [...numbers]
      .filter((n) => this.images.has(n))
      .map((n) => this.images.get(n) as ImageBlock);
  }

  /** Take back images sent from here before, so their tokens resolve again
   * and the next image is numbered past them. */
  rememberImages(images: readonly ImageBlock[]): void {
    for (const image of images) {
      const match = IMAGE_LABEL.exec(`[${image.label}]`);
      if (match === null) continue;
      const number = Number(match[1]);
      this.images.set(number, image);
      this.lastImage = Math.max(this.lastImage, number);
    }
  }

  setRunning(running: boolean, enterSteers = false): void {
    this.running = running;
    this.enterSteers = enterSteers;
    this.placeholder = placeholderText(running, enterSteers);
    this.invalidate();
  }

  get isRunning(): boolean {
    return this.running;
  }

  get steersOnEnter(): boolean {
    return this.enterSteers;
  }

  /** Recolour the frame - the thinking level, or bash mode. */
  setTint(color: ((text: string) => string) | null): void {
    this.topRule.setColor(color);
    this.bottomRule.setColor(color);
    this.invalidate();
  }

  // -- keys ----------------------------------------------------------------

  handleInput(key: string, data: string): boolean {
    const keys = keymap();
    const bound = (action: string) => keys.keysFor(action).includes(key);

    if (this.completionKey(key, bound)) return true;

    if (bound("tui.input.submit")) {
      if (this.backslashNewline()) return true;
      this.submit();
      return true;
    }
    if (bound("tui.input.steer")) {
      this.steer();
      return true;
    }
    if (bound("tui.input.newLine")) {
      this.insert("\n");
      this.syncCompletion();
      return true;
    }
    if (bound("tui.input.complete")) {
      this.complete();
      return true;
    }
    if (bound("tui.input.pasteImage")) {
      this.requestImage(null);
      return true;
    }

    // History at the edges of the buffer, so the arrows still move the
    // cursor everywhere else in a multi-line draft.
    if (key === "up" && this.buffer.row === 0) {
      this.historyPrev();
      return true;
    }
    if (key === "down" && this.buffer.row === this.buffer.lineCount - 1) {
      this.historyNext();
      return true;
    }
    return this.editingKey(key, data, bound);
  }

  /**
   * `\` then Enter: a newline, for terminals that cannot send shift+enter. A
   * backslash escapes itself, so `\\` then Enter sends a single trailing `\`.
   * Returns whether the key was taken as a newline.
   */
  private backslashNewline(): boolean {
    const cursor = this.buffer.cursor;
    const before = this.text.slice(0, cursor);
    const backslashes = before.length - before.replace(/\\+$/, "").length;
    if (backslashes === 0) return false;
    if (backslashes % 2 === 0) {
      // Escaped: drop the escape, and let the Enter send.
      this.buffer.deleteRange(cursor - 1, cursor);
      return false;
    }
    this.undoStack.record(this.text, cursor);
    this.buffer.deleteRange(cursor - 1, cursor);
    this.buffer.insert("\n");
    this.invalidate();
    this.syncCompletion();
    return true;
  }

  private editingKey(key: string, data: string, bound: (action: string) => boolean): boolean {
    const buffer = this.buffer;

    if (key === "paste" && this.onImageRequest) {
      // An empty paste is a picture on the clipboard; a dragged-in file
      // arrives as its path. Either is an image to attach.
      if (!data) {
        this.requestImage(null);
        return true;
      }
      const paths = pastedImagePaths(cleanPaste(data));
      if (paths?.length) {
        this.requestImage(paths);
        return true;
      }
    }
    // Sanitized here rather than on submit: the draft is drawn as it is
    // typed, so an escape in it would act before anyone pressed enter.
    if (key === "paste") {
      this.paste(data);
      this.syncCompletion();
      return true;
    }
    if (key === "text") {
      this.insert(plainText(data), true);
      this.syncCompletion();
      return true;
    }
    if (key === "backspace") {
      this.undoStack.record(this.text, buffer.cursor, true);
      if (buffer.cursor > 0)
        buffer.deleteRange(
          ...this.widened(buffer.cursor - stepBack(this.text, buffer.cursor), buffer.cursor),
        );
      this.invalidate();
      this.syncCompletion();
      return true;
    }
    if (key === "delete") {
      this.undoStack.record(this.text, buffer.cursor);
      if (buffer.cursor < this.text.length)
        buffer.deleteRange(
          ...this.widened(buffer.cursor, buffer.cursor + stepForward(this.text, buffer.cursor)),
        );
      this.invalidate();
      this.syncCompletion();
      return true;
    }

    if (key === "left" || bound("tui.editor.cursorLeft")) {
      buffer.left();
      this.snapCursor(false);
    } else if (key === "right" || bound("tui.editor.cursorRight")) {
      buffer.right();
      this.snapCursor(true);
    } else if (bound("tui.editor.cursorWordLeft")) {
      buffer.cursor = buffer.wordLeft();
      this.snapCursor(false);
    } else if (bound("tui.editor.cursorWordRight")) {
      buffer.cursor = buffer.wordRight();
      this.snapCursor(true);
    } else if (key === "home" || key === "ctrl+a") buffer.home();
    else if (key === "end" || key === "ctrl+e") buffer.end();
    else if (key === "up") {
      buffer.up();
      this.snapCursor(false);
    } else if (key === "down") {
      buffer.down();
      this.snapCursor(true);
    } else if (key === "ctrl+w" || key === "alt+backspace") this.killTo(buffer.wordLeft());
    else if (bound("tui.editor.deleteWordForward")) this.killTo(buffer.wordRight());
    else if (key === "ctrl+u") this.killTo(buffer.lineStart());
    else if (key === "ctrl+k") this.killTo(buffer.lineEnd());
    else if (bound("tui.editor.yank")) this.yank();
    else if (bound("tui.editor.yankPop")) this.yankPop();
    else if (bound("tui.editor.undo")) this.undo();
    else if (bound("tui.editor.redo")) this.redo();
    else return false;

    this.invalidate();
    this.syncCompletion();
    return true;
  }

  /** `[start, end)` grown to swallow any token it cuts into: a token goes as
   * a whole, whichever key deletes it. */
  private widened(a: number, b: number): [number, number] {
    let [start, end] = a <= b ? [a, b] : [b, a];
    for (const [tokenStart, tokenEnd] of this.tokenSpans()) {
      if (tokenStart < end && tokenEnd > start) {
        start = Math.min(start, tokenStart);
        end = Math.max(end, tokenEnd);
      }
    }
    return [start, end];
  }

  private tokenSpans(): [number, number][] {
    const text = this.text;
    const spans: [number, number][] = [...text.matchAll(IMAGE_TOKEN)].map((m) => [
      m.index,
      m.index + m[0].length,
    ]);
    for (const m of text.matchAll(PASTE_TOKEN))
      if (this.pastes.has(Number(m[1]))) spans.push([m.index, m.index + m[0].length]);
    return spans;
  }

  /** Keep the cursor out of the middle of a token: it steps over one as if
   * it were one character, landing on the side it was heading for. */
  private snapCursor(forward: boolean): void {
    const cursor = this.buffer.cursor;
    for (const [start, end] of this.tokenSpans()) {
      if (start < cursor && cursor < end) {
        this.buffer.cursor = forward ? end : start;
        return;
      }
    }
  }

  private requestImage(paths: string[] | null): void {
    this.onImageRequest?.(paths);
  }

  // -- kill ring -----------------------------------------------------------

  /** Delete between the cursor and `target`, keeping the text on the ring. */
  private killTo(target: number): void {
    const killed = this.killRange(...this.widened(this.buffer.cursor, target));
    if (!killed) return;
    this.killRing.kill(killed);
    this.lastYank = null;
  }

  private yank(): void {
    const text = this.killRing.yank();
    if (text === null) return;
    const start = this.buffer.cursor;
    this.insert(text);
    this.lastYank = [start, this.buffer.cursor, text];
  }

  /** Replace the text just yanked with the next kill down the ring. Only
   * valid immediately after a yank, and re-checked against the buffer. */
  private yankPop(): void {
    const current = this.currentYank();
    if (current === null) {
      this.lastYank = null;
      return;
    }
    const text = this.killRing.yankPop();
    if (text === null) return;
    const [start, end] = current;
    this.buffer.deleteRange(start, end);
    this.buffer.cursor = start;
    this.insert(text);
    this.lastYank = [start, this.buffer.cursor, text];
  }

  private currentYank(): [number, number, string] | null {
    if (this.lastYank === null) return null;
    const [start, end, text] = this.lastYank;
    if (this.buffer.cursor !== end || this.buffer.getRange(start, end) !== text) return null;
    return this.lastYank;
  }

  // -- submit and history --------------------------------------------------

  private submit(): void {
    this.closeCompletion();
    const text = strip(this.text);
    if (!text) return;
    // History keeps the draft as it was seen, tokens and all; what goes out
    // is the text the tokens stand for.
    this.remember(text);
    this.onSubmit?.(strip(this.expandPastes(text)), this.imagesIn(text));
  }

  /** Steer the draft, or - with nothing typed - whatever is queued. */
  private steer(): void {
    this.closeCompletion();
    const text = strip(this.text);
    if (text) this.remember(text);
    this.onSteer?.(strip(this.expandPastes(text)), this.imagesIn(text));
  }

  private remember(text: string): void {
    this.history.push(text);
    if (this.history.length > MAX_HISTORY)
      this.history.splice(0, this.history.length - MAX_HISTORY);
    this.historyIndex = null;
    this.draft = "";
    this.clear();
  }

  historyPrev(): void {
    if (!this.history.length) return;
    if (this.historyIndex === null) {
      this.draft = this.text;
      this.historyIndex = this.history.length;
    }
    this.historyIndex = Math.max(0, this.historyIndex - 1);
    this.text = this.history[this.historyIndex] as string;
  }

  historyNext(): void {
    if (this.historyIndex === null) return;
    this.historyIndex++;
    if (this.historyIndex >= this.history.length) {
      this.historyIndex = null;
      this.text = this.draft;
    } else this.text = this.history[this.historyIndex] as string;
  }

  // -- completion ----------------------------------------------------------

  /** Keys the open completion owns. */
  private completionKey(key: string, bound: (action: string) => boolean): boolean {
    const completion = this.completion;
    if (completion === null) return false;
    if (key === "up" || key === "down") {
      completion.move(key === "up" ? -1 : 1);
      this.syncCompletions();
      return true;
    }
    if (bound("tui.input.submit")) {
      // Once the prompt already holds the highlighted completion, Enter
      // submits: exact commands such as /clear need no second Enter.
      const candidate = completion.current;
      if (candidate !== null && this.text === completion.prefix + candidate.value) this.submit();
      else this.acceptCompletion();
      return true;
    }
    if (bound("tui.input.complete")) {
      this.acceptCompletion();
      return true;
    }
    if (bound("app.interrupt")) {
      this.closeCompletion();
      return true;
    }
    return false;
  }

  /** Open the completion for the token under the cursor, or advance it. */
  complete(): void {
    if (this.completion !== null) {
      this.completion.move(1);
      this.syncCompletions();
      return;
    }
    this.completionDismissed = false;
    this.completion = this.buildCompletion();
    this.syncCompletions();
  }

  /** Open, refilter or close for whatever is under the cursor. */
  private syncCompletion(): void {
    const built = this.buildCompletion();
    if (built === null) {
      this.completionDismissed = false;
      this.completion = null;
    } else if (this.completionDismissed) this.completion = null;
    else {
      // Keep the highlighted row where it was while the list narrows.
      if (this.completion !== null && this.completion.start === built.start)
        built.index = Math.min(this.completion.index, Math.max(0, built.candidates.length - 1));
      this.completion = built;
    }
    this.syncCompletions();
  }

  closeCompletion(): void {
    this.completionDismissed = this.completion !== null;
    this.completion = null;
    this.syncCompletions();
  }

  acceptCompletion(): void {
    const completion = this.completion;
    const candidate = completion?.current ?? null;
    if (completion === null || candidate === null) return;
    // Splice, never truncate: everything after the cursor is the rest of the
    // user's draft.
    const text = this.text;
    const replacement = completion.prefix + candidate.value;
    const end = completion.start + replacement.length;
    this.buffer.set(
      text.slice(0, completion.start) + replacement + text.slice(this.buffer.cursor),
      end,
    );
    this.completion = null;
    this.completionDismissed = false;
    this.invalidate();
    this.syncCompletions();
  }

  private buildCompletion(): Completion | null {
    const text = this.text.slice(0, this.buffer.cursor);
    if (text.startsWith("/") && !text.includes("\n") && !text.includes(" "))
      return this.commandCompletion(text);
    const at = text.lastIndexOf("@");
    if (at !== -1 && !Array.from(text.slice(at + 1)).some(isSpace))
      return this.pathCompletion(text, at);
    return null;
  }

  private commandCompletion(text: string): Completion | null {
    if (this.commands === null) return null;
    // Name only: matching summaries turns "/co" into half the list.
    const matches = filterItems(this.commands.all(), text.slice(1), (command) => command.name);
    const candidates = matches.map((command) => ({
      value: command.name,
      label: `/${command.name}`,
      detail: command.summary,
    }));
    return candidates.length ? new Completion(0, "/", candidates) : null;
  }

  private pathCompletion(text: string, at: number): Completion | null {
    // A filename is bytes off a disk, not something HX wrote.
    const candidates = this.completer
      .complete(text.slice(at + 1))
      .map((path) => plainText(path))
      .map((path) => ({ value: path, label: path, detail: "" }));
    return candidates.length ? new Completion(at, "@", candidates) : null;
  }

  /**
   * Draw the completion list inside the editor's own line array, above the
   * prompt: a list below would shove the prompt and the status bar up by one
   * row per match as the query narrows.
   */
  private syncCompletions(): void {
    const completion = this.completion;
    if (completion === null || !completion.candidates.length) {
      this.setCompletions([]);
      return;
    }
    const total = completion.candidates.length;
    let start = 0;
    if (total > LIST_VISIBLE) {
      const half = Math.floor(LIST_VISIBLE / 2);
      start = Math.max(0, Math.min(completion.index - half, total - LIST_VISIBLE));
    }
    const window = completion.candidates.slice(start, start + LIST_VISIBLE);
    const rows = columns(window.map((candidate) => [candidate.label, candidate.detail]));
    const lines = rows.map((row, offset) => {
      const chosen = start + offset === completion.index;
      const marker = chosen ? fg("accent", CURSOR) : GUTTER;
      return marker + fg(chosen ? "accent" : "text", row);
    });
    if (total > LIST_VISIBLE)
      lines.push(GUTTER + fg("muted", `(${completion.index + 1}/${total})`));
    this.setCompletions(lines);
  }
}
