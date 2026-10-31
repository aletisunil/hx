// Bundles src/main.ts into dist/hx.js: one file, so `hx` starts by reading one
// module rather than resolving a tree of them.
import { readFileSync } from "node:fs";
import { build } from "esbuild";

const { version } = JSON.parse(readFileSync(new URL("../package.json", import.meta.url)));

await build({
  entryPoints: ["src/main.ts"],
  outfile: "dist/hx.js",
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node22",
  banner: { js: "#!/usr/bin/env node" },
  define: { __HX_VERSION__: JSON.stringify(version) },
  sourcemap: "linked",
  logLevel: "warning",
});
