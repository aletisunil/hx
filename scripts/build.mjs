// Bundles src/main.ts into dist/hx.js: one file, so `hx` starts by reading one
// module rather than resolving a tree of them.
import { readFileSync, rmSync } from "node:fs";
import { build } from "esbuild";

const { name, version } = JSON.parse(readFileSync(new URL("../package.json", import.meta.url)));

// The build owns dist/: whatever else is there - a uv build's wheels and the
// .gitignore it leaves, which hides the bundle from npm - would ship with it.
rmSync(new URL("../dist", import.meta.url), { recursive: true, force: true });

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
  define: { __HX_VERSION__: JSON.stringify(version), __HX_PACKAGE__: JSON.stringify(name) },
  // The trace page's template, bundled as the text it is.
  loader: { ".html": "text" },
  sourcemap: "linked",
  // Native code, loaded from node_modules at run time rather than bundled.
  external: ["sharp", "koffi", "@vscode/tree-sitter-wasm"],
  logLevel: "warning",
});
