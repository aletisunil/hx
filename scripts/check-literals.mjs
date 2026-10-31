// Source files must spell invisible line separators as escapes: a literal
// U+2028, U+2029 or U+0085 ends a regular expression or a comment early in
// some tools and is impossible to see in review.
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const fix = process.argv.includes("--fix");
const SEPARATORS = new RegExp(`[${String.fromCharCode(0x2028, 0x2029, 0x85)}]`, "g");
const bad = [];
const walk = (dir) => {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path);
    else if (path.endsWith(".ts")) {
      const text = readFileSync(path, "utf8");
      const clean = text.replace(
        SEPARATORS,
        (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
      );
      if (clean !== text) {
        bad.push(path);
        if (fix) writeFileSync(path, clean);
      }
    }
  }
};
walk("src");
if (bad.length && !fix) {
  console.error(`literal line separators in: ${bad.join(", ")} (run with --fix)`);
  process.exit(1);
}
