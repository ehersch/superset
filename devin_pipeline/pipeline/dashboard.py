# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""A single-file HTML dashboard rendered from the pipeline ledger.

The ledger is the only input, so the same page can be produced by a workflow
run, by a local run against a pulled ledger, or from a ledger committed to the
state branch. No JavaScript dependencies and no server: the output is an
artifact that can be opened from a laptop or published to Pages.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from .models import Attempt, IssueRecord, Outcome, utcnow

_STATUS_CLASS = {
    Outcome.FIXED.value: "ok",
    Outcome.NEEDS_HUMAN.value: "warn",
    Outcome.NOT_REPRODUCIBLE.value: "warn",
}

_CSS = """
:root {
  --bg: #0f1115; --panel: #171a21; --line: #272c37; --text: #e6e9ef;
  --muted: #9aa3b2; --ok: #3fb950; --warn: #d29922; --live: #58a6ff;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 32px 24px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 36px 0 12px; color: var(--muted);
     text-transform: uppercase; letter-spacing: .08em; }
.sub { color: var(--muted); margin: 0 0 28px; }
.sub a { color: var(--live); }
.cards { display: grid; gap: 12px;
         grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
.card { background: var(--panel); border: 1px solid var(--line);
        border-radius: 10px; padding: 16px 18px; }
.card .n { font-size: 30px; font-weight: 600; letter-spacing: -.02em; }
.card .l { color: var(--muted); font-size: 12px; margin-top: 2px; }
.card.ok .n { color: var(--ok); }
.card.warn .n { color: var(--warn); }
.card.live .n { color: var(--live); }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-size: 12px; color: var(--muted); font-weight: 500;
     padding: 8px 10px; border-bottom: 1px solid var(--line); }
td { padding: 10px; border-bottom: 1px solid var(--line);
     vertical-align: top; }
tr:last-child td { border-bottom: none; }
a { color: var(--live); text-decoration: none; }
a:hover { text-decoration: underline; }
.pill { display: inline-block; padding: 1px 9px; border-radius: 999px;
        font-size: 12px; border: 1px solid var(--line); color: var(--muted); }
.pill.ok { color: var(--ok); border-color: #1d4429; background: #12261a; }
.pill.warn { color: var(--warn); border-color: #4a3a12; background: #241d0d; }
.pill.live { color: var(--live); border-color: #1b3a5c; background: #10202f; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 12px; }
.trace { color: var(--muted); max-width: 520px; }
details summary { cursor: pointer; color: var(--live); font-size: 12px;
                  margin-top: 6px; }
pre { background: #0b0d11; border: 1px solid var(--line); border-radius: 6px;
      padding: 10px; overflow-x: auto; font-size: 12px; white-space: pre-wrap;
      margin: 8px 0 0; }
.bars { display: flex; align-items: flex-end; gap: 6px; height: 120px;
        background: var(--panel); border: 1px solid var(--line);
        border-radius: 10px; padding: 16px; }
.bar { flex: 1; display: flex; flex-direction: column; justify-content: flex-end;
       align-items: center; gap: 4px; height: 100%; }
.bar i { display: block; width: 100%; background: var(--live);
         border-radius: 3px 3px 0 0; }
.bar i.settled { background: var(--ok); }
.bar span { font-size: 10px; color: var(--muted); }
.empty { color: var(--muted); background: var(--panel);
         border: 1px solid var(--line); border-radius: 10px; padding: 20px; }
"""


def _pill(label: str, kind: str) -> str:
    return f'<span class="pill {kind}">{escape(label)}</span>'


def _status_cell(attempt: Attempt | None) -> str:
    if attempt is None:
        return _pill("not dispatched", "")
    if attempt.finished_at is None:
        return _pill(attempt.status or "working", "live")
    outcome = attempt.outcome or Outcome.NEEDS_HUMAN.value
    return _pill(outcome, _STATUS_CLASS.get(outcome, "warn"))


def _duration(attempt: Attempt | None) -> str:
    if attempt is None:
        return "—"
    end = attempt.finished_at or utcnow()
    delta = datetime.fromisoformat(end) - datetime.fromisoformat(attempt.created_at)
    return f"{delta.total_seconds() / 60:.0f}m"


def _trace(attempt: Attempt | None) -> str:
    """One-line summary of what the session did, with its evidence folded away."""
    if attempt is None:
        return "—"
    headline = attempt.summary or attempt.blockers or "session in progress"
    out = [f'<div class="trace">{escape(headline)}</div>']
    if attempt.verification:
        out.append(
            "<details><summary>verification</summary>"
            f"<pre>{escape(attempt.verification[:4000])}</pre></details>"
        )
    return "".join(out)


def _hour(ts: str) -> str:
    return datetime.fromisoformat(ts).strftime("%m-%d %H:00")


def _throughput(records: list[IssueRecord]) -> str:
    """Dispatched vs. settled sessions per hour — the progress signal."""
    buckets: dict[str, list[int]] = {}
    for record in records:
        for attempt in record.attempts:
            buckets.setdefault(_hour(attempt.created_at), [0, 0])[0] += 1
            if attempt.finished_at:
                buckets.setdefault(_hour(attempt.finished_at), [0, 0])[1] += 1
    if not buckets:
        return '<div class="empty">No sessions dispatched yet.</div>'
    peak = max(max(pair) for pair in buckets.values()) or 1
    bars = []
    for label in sorted(buckets)[-24:]:
        dispatched, settled = buckets[label]
        bars.append(
            f'<div class="bar" title="{escape(label)}: {dispatched} dispatched, '
            f'{settled} settled">'
            f'<i class="settled" style="height:{settled / peak * 100:.0f}%"></i>'
            f'<i style="height:{dispatched / peak * 100:.0f}%"></i>'
            f"<span>{escape(label[-5:])}</span></div>"
        )
    return f'<div class="bars">{"".join(bars)}</div>'


def render(repo: str, records: list[IssueRecord], stats: dict[str, Any]) -> str:
    """Render the ledger as a standalone HTML page."""
    rows = []
    for record in sorted(records, key=lambda r: r.issue_number):
        attempt = record.latest
        issue_url = f"https://github.com/{repo}/issues/{record.issue_number}"
        session = (
            f'<a class="mono" href="{attempt.session_url}">'
            f"{escape(attempt.session_id[:12])}</a>"
            if attempt
            else "—"
        )
        pr = (
            f'<a href="{attempt.pr_url}">#{attempt.pr_url.rsplit("/", 1)[-1]}</a>'
            if attempt and attempt.pr_url
            else "—"
        )
        rows.append(
            "<tr>"
            f'<td><a href="{issue_url}">#{record.issue_number}</a><br>'
            f'<span class="mono trace">{escape(record.title[:64])}</span></td>'
            f'<td class="mono">{escape(record.detector)}</td>'
            f"<td>{_status_cell(attempt)}</td>"
            f"<td>{pr}</td>"
            f"<td>{session}</td>"
            f"<td>{_duration(attempt)}</td>"
            f"<td>{attempt.ci_retries if attempt else 0}</td>"
            f"<td>{_trace(attempt)}</td>"
            "</tr>"
        )

    median = stats["median_minutes_to_settle"]
    cards = [
        ("live", stats["in_flight"], "sessions in flight"),
        ("ok", stats["fixed_with_pr"], "fixed with a PR"),
        ("warn", stats["escalated"], "escalated to a human"),
        (
            "ok" if stats["autonomous_resolution_rate"] >= 0.5 else "warn",
            f"{stats['autonomous_resolution_rate'] * 100:.0f}%",
            "autonomous resolution rate",
        ),
        ("", f"{median}m" if median is not None else "—", "median time to settle"),
        ("", stats["ci_retries_spent"], "CI retries spent"),
        ("", stats["dispatched"], "sessions dispatched"),
        ("", stats["issues_tracked"], "issues tracked"),
    ]
    card_html = "".join(
        f'<div class="card {kind}"><div class="n">{value}</div>'
        f'<div class="l">{label}</div></div>'
        for kind, value, label in cards
    )
    table = (
        "<table><thead><tr><th>Issue</th><th>Detector</th><th>Status</th>"
        "<th>PR</th><th>Session</th><th>Elapsed</th><th>CI retries</th>"
        "<th>What the session did</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        if rows
        else '<div class="empty">No issues tracked yet.</div>'
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Devin remediation pipeline — {escape(repo)}</title>
<style>{_CSS}</style></head><body><div class="wrap">
<h1>Devin remediation pipeline</h1>
<p class="sub"><a href="https://github.com/{escape(repo)}">{escape(repo)}</a>
 · generated {escape(utcnow())}</p>
<div class="cards">{card_html}</div>
<h2>Throughput — sessions dispatched (blue) vs. settled (green), per hour</h2>
{_throughput(records)}
<h2>Per-issue ledger</h2>
{table}
</div></body></html>
"""
