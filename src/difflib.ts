/**
 * Python's `difflib.SequenceMatcher` and `unified_diff`, line for line.
 *
 * The diff an approval shows, the one Edit returns to the model and the word
 * highlighting inside a changed line are all computed this way. Other diff
 * libraries pick different, equally valid alignments, which would change what
 * every edit looks like.
 */

export type Tag = "replace" | "delete" | "insert" | "equal";
export type Opcode = [Tag, number, number, number, number];
type Match = [number, number, number];

export class SequenceMatcher<T> {
  private b2j = new Map<T, number[]>();
  private matchingBlocks: Match[] | null = null;
  private opcodes: Opcode[] | null = null;

  constructor(
    readonly a: readonly T[],
    readonly b: readonly T[],
    /** Treat elements of `b` seen in more than 1% of 200+ items as junk. */
    readonly autojunk = true,
  ) {
    for (const [index, element] of b.entries()) {
      let indices = this.b2j.get(element);
      if (indices === undefined) {
        indices = [];
        this.b2j.set(element, indices);
      }
      indices.push(index);
    }
    if (autojunk && b.length >= 200) {
      const ntest = Math.floor(b.length / 100) + 1;
      for (const [element, indices] of [...this.b2j]) {
        if (indices.length > ntest) this.b2j.delete(element);
      }
    }
  }

  /** The longest matching block in `a[alo:ahi]` and `b[blo:bhi]`, earliest in
   * `a` and then in `b` among equals. No junk is defined, so no junk extension. */
  findLongestMatch(alo: number, ahi: number, blo: number, bhi: number): Match {
    const { a, b } = this;
    let besti = alo;
    let bestj = blo;
    let bestsize = 0;
    let j2len = new Map<number, number>();
    for (let i = alo; i < ahi; i++) {
      const next = new Map<number, number>();
      for (const j of this.b2j.get(a[i] as T) ?? []) {
        if (j < blo) continue;
        if (j >= bhi) break;
        const k = (j2len.get(j - 1) ?? 0) + 1;
        next.set(j, k);
        if (k > bestsize) {
          besti = i - k + 1;
          bestj = j - k + 1;
          bestsize = k;
        }
      }
      j2len = next;
    }
    // Popular elements removed by autojunk are not junk to `isbjunk`, so the
    // match extends over them as over any equal element.
    while (besti > alo && bestj > blo && a[besti - 1] === b[bestj - 1]) {
      besti--;
      bestj--;
      bestsize++;
    }
    while (
      besti + bestsize < ahi &&
      bestj + bestsize < bhi &&
      a[besti + bestsize] === b[bestj + bestsize]
    ) {
      bestsize++;
    }
    return [besti, bestj, bestsize];
  }

  getMatchingBlocks(): Match[] {
    if (this.matchingBlocks !== null) return this.matchingBlocks;
    const la = this.a.length;
    const lb = this.b.length;
    const queue: [number, number, number, number][] = [[0, la, 0, lb]];
    const blocks: Match[] = [];
    while (queue.length) {
      const [alo, ahi, blo, bhi] = queue.pop() as [number, number, number, number];
      const match = this.findLongestMatch(alo, ahi, blo, bhi);
      const [i, j, k] = match;
      if (k) {
        blocks.push(match);
        if (alo < i && blo < j) queue.push([alo, i, blo, j]);
        if (i + k < ahi && j + k < bhi) queue.push([i + k, ahi, j + k, bhi]);
      }
    }
    blocks.sort((x, y) => x[0] - y[0] || x[1] - y[1] || x[2] - y[2]);
    let [i1, j1, k1] = [0, 0, 0];
    const nonAdjacent: Match[] = [];
    for (const [i2, j2, k2] of blocks) {
      if (i1 + k1 === i2 && j1 + k1 === j2) k1 += k2;
      else {
        if (k1) nonAdjacent.push([i1, j1, k1]);
        [i1, j1, k1] = [i2, j2, k2];
      }
    }
    if (k1) nonAdjacent.push([i1, j1, k1]);
    nonAdjacent.push([la, lb, 0]);
    this.matchingBlocks = nonAdjacent;
    return nonAdjacent;
  }

  getOpcodes(): Opcode[] {
    if (this.opcodes !== null) return this.opcodes;
    let i = 0;
    let j = 0;
    const answer: Opcode[] = [];
    for (const [ai, bj, size] of this.getMatchingBlocks()) {
      let tag: Tag | "" = "";
      if (i < ai && j < bj) tag = "replace";
      else if (i < ai) tag = "delete";
      else if (j < bj) tag = "insert";
      if (tag) answer.push([tag, i, ai, j, bj]);
      i = ai + size;
      j = bj + size;
      if (size) answer.push(["equal", ai, i, bj, j]);
    }
    this.opcodes = answer;
    return answer;
  }

  /** Groups of opcodes with up to `n` lines of context. */
  *getGroupedOpcodes(n = 3): Generator<Opcode[]> {
    let codes = this.getOpcodes().map((code) => [...code] as Opcode);
    if (codes.length === 0) codes = [["equal", 0, 1, 0, 1]];
    const first = codes[0] as Opcode;
    if (first[0] === "equal") {
      const [tag, i1, i2, j1, j2] = first;
      codes[0] = [tag, Math.max(i1, i2 - n), i2, Math.max(j1, j2 - n), j2];
    }
    const last = codes.at(-1) as Opcode;
    if (last[0] === "equal") {
      const [tag, i1, i2, j1, j2] = last;
      codes[codes.length - 1] = [tag, i1, Math.min(i2, i1 + n), j1, Math.min(j2, j1 + n)];
    }
    const nn = n + n;
    let group: Opcode[] = [];
    for (let [tag, i1, i2, j1, j2] of codes) {
      if (tag === "equal" && i2 - i1 > nn) {
        group.push([tag, i1, Math.min(i2, i1 + n), j1, Math.min(j2, j1 + n)]);
        yield group;
        group = [];
        i1 = Math.max(i1, i2 - n);
        j1 = Math.max(j1, j2 - n);
      }
      group.push([tag, i1, i2, j1, j2]);
    }
    if (group.length && !(group.length === 1 && (group[0] as Opcode)[0] === "equal")) yield group;
  }
}

function formatRange(start: number, stop: number): string {
  let beginning = start + 1;
  const length = stop - start;
  if (length === 1) return String(beginning);
  if (!length) beginning -= 1;
  return `${beginning},${length}`;
}

/** `difflib.unified_diff(a, b, fromfile, tofile, n=n)`, joined. */
export function unifiedDiff(
  a: readonly string[],
  b: readonly string[],
  fromfile = "",
  tofile = "",
  n = 3,
): string {
  const out: string[] = [];
  let started = false;
  for (const group of new SequenceMatcher(a, b).getGroupedOpcodes(n)) {
    if (!started) {
      started = true;
      out.push(`--- ${fromfile}\n`, `+++ ${tofile}\n`);
    }
    const first = group[0] as Opcode;
    const last = group.at(-1) as Opcode;
    out.push(`@@ -${formatRange(first[1], last[2])} +${formatRange(first[3], last[4])} @@\n`);
    for (const [tag, i1, i2, j1, j2] of group) {
      if (tag === "equal") {
        for (const line of a.slice(i1, i2)) out.push(` ${line}`);
        continue;
      }
      if (tag === "replace" || tag === "delete")
        for (const line of a.slice(i1, i2)) out.push(`-${line}`);
      if (tag === "replace" || tag === "insert")
        for (const line of b.slice(j1, j2)) out.push(`+${line}`);
    }
  }
  return out.join("");
}
