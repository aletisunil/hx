/** `shutil.which`: the first executable `name` on `PATH`. */

import { accessSync, constants, statSync } from "node:fs";
import { join } from "./pathlib.ts";

export function which(name: string, path: string | undefined = process.env.PATH): string | null {
  const candidates = name.includes("/")
    ? [name]
    : (path ?? "").split(":").map((dir) => join(dir || ".", name));
  for (const candidate of candidates) {
    try {
      accessSync(candidate, constants.X_OK);
      if (statSync(candidate).isFile()) return candidate;
    } catch {
      // Not here.
    }
  }
  return null;
}
