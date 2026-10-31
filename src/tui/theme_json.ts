/**
 * Themes as data: a JSON file in pi's format, a `vars` block of raw colours and
 * a `colors` block mapping semantic roles onto them. Role names in the file are
 * pi's camelCase, so one of its theme files loads here unchanged.
 */

import { readdirSync } from "node:fs";
import { compareCodePoints } from "../json.ts";
import { basename, isDir, join, readText } from "../pathlib.ts";
import { userThemesDir } from "../paths.ts";
import { repr } from "../py.ts";
import { type Palette, ROLES, type Role } from "./roles.ts";
import ansiTheme from "./themes/ansi.json" with { type: "json" };
import darkTheme from "./themes/dark.json" with { type: "json" };
import lightTheme from "./themes/light.json" with { type: "json" };

/** JSON role name -> palette role, for the roles whose names differ. */
const ALIASES: Record<string, string> = {
  thinkingText: "thinking",
  userMessageBg: "user_bg",
  userMessageText: "user_text",
  mdListBullet: "md_bullet",
  toolDiffAdded: "diff_added",
  toolDiffRemoved: "diff_removed",
  toolDiffContext: "diff_context",
  toolDiffHunk: "diff_hunk",
};

/** Roles pi keeps in `export` rather than `colors`. */
const EXPORT_FALLBACKS: Record<string, string> = { background: "pageBg", surface: "cardBg" };

/** Roles that borrow another role when absent: only roles pi has no concept of. */
const DERIVED: Record<string, string> = {
  panel: "surface",
  diff_hunk: "md_link",
  user_text: "text",
  md_code_block_bg: "surface",
  // Tool arguments were the accent until the role existed.
  tool_args: "accent",
};

/** Roles pi defines that nothing here reads any more: accepted, and dropped. */
const RETIRED = new Set(["tool_pending_bg"]);
const FIELDS = new Set<string>(ROLES);

function snake(name: string): string {
  return ALIASES[name] ?? name.replace(/[A-Z]/g, (char) => `_${char.toLowerCase()}`);
}

/** Rough perceived lightness of a hex colour, 0 (black) to 1 (white). */
function luminance(color: string): number {
  if (!color.startsWith("#") || color.length !== 7) return 0;
  const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(color.slice(i, i + 2), 16) / 255) as [
    number,
    number,
    number,
  ];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Build a palette from parsed theme JSON, naming the offending role on error
 * rather than letting a typo become a silently missing colour. */
export function parse(
  data: Record<string, unknown>,
  name: string | null = null,
  source = "<memory>",
): Palette {
  const variables = data.vars ?? {};
  const colors = data.colors ?? {};
  const exported = (data.export ?? {}) as Record<string, unknown>;
  if (!isObject(variables) || !isObject(colors))
    throw new Error(`${source}: 'vars' and 'colors' must be objects`);
  const resolve = (value: unknown, role: string): string => {
    if (typeof value !== "string")
      throw new Error(`${source}: role ${repr(role)} must be a string`);
    // A value is a reference when it names a var, and a literal otherwise.
    return String(variables[value] ?? value);
  };
  const resolved: Record<string, string> = {};
  for (const [key, value] of Object.entries(colors)) {
    const field = snake(key);
    if (RETIRED.has(field)) continue;
    if (!FIELDS.has(field)) throw new Error(`${source}: unknown theme role ${repr(key)}`);
    resolved[field] = resolve(value, key);
  }
  for (const [field, key] of Object.entries(EXPORT_FALLBACKS)) {
    if (!(field in resolved) && key in exported) resolved[field] = resolve(exported[key], key);
  }
  for (const [field, from] of Object.entries(DERIVED)) {
    if (!(field in resolved) && from in resolved) resolved[field] = resolved[from] as string;
  }
  const missing = ROLES.filter((role) => !(role in resolved)).sort(compareCodePoints);
  if (missing.length) throw new Error(`${source}: theme is missing roles: ${missing.join(", ")}`);
  const themeName = name || data.name;
  if (!themeName) throw new Error(`${source}: theme has no name`);
  // pi's files do not carry the flag; infer it the way a reader would.
  const dark = data.dark ?? luminance(resolved.background as string) < 0.5;
  return { name: String(themeName), dark: Boolean(dark), ...(resolved as Record<Role, string>) };
}

/** Read one theme file. The filename is the theme name. */
export function loadFile(path: string): Palette {
  let data: unknown;
  try {
    data = JSON.parse(readText(path));
  } catch (error) {
    if (error instanceof SyntaxError) throw new Error(`${path}: not valid JSON: ${error.message}`);
    throw error;
  }
  if (!isObject(data)) throw new Error(`${path}: theme must be a JSON object`);
  return parse(data, basename(path).replace(/\.json$/, ""), path);
}

/** Themes shipped with HX, bundled into the build. */
export function builtinPalettes(): Map<string, Palette> {
  return new Map([
    ["ansi", parse(ansiTheme, "ansi", "ansi.json")],
    ["dark", parse(darkTheme, "dark", "dark.json")],
    ["light", parse(lightTheme, "light", "light.json")],
  ]);
}

function themeFiles(): string[] {
  const directory = userThemesDir();
  if (!isDir(directory)) return [];
  return readdirSync(directory)
    .filter((name) => name.endsWith(".json"))
    .sort(compareCodePoints)
    .map((name) => join(directory, name));
}

/** Themes from `~/.hx/themes`. A broken file costs that theme, not the session. */
export function userPalettes(): Map<string, Palette> {
  const palettes = new Map<string, Palette>();
  for (const path of themeFiles()) {
    try {
      palettes.set(basename(path).replace(/\.json$/, ""), loadFile(path));
    } catch {
      // Reported by userPaletteErrors.
    }
  }
  return palettes;
}

/** Human-readable reasons any user theme file was skipped. */
export function userPaletteErrors(): string[] {
  const errors: string[] = [];
  for (const path of themeFiles()) {
    try {
      loadFile(path);
    } catch (error) {
      errors.push((error as Error).message);
    }
  }
  return errors;
}
