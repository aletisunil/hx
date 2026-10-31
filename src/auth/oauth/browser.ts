/**
 * The browser half of an authorization-code sign-in, shared by every route.
 *
 * A route supplies the authorize URL and the loopback port it registered; this
 * opens the browser, listens for the redirect, and races it against a paste
 * prompt. What the route does with the code stays in the route's own module.
 */

import { spawn } from "node:child_process";
import { CallbackError, type CallbackResult, LoopbackCallback, parseRedirect } from "./callback.ts";

/** A sign-in that could not complete. The message is shown as-is. */
export class OAuthError extends Error {
  override name = "OAuthError";
}

/** How a login flow talks to whoever started it (CLI prompt or TUI modal). */
export interface LoginInteraction {
  showUrl(url: string, instructions: string): void;
  showDeviceCode(userCode: string, verificationUri: string): void;
  progress(message: string): void;
  /** A pasted redirect URL or code. May never resolve - the browser callback
   * usually wins the race - so the signal says when to stop asking. */
  promptPaste(message: string, signal: AbortSignal): Promise<string>;
}

/**
 * Send the user to `url` and return the code the provider hands back.
 *
 * The callback server and the paste prompt race each other: over SSH the
 * browser opens on the wrong machine and can never reach the loopback port, so
 * pasting the final redirect URL has to work just as well.
 */
export async function authorizeInBrowser(
  url: string,
  options: {
    port: number;
    path: string;
    state: string;
    interaction: LoginInteraction;
    instructions?: string;
  },
): Promise<CallbackResult> {
  const { interaction } = options;
  const callback = await new LoopbackCallback(options.port, options.path, options.state).start();
  let result: CallbackResult;
  try {
    interaction.showUrl(
      url,
      `${options.instructions ? `${options.instructions} ` : ""}Complete the sign-in in your browser. On a remote machine, paste the final redirect URL here instead.`,
    );
    if (!callback.listening) {
      interaction.progress(
        `Port ${options.port} is busy, so the browser cannot hand the code back. ` +
          "Paste the redirect URL here instead.",
      );
    } else if (!(await openBrowser(url))) {
      interaction.progress("Could not open a browser - open the URL above manually.");
    }
    result = await raceCallbackAndPaste(callback, interaction);
  } finally {
    await callback.close();
  }
  if (result.state !== null && result.state !== options.state) {
    throw new OAuthError("OAuth state mismatch - discard this login and try again.");
  }
  return result;
}

async function raceCallbackAndPaste(
  callback: LoopbackCallback,
  interaction: LoginInteraction,
): Promise<CallbackResult> {
  const stop = new AbortController();
  type Outcome =
    | { from: "callback"; result?: CallbackResult; error?: unknown }
    | { from: "paste"; text?: string; error?: unknown };
  const fromCallback = callback.wait().then(
    (result): Outcome => ({ from: "callback", result }),
    (error): Outcome => ({ from: "callback", error }),
  );
  const fromPaste = interaction
    .promptPaste("Paste the redirect URL or authorization code:", stop.signal)
    .then(
      (text): Outcome => ({ from: "paste", text }),
      (error): Outcome => ({ from: "paste", error }),
    );
  try {
    const outcome = await Promise.race([fromCallback, fromPaste]);
    // Prefer the loopback result: it is the one whose state was validated.
    if (outcome.from === "callback") {
      if (outcome.error === undefined) return outcome.result as CallbackResult;
      throw outcome.error;
    }
    if (outcome.error !== undefined) throw outcome.error;
    return parseRedirect(outcome.text ?? "");
  } finally {
    stop.abort();
  }
}

/**
 * Open the URL in the user's browser. `$BROWSER` wins, as Python's
 * `webbrowser` let it; otherwise the platform's opener.
 */
export async function openBrowser(url: string): Promise<boolean> {
  const commands: string[][] = [];
  for (const entry of (process.env.BROWSER ?? "").split(":").filter(Boolean)) {
    commands.push(entry.includes("%s") ? entry.replace("%s", url).split(" ") : [entry, url]);
  }
  if (process.platform === "darwin") commands.push(["open", url]);
  else if (process.platform === "win32") commands.push(["cmd", "/c", "start", "", url]);
  else commands.push(["xdg-open", url], ["gio", "open", url]);

  for (const [command, ...args] of commands) {
    const ok = await new Promise<boolean>((resolve) => {
      const child = spawn(command as string, args, { stdio: "ignore" });
      child.on("error", () => resolve(false));
      child.on("exit", (code) => resolve(code === 0));
    });
    if (ok) return true;
  }
  return false;
}

export { CallbackError };
