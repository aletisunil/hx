/**
 * The system clipboard, from inside a terminal: copying text out, and reading
 * an image in. Tried in a deliberate order, as pi does:
 *
 * 1. The platform's own clipboard command first, so the terminal cannot race
 *    the write. On Linux the tool depends on the session, chosen from the
 *    environment.
 * 2. OSC 52, which asks the terminal to do it - always over SSH, where the
 *    platform command would write to the *remote* clipboard, and as the
 *    fallback when step 1 found nothing.
 */

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isImagePath, MAX_SOURCE_BYTES } from "../core/images.ts";
import { basename, join } from "../pathlib.ts";
import { formatFixed, splitLines, splitWhitespace, strip } from "../py.ts";

/** Terminals cut the escape sequence off past this; a refusal beats a
 * silently truncated clipboard. */
export const MAX_OSC52_ENCODED = 100_000;
const TIMEOUT_MS = 5000;

/** Nothing available on this machine could take the text. */
export class ClipboardError extends Error {
  override name = "ClipboardError";
}

export const isRemoteSession = (env: NodeJS.ProcessEnv = process.env) =>
  ["SSH_CONNECTION", "SSH_CLIENT", "MOSH_CONNECTION"].some((name) => env[name]);

/** Clipboard commands to try, best first, for this platform and session. */
export function commandsFor(platform: string, env: NodeJS.ProcessEnv = process.env): string[][] {
  if (platform === "darwin") return [["pbcopy"]];
  if (platform === "win32") return [["clip"]];
  const candidates: string[][] = [];
  if (env.TERMUX_VERSION) candidates.push(["termux-clipboard-set"]);
  if (env.WAYLAND_DISPLAY) candidates.push(["wl-copy"]);
  if (env.DISPLAY)
    candidates.push(["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]);
  return candidates;
}

/** Run `command`, feeding it `input`, and collect stdout - within the timeout. */
function capture(
  command: string[],
  input = "",
  env?: NodeJS.ProcessEnv,
): Promise<[boolean, Buffer]> {
  return new Promise((resolve) => {
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(command[0] as string, command.slice(1), {
        stdio: ["pipe", "pipe", "ignore"],
        ...(env ? { env } : {}),
      });
    } catch {
      resolve([false, Buffer.alloc(0)]);
      return;
    }
    const out: Buffer[] = [];
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve([false, Buffer.alloc(0)]);
    }, TIMEOUT_MS);
    child.stdout?.on("data", (chunk: Buffer) => out.push(chunk));
    child.stdin?.on("error", () => {});
    child.on("error", () => {
      clearTimeout(timer);
      resolve([false, Buffer.alloc(0)]);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve([code === 0, Buffer.concat(out)]);
    });
    child.stdin?.end(input);
  });
}

/** The escape sequence for `text`, or null when it is too long to send. */
export function osc52Sequence(text: string): string | null {
  const encoded = Buffer.from(text, "utf8").toString("base64");
  return encoded.length > MAX_OSC52_ENCODED ? null : `\x1b]52;c;${encoded}\x07`;
}

/** Copy `text`, returning the mechanism that took it. `writeOsc52` writes the
 * escape to the terminal. */
export async function copyText(
  text: string,
  writeOsc52: ((sequence: string) => void) | null,
  platform: string = process.platform,
  env: NodeJS.ProcessEnv = process.env,
): Promise<string> {
  if (!text) throw new ClipboardError("nothing to copy");
  let used = "";
  for (const command of commandsFor(platform, env)) {
    if ((await capture(command, text))[0]) {
      used = command[0] as string;
      break;
    }
  }
  const sequence = osc52Sequence(text);
  if ((!used || isRemoteSession(env)) && writeOsc52 !== null && sequence !== null) {
    writeOsc52(sequence);
    used ||= "OSC 52";
  }
  if (!used)
    throw new ClipboardError("no clipboard tool available (tried pbcopy/wl-copy/xclip and OSC 52)");
  return used;
}

/** `1.2 kB` - what the copy notice reports. */
export function formatSize(text: string): string {
  const size = Buffer.byteLength(text, "utf8");
  if (size < 1000) return `${size} B`;
  if (size < 1_000_000) return `${formatFixed(size / 1000, 1)} kB`;
  return `${formatFixed(size / 1_000_000, 1)} MB`;
}

export interface ClipboardImage {
  readonly data: Buffer;
  /** The file's name when a file was copied rather than a picture. */
  readonly name: string | null;
}

/** A copied file first: Finder puts the file's icon on the clipboard beside its
 * path, and pasting a copied screenshot should attach the screenshot. */
const MACOS_READ_IMAGE = `on run argv
    try
        set copiedFile to the clipboard as «class furl»
        return "file:" & POSIX path of copiedFile
    end try
    try
        set imageData to the clipboard as «class PNGf»
    on error
        try
            set imageData to the clipboard as TIFF picture
        on error
            return "none"
        end try
    end try
    set outFile to open for access (POSIX file (item 1 of argv)) with write permission
    set eof outFile to 0
    write imageData to outFile
    close access outFile
    return "image"
end run
`;

/** The target path comes in through the environment: `-Command` folds every
 * argument after it into the script text. */
const WINDOWS_READ_IMAGE =
  "Add-Type -AssemblyName System.Windows.Forms;" +
  "$i=[Windows.Forms.Clipboard]::GetImage();" +
  "if($i){$i.Save($env:HX_CLIPBOARD_OUT,[Drawing.Imaging.ImageFormat]::Png);'image'}" +
  "else{'none'}";

/** The image on the clipboard, or null when it holds none. Throws when the
 * clipboard cannot be read at all - over SSH, or with no tool installed. */
export async function readImage(
  platform: string = process.platform,
  env: NodeJS.ProcessEnv = process.env,
): Promise<ClipboardImage | null> {
  if (isRemoteSession(env)) {
    throw new ClipboardError(
      "the clipboard is on your own machine, not this one - copy the image file over and paste its path instead",
    );
  }
  if (platform === "darwin") return readMacos();
  if (platform === "win32") return readWindows();
  return readLinux(env);
}

async function withScratch<T>(fn: (target: string) => Promise<T>): Promise<T> {
  const target = join(tmpdir(), `hx-clipboard-${randomBytes(4).toString("hex")}.img`);
  writeFileSync(target, "");
  try {
    return await fn(target);
  } finally {
    rmSync(target, { force: true });
  }
}

function readMacos(): Promise<ClipboardImage | null> {
  return withScratch(async (target) => {
    const [ok, out] = await capture(["osascript", "-e", MACOS_READ_IMAGE, target]);
    if (!ok) throw new ClipboardError("osascript could not read the clipboard");
    const answer = strip(out.toString("utf8"));
    if (answer.startsWith("file:")) return copiedFile(answer.slice("file:".length));
    if (answer === "image") return { data: readFileSync(target), name: null };
    return null;
  });
}

function readWindows(): Promise<ClipboardImage | null> {
  return withScratch(async (target) => {
    const [ok, out] = await capture(
      ["powershell", "-NoProfile", "-STA", "-Command", WINDOWS_READ_IMAGE],
      "",
      {
        ...process.env,
        HX_CLIPBOARD_OUT: target,
      },
    );
    if (!ok) throw new ClipboardError("PowerShell could not read the clipboard");
    return strip(out.toString("utf8")) === "image"
      ? { data: readFileSync(target), name: null }
      : null;
  });
}

/** `wl-paste` on Wayland, `xclip` on X11: list what the clipboard offers, then
 * ask for a file list or the first image type in it. */
async function readLinux(env: NodeJS.ProcessEnv): Promise<ClipboardImage | null> {
  let listTypes: string[];
  let fetch: string[];
  if (env.WAYLAND_DISPLAY) {
    listTypes = ["wl-paste", "--list-types"];
    fetch = ["wl-paste", "--no-newline", "--type"];
  } else if (env.DISPLAY) {
    listTypes = ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"];
    fetch = ["xclip", "-selection", "clipboard", "-o", "-t"];
  } else throw new ClipboardError("no clipboard tool available (tried wl-paste and xclip)");

  const [ok, out] = await capture(listTypes);
  if (!ok) throw new ClipboardError(`${listTypes[0]} could not read the clipboard`);
  const offered = splitWhitespace(out.toString("utf8"));
  if (offered.includes("text/uri-list")) {
    const [found, uris] = await capture([...fetch, "text/uri-list"]);
    if (found) {
      for (const line of splitLines(uris.toString("utf8"))) {
        if (!line.startsWith("file://")) continue;
        const copied = copiedFile(decodeURIComponent(new URL(strip(line)).pathname));
        if (copied !== null) return copied;
      }
    }
  }
  const kind = ["image/png", ...offered].find((t) => offered.includes(t) && t.startsWith("image/"));
  if (kind === undefined) return null;
  const [fetched, data] = await capture([...fetch, kind]);
  return fetched && data.length ? { data, name: null } : null;
}

/** A file copied in the file manager, when it is an image. */
function copiedFile(path: string): ClipboardImage | null {
  if (!isImagePath(path)) return null;
  try {
    // Checked before reading: otherwise the whole file lands in memory only
    // for the decoder to refuse it.
    if (statSync(path).size > MAX_SOURCE_BYTES)
      throw new ClipboardError(`${basename(path)} is too large to attach`);
    return { data: readFileSync(path), name: basename(path) };
  } catch (error) {
    if (error instanceof ClipboardError) throw error;
    throw new ClipboardError(`cannot read ${path}: ${(error as Error).message}`);
  }
}
