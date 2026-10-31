/**
 * The shipped documentation: the manual and the release record.
 *
 * `README.md` and `CHANGELOG.md` live at the package root - one copy, rendered
 * by GitHub and npm - beside the `dist/` folder the bundle runs from, in a
 * checkout and in an installed package alike.
 *
 * They are part of the product, not just the repo. The system prompt points a
 * session at `hx docs` and `hx changelog`, so "how do I set the API key?" or
 * "what changed in 0.1.5?" is answered from the documentation that shipped with
 * the running version rather than from the model's memory of some other
 * release. Nothing is read at startup: the text costs tokens only in the turn
 * that asks for it.
 */

import { fileURLToPath } from "node:url";
import { isFile, readText } from "./pathlib.ts";
import { lstrip, repr, rstrip, strip } from "./py.ts";

export const README = "README.md";
export const CHANGELOG = "CHANGELOG.md";

const HEADING = /^(#{2,4}) +(.+?)\s*$/gm;
const RELEASE = /^## +\[([^\]]+)\]/gm;
const LINK_DEF = /^\[[^\]]+\]: \S+$/gm;

/** The document is not next to the code. A broken build, not a user error. */
export class DocsUnavailable extends Error {
  override name = "DocsUnavailable";
}

/** Locate a shipped document at the package root. */
export function docPath(name: string): string {
  const candidate = fileURLToPath(new URL(`../${name}`, import.meta.url));
  if (isFile(candidate)) return candidate;
  throw new DocsUnavailable(`${name} is not bundled with this installation of hx`);
}

export function docText(name: string): string {
  return readText(docPath(name));
}

/**
 * Split a document on its headings, title to section text.
 *
 * Subsections are addressable in their own right and are also part of their
 * parent, so `hx docs credentials` prints just that and `hx docs install`
 * prints it in context. The first heading of a given title wins, so an outer
 * section is never shadowed by something nested deeper.
 *
 * The text above the first heading is kept under `""` so nothing is lost.
 */
export function sections(text: string): Map<string, string> {
  const matches = [...text.matchAll(HEADING)];
  const first = matches[0];
  const found = new Map<string, string>([["", strip(first ? text.slice(0, first.index) : text)]]);

  matches.forEach((match, index) => {
    const level = (match[1] as string).length;
    let end = text.length;
    for (const later of matches.slice(index + 1)) {
      if ((later[1] as string).length <= level) {
        end = later.index;
        break;
      }
    }
    const title = match[2] as string;
    if (!found.has(title)) found.set(title, strip(text.slice(match.index, end)));
  });
  return found;
}

/** Titles matching `query`: exact (case-insensitive) wins outright,
 * otherwise every substring match, so an ambiguous query can be reported. */
function matchTitles(query: string, titles: string[]): string[] {
  const wanted = strip(query).toLowerCase();
  const exact = titles.filter((title) => title.toLowerCase() === wanted);
  if (exact.length) return exact;
  return titles.filter((title) => title.toLowerCase().includes(wanted));
}

/**
 * The README, whole or by section.
 *
 * Without a section this lists the section titles rather than printing the
 * entire manual: the full text is thousands of tokens, and a session asking
 * "how do I set the API key" should pay for one section, not all of them.
 */
export function manual(section: string | null = null): string {
  const text = docText(README);
  const found = sections(text);
  const titles = [...found.keys()].filter((title) => title);

  if (section === null) {
    // Indented by heading depth, so the listing reads as a table of contents.
    const listed = [...text.matchAll(HEADING)]
      .map((match) => `${"  ".repeat((match[1] as string).length - 1)}${match[2]}`)
      .join("\n");
    return (
      `${found.get("")}\n\nSections (print one with \`hx docs <section>\`, ` +
      `or all of it with \`hx docs --all\`):\n${listed}`
    );
  }

  const matches = matchTitles(section, titles);
  if (matches.length === 0) {
    throw new DocsUnavailable(
      `no section matching ${repr(section)}. Sections: ${titles.join(", ")}`,
    );
  }
  if (matches.length > 1) {
    throw new DocsUnavailable(`${repr(section)} matches several sections: ${matches.join(", ")}`);
  }
  return found.get(matches[0] as string) as string;
}

/** Every `## [version]` section of the changelog, newest first. */
export function releases(text: string = docText(CHANGELOG)): Map<string, string> {
  const found = new Map<string, string>();
  const matches = [...text.matchAll(RELEASE)];
  matches.forEach((match, index) => {
    const end = matches[index + 1]?.index ?? text.length;
    // The trailing `---` rules and the link definitions at the foot of the
    // file are page furniture, not part of the release they follow.
    const body = strip(text.slice(match.index, end).replace(LINK_DEF, ""));
    found.set(match[1] as string, strip(rstrip(body, "-")));
  });
  return found;
}

/**
 * The changelog, whole or for one release.
 *
 * `version` accepts `0.1.5`, `v0.1.5`, `unreleased` or `latest`.
 */
export function changelog(version: string | null = null): string {
  const text = docText(CHANGELOG);
  if (version === null) return strip(text);

  const found = releases(text);
  const wanted = lstrip(strip(version), "vV").toLowerCase();
  if (wanted === "latest") {
    const released = [...found.keys()].filter((name) => name.toLowerCase() !== "unreleased");
    if (released.length === 0) {
      throw new DocsUnavailable("the changelog has no released versions yet");
    }
    return found.get(released[0] as string) as string;
  }
  for (const [name, body] of found) {
    if (lstrip(name.toLowerCase(), "v") === wanted) return body;
  }
  throw new DocsUnavailable(
    `no changelog entry for ${repr(version)}. Versions: ${[...found.keys()].join(", ")}`,
  );
}
