/**
 * Reading a line from the person at the terminal, outside the TUI: Python's
 * `input()` and `getpass.getpass()`.
 */

import { closeSync, openSync, readSync, writeSync } from "node:fs";
import { ReadStream } from "node:tty";

/** The user pressed ctrl+c at a prompt: Python's `KeyboardInterrupt`. */
export class Interrupted extends Error {
  override name = "KeyboardInterrupt";
}

/** The input ended before a line did: Python's `EOFError`. */
export class EndOfInput extends Error {
  override name = "EOFError";
}

/** `sys.stdin.isatty()`. */
export const stdinIsTty = () => Boolean(process.stdin.isTTY);

/**
 * `getpass.getpass(prompt)`: the prompt and the newline after the answer go to
 * the terminal itself, and what is typed is never echoed. Line editing still
 * works: backspace, ctrl+u.
 */
export function getpass(prompt: string): string {
  let fd: number;
  try {
    fd = openSync("/dev/tty", "r+");
  } catch {
    // No terminal: read a line from stdin, as Python does with a warning.
    return input(prompt);
  }
  const stream = new ReadStream(fd);
  stream.setRawMode(true);
  try {
    writeSync(fd, prompt);
    let line = "";
    const buffer = Buffer.alloc(64);
    const decoder = new TextDecoder();
    while (true) {
      let count: number;
      try {
        count = readSync(fd, buffer, 0, buffer.length, null);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "EAGAIN") continue;
        throw error;
      }
      if (count === 0) throw new EndOfInput();
      for (const char of decoder.decode(buffer.subarray(0, count), { stream: true })) {
        if (char === "\r" || char === "\n") {
          writeSync(fd, "\n");
          return line;
        }
        if (char === "\x03") throw new Interrupted();
        if (char === "\x04" && !line) throw new EndOfInput();
        if (char === "\x7f" || char === "\b") line = [...line].slice(0, -1).join("");
        else if (char === "\x15") line = "";
        else if (char >= " ") line += char;
      }
    }
  } finally {
    stream.setRawMode(false);
    stream.destroy();
    try {
      closeSync(fd);
    } catch {
      // Already closed with the stream.
    }
  }
}

/**
 * `input(prompt)`: the prompt on stdout, one line from stdin without its
 * newline. Read a byte at a time, so nothing after the line is consumed.
 */
export function input(prompt: string): string {
  process.stdout.write(prompt);
  const bytes: number[] = [];
  const one = Buffer.alloc(1);
  while (true) {
    let count: number;
    try {
      count = readSync(0, one, 0, 1, null);
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "EAGAIN") continue;
      if (code === "EOF") count = 0;
      else throw error;
    }
    if (count === 0) {
      if (bytes.length === 0) throw new EndOfInput();
      break;
    }
    if (one[0] === 0x0a) break;
    bytes.push(one[0] as number);
  }
  return Buffer.from(bytes).toString("utf8").replace(/\r$/, "");
}
