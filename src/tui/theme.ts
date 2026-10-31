/**
 * The active theme: one palette of named roles. Every colour in the UI comes
 * from a role, and there are no hex literals in the views - that is what makes
 * a user's theme file actually reach the screen.
 */

import { repr } from "../py.ts";
import { type Palette, ROLES } from "./roles.ts";
import { builtinPalettes, userPalettes } from "./theme_json.ts";

export const DEFAULT_THEME = "dark";
const KNOWN = new Set<string>(ROLES);

let shipped: Map<string, Palette> | null = null;

/** Themes shipped with HX. Parsed once. */
export function builtins(): Map<string, Palette> {
  shipped ??= builtinPalettes();
  return shipped;
}

/** Every theme available right now, user themes shadowing built-ins - re-read
 * each call, so a new file is picked up by `/theme` without a restart. */
export function palettes(): Map<string, Palette> {
  return new Map([...builtins(), ...userPalettes()]);
}

export const defaultPalette = () => builtins().get(DEFAULT_THEME) as Palette;

/** The active palette. A module-level singleton: every renderable wants it. */
export class Theme {
  palette: Palette;

  constructor(palette?: Palette) {
    this.palette = palette ?? defaultPalette();
  }

  /** Switch palettes. Unknown names fall back to dark. */
  use(name: string): Palette {
    this.palette = palettes().get(name) ?? defaultPalette();
    return this.palette;
  }

  /** Raw colour for a role. */
  color(role: string): string {
    if (!KNOWN.has(role)) throw new Error(`unknown theme role ${repr(role)}`);
    return this.palette[role as keyof Palette] as string;
  }
}

export const THEME = new Theme();
