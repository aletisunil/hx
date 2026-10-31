import "./providers/subscriptions.ts";
import { main } from "./cli.ts";

// A reader that goes away - `hx docs | head` - ends the output, not the
// process with a stack trace.
for (const stream of [process.stdout, process.stderr]) {
  stream.on("error", (error: NodeJS.ErrnoException) => {
    if (error.code !== "EPIPE") throw error;
  });
}

// The exit code is set, not forced with process.exit(): a pipe on macOS is
// written asynchronously, and exiting now would cut the output short.
process.exitCode = await main();
