/** Laying out columns, measured rather than guessed. */

import { pyRound, splitWhitespace } from "../py.ts";
import { plainText } from "../term/sanitize.ts";
import { cellWidth, truncateToWidth } from "../term/width.ts";
import { ELLIPSIS } from "./glyphs.ts";

/**
 * Pad each column to the widest cell in it, measured on the visible text. The
 * last column is not padded. A line break inside a cell is shown as `↵`
 * rather than obeyed: a cell is one row by definition.
 */
export function columns(input: readonly (readonly string[])[], gap = 2): string[] {
  if (input.length === 0) return [];
  const rows = input.map((row) => row.map((cell) => cell.replace(/\r\n|\r|\n/g, "↵")));
  const count = Math.max(...rows.map((row) => row.length));
  const widths = Array.from({ length: count }, (_, index) =>
    Math.max(
      0,
      ...rows.filter((row) => index < row.length).map((row) => cellWidth(row[index] as string)),
    ),
  );
  return rows.map((row) =>
    row
      .map((cell, index) =>
        index === row.length - 1
          ? cell
          : cell + " ".repeat((widths[index] as number) - cellWidth(cell)),
      )
      .join(" ".repeat(gap)),
  );
}

/** How many cells of a `cells`-wide gauge are filled at `fraction`. Any
 * non-zero fraction lights at least one cell. */
export function meterFill(fraction: number, cells: number): number {
  const clamped = Math.min(Math.max(fraction, 0), 1);
  const filled = pyRound(clamped * cells);
  return clamped > 0 ? Math.max(1, filled) : filled;
}

/** Collapse to a single line, sanitized and ellipsized to fit. */
export function oneLine(text: string, width: number): string {
  const flat = splitWhitespace(plainText(text)).join(" ");
  if (cellWidth(flat) <= width) return flat;
  return truncateToWidth(flat, Math.max(0, width - cellWidth(ELLIPSIS))) + ELLIPSIS;
}
