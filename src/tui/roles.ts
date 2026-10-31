/**
 * The set of colour roles a theme has to fill. Roles are named for what they
 * mean, never for what they look like: `error` rather than `red`.
 */

export const ROLES = [
  // Surfaces.
  "background",
  "surface",
  "panel",
  "user_bg",
  "user_text",
  "custom_message_bg",
  "custom_message_text",
  "custom_message_label",
  // Tool calls have no background; these mark the words an edit added and removed.
  "tool_success_bg",
  "tool_error_bg",
  "selected_bg",
  "search_match_bg",
  "search_match_text",
  // Type.
  "text",
  "muted",
  "dim",
  "accent",
  "border",
  "border_accent",
  "border_muted",
  // State.
  "success",
  "error",
  "warning",
  "thinking",
  "bash_mode",
  // Chrome.
  "scrollbar_track",
  "scrollbar_thumb",
  // Tool blocks: a step below the answer, and what came back a step below that.
  "tool_title",
  "tool_args",
  "tool_output",
  // Markdown.
  "md_heading",
  "md_link",
  "md_link_url",
  "md_code",
  "md_code_block",
  "md_code_block_bg",
  "md_code_block_border",
  "md_quote",
  "md_quote_border",
  "md_hr",
  "md_bullet",
  // Diffs.
  "diff_added",
  "diff_removed",
  "diff_context",
  "diff_hunk",
  // Syntax highlighting.
  "syntax_comment",
  "syntax_keyword",
  "syntax_function",
  "syntax_variable",
  "syntax_string",
  "syntax_number",
  "syntax_type",
  "syntax_operator",
  "syntax_punctuation",
] as const;

export type Role = (typeof ROLES)[number];

/** Colour per role. Values are hex, except in the ANSI palette. */
export type Palette = { readonly name: string; readonly dark: boolean } & {
  readonly [role in Role]: string;
};
