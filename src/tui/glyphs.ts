/** Every glyph the UI draws, defined once: each symbol means one thing, and
 * nothing outside this module spells one out. */

/** Frames for anything in progress. Advances every 80ms. */
export const SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
/** A tool call finished. Only that. */
export const TOOL_DONE = "●";
/** A tool call failed, or a notice reports an error. */
export const TOOL_FAILED = "✗";
/** A tool call held at an approval: nothing runs until the user answers. */
export const TOOL_AWAITING = "?";
/** Hangs what a tool call gave back under its header. */
export const TOOL_RESULT = "⎿  ";
/** A todo not started. */
export const TODO_PENDING = "○";
export const TODO_ACTIVE = "▸";
export const TODO_DONE = "✓";
/** Where Enter will land, in any list. */
export const CURSOR = "→ ";
/** The value already in force. Sits beside `CURSOR`, never instead of it. */
export const CURRENT = "✓ ";
/** An unselected row, occupying exactly what a marker would. */
export const GUTTER = "  ";
/** Bullets for a notice, by level. */
export const NOTICE: Record<string, string> = {
  info: "· ",
  warning: "! ",
  error: "✗ ",
  success: "✓ ",
};
/** A shell command, wherever one is shown. */
export const BASH = "$ ";
/** One cell of the context gauge, filled and unfilled. */
export const METER_FULL = "▰";
export const METER_EMPTY = "▱";
/** An image attached to a message. */
export const IMAGE = "▣ ";
export const RULE = "─";
export const QUOTE_RAIL = "│ ";
export const ELLIPSIS = "…";
/** Joins fields on one line: status bar, hints, record lines. */
export const SEPARATOR = " · ";
