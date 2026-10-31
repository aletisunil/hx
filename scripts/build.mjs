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
  // Bundled CommonJS packages (yaml) still call require() for Node's builtins.
  banner: {
    js: '#!/usr/bin/env node\nimport { createRequire as __hxRequire } from "node:module";\nconst require = __hxRequire(import.meta.url);',
  },
  define: { __HX_VERSION__: JSON.stringify(version) },
  sourcemap: "linked",
  // Native code, loaded from node_modules at run time rather than bundled.
  external: ["sharp", "koffi"],
  logLevel: "warning",
});
