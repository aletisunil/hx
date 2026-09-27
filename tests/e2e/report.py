"""The run's artifact: what every test saw, in one page.

Each test records the screens it asserted against, the commands it ran and
what the model was asked. At the end of the session they are written to
``e2e-report/`` (or ``$HX_E2E_REPORT``):

* ``index.html`` - every test, its outcome, and each recorded screen rendered
  in colour, so a regression in layout is visible and not only in text;
* ``<test>/NN-<label>.txt`` - the same screens as plain text, one file each,
  so two runs can be compared with ``diff -r``.

Paths that change per run (the temporary directories, the stub's port) are
replaced by stable placeholders before anything is written, which is what
makes two reports of the same code diffable.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class Step:
    kind: str  # "screen" | "command" | "note"
    label: str
    text: str = ""
    html: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TestRecord:
    nodeid: str
    doc: str
    steps: list[Step] = field(default_factory=list)
    outcome: str = "unknown"
    duration: float = 0.0
    failure: str = ""
    requests: list[dict[str, Any]] = field(default_factory=list)
    replacements: list[tuple[str, str]] = field(default_factory=list)

    def scrub(self, text: str) -> str:
        for real, stable in self.replacements:
            text = text.replace(real, stable)
        return text

    @property
    def slug(self) -> str:
        name = self.nodeid.split("::", 1)[-1]
        module = Path(self.nodeid.split("::", 1)[0]).stem
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{module}.{name}")[:120]


class Report:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.tests: list[TestRecord] = []
        self.started = time.time()

    def begin(self, nodeid: str, doc: str) -> TestRecord:
        record = TestRecord(nodeid=nodeid, doc=doc)
        self.tests.append(record)
        return record

    def write(self) -> Path:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        for record in self.tests:
            folder = self.root / record.slug
            folder.mkdir(parents=True, exist_ok=True)
            for number, step in enumerate(record.steps, 1):
                label = re.sub(r"[^A-Za-z0-9]+", "-", step.label).strip("-").lower()[:60]
                body = record.scrub(step.text)
                if step.kind == "command":
                    body = record.scrub(json.dumps(step.extra, indent=2)) + "\n\n" + body
                (folder / f"{number:02d}-{label or step.kind}.txt").write_text(body + "\n")
            (folder / "requests.json").write_text(
                record.scrub(json.dumps(record.requests, indent=2)) + "\n"
            )
        index = self.root / "index.html"
        index.write_text(self._page())
        (self.root / "summary.json").write_text(
            json.dumps(
                {
                    "tests": len(self.tests),
                    "passed": sum(t.outcome == "passed" for t in self.tests),
                    "failed": sum(t.outcome == "failed" for t in self.tests),
                    "skipped": sum(t.outcome == "skipped" for t in self.tests),
                    "results": {t.nodeid: t.outcome for t in self.tests},
                },
                indent=2,
            )
            + "\n"
        )
        return index

    def _page(self) -> str:
        passed = sum(t.outcome == "passed" for t in self.tests)
        failed = sum(t.outcome == "failed" for t in self.tests)
        skipped = sum(t.outcome == "skipped" for t in self.tests)
        items = "\n".join(self._test(t) for t in self.tests)
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started))
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HX E2E Report</title>
<style>{_CSS}</style></head>
<body>
<header>
  <h1>HX end-to-end run</h1>
  <p class="meta">{stamp} &middot; {len(self.tests)} tests &middot;
    <span class="ok">{passed} passed</span> &middot;
    <span class="bad">{failed} failed</span> &middot; {skipped} skipped</p>
  <p class="meta">The real <code>hx</code> binary on a pseudo-terminal, against a local
  OpenRouter stand-in. Screens are what a VT100-compatible terminal showed at each
  checkpoint.</p>
</header>
<nav>{"".join(self._nav(t) for t in self.tests)}</nav>
<main>{items}</main>
</body></html>
"""

    def _nav(self, record: TestRecord) -> str:
        return f'<a class="{record.outcome}" href="#{record.slug}">{html.escape(record.slug)}</a>'

    def _test(self, record: TestRecord) -> str:
        steps = "\n".join(self._step(record, s) for s in record.steps)
        failure = (
            f'<pre class="failure">{html.escape(record.scrub(record.failure))}</pre>'
            if record.failure
            else ""
        )
        requests = ""
        if record.requests:
            requests = (
                "<details><summary>Model requests "
                f"({len(record.requests)})</summary><pre>"
                f"{html.escape(record.scrub(json.dumps(record.requests, indent=2)))}</pre></details>"
            )
        return f"""<section id="{record.slug}" class="test {record.outcome}">
<h2><span class="badge">{record.outcome}</span> {html.escape(record.nodeid)}</h2>
<p class="doc">{html.escape(record.doc)}</p>
<p class="meta">{record.duration:.1f}s</p>
{failure}{steps}{requests}
</section>"""

    def _step(self, record: TestRecord, step: Step) -> str:
        label = html.escape(step.label)
        if step.kind == "screen":
            return f"<figure><figcaption>{label}</figcaption>{record.scrub(step.html)}</figure>"
        if step.kind == "command":
            meta = html.escape(record.scrub(json.dumps(step.extra, indent=2)))
            return (
                f"<figure><figcaption>{label}</figcaption><pre class=meta>{meta}</pre>"
                f'<pre class="out">{html.escape(record.scrub(step.text))}</pre></figure>'
            )
        return f'<p class="note">{label}</p>'


_CSS = """
:root { --bg:#16181d; --panel:#1e2128; --fg:#dcdfe4; --muted:#8b93a1; --ok:#98c379; --bad:#e06c75;
        --line:#2c313a; color-scheme: dark; }
@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) {
  --panel:#f4f5f7; --muted:#5c6370; --line:#d8dbe0; color-scheme: light; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--panel); color:#1f2328; font:14px/1.5 system-ui, sans-serif; }
@media (prefers-color-scheme: dark) { body { color:var(--fg); } }
header, main, nav { max-width: 1100px; margin: 0 auto; padding: 16px; }
h1 { margin: 8px 0; font-size: 22px; }
h2 { font-size: 15px; margin: 0 0 4px; word-break: break-all; }
nav { display:flex; flex-wrap:wrap; gap:6px; }
nav a { font: 12px ui-monospace, monospace; padding:2px 6px; border-radius:4px; max-width:100%;
        border:1px solid var(--line); text-decoration:none; color:inherit; overflow-wrap:anywhere; }
nav a.failed { border-color: var(--bad); color: var(--bad); }
.meta { color: var(--muted); margin: 2px 0; }
.ok { color: var(--ok); } .bad { color: var(--bad); }
.test { border:1px solid var(--line); border-radius:8px; padding:12px; margin:16px 0; }
.test.failed { border-color: var(--bad); }
.badge { font-size: 11px; text-transform: uppercase; padding: 1px 6px; border-radius: 4px;
         background: var(--ok); color: #111; }
.failed .badge { background: var(--bad); }
.doc { margin: 4px 0 8px; white-space: pre-wrap; }
figure { margin: 12px 0; overflow-x: auto; }
figcaption { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
pre { overflow-x: auto; max-width: 100%; }
pre.term { position: relative; margin:0; padding: 0; background:var(--bg); color:var(--fg);
  font: 13px/1.25 ui-monospace, "SF Mono", Menlo, monospace; width: max-content;
  border: 1px solid var(--line); border-radius: 4px; --bg:#16181d; --fg:#dcdfe4; }
.cursor { position:absolute; left: calc(var(--x) * 1ch); top: calc(var(--y) * 1.25em);
  width: 1ch; height: 1.25em; outline: 1px solid #f0c674; pointer-events:none; }
pre.out, pre.failure, details pre { background: var(--bg); color: var(--fg); padding: 8px;
  border-radius: 4px; font: 12px/1.4 ui-monospace, monospace; }
pre.failure { border: 1px solid var(--bad); white-space: pre-wrap; }
@media (max-width: 600px) { header, main, nav { padding: 12px 16px; } }
"""
