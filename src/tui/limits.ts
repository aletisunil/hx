/** How much of a thing is shown before it is folded away - one number for
 * every code path, so what you see does not depend on how you arrived. */

/** Lines shown before a block offers to expand. */
export const PREVIEW_LINES = 8;
/** Ceiling on an expanded block, so ctrl+o cannot stall the renderer. */
export const EXPANDED_MAX = 400;
/** Characters of a running tool's streamed output kept for display. */
export const STREAM_TAIL = 256_000;
/** Rows visible in a scrolling list that has no height to work with. */
export const LIST_VISIBLE = 8;
/** Rows a picker keeps even on a very short terminal. */
export const LIST_MINIMUM = 4;
/** Cells a one-line summary is collapsed to before being ellipsized. */
export const RECORD_WIDTH = 72;
