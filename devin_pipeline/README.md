<!--
 Licensed to the Apache Software Foundation (ASF) under one
 or more contributor license agreements.  See the NOTICE file
 distributed with this work for additional information
 regarding copyright ownership.  The ASF licenses this file
 to you under the Apache License, Version 2.0 (the
 "License"); you may not use this file except in compliance
 with the License.  You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing,
 software distributed under the License is distributed on an
 "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 KIND, either express or implied.  See the License for the
 specific language governing permissions and limitations
 under the License.
-->
# Devin remediation pipeline

An event-driven control plane that finds machine-verifiable defects in a target
repository, files them as issues, and remediates them with Devin sessions
created through the [Devin API](https://docs.devin.ai/api-reference/overview).

It runs against [Apache Superset](https://github.com/apache/superset). The live
results are in the fork it operates on, <https://github.com/ehersch/superset>:
the [issues it filed](https://github.com/ehersch/superset/issues) and the
[pull requests Devin opened against them](https://github.com/ehersch/superset/pulls).

**New here?** [`docs/DEMO.md`](docs/DEMO.md) is the two-minute version: what the
system is for, what is live right now, and what to click in what order.

```
 detectors ──▶ issues (evidence block) ──▶ label `devin-fix` ──▶ Devin session
     ▲                                                               │
     │                                                          PR + checks
     │                                                               │
 run report ◀── monitor (structured output) ◀── CI failure fed back ─┘
```

## Why it is shaped this way

**A finding is only worth automating if a script can tell when it is fixed.**
Every detector must emit a reproduction command and acceptance criteria; the
same strings become the issue checklist, the session prompt, and the bar the
session has to clear before it may report `fixed`. A defect that cannot be
stated that way is triage work for a human and never enters the pipeline.

**The issue is the interface.** Each filed issue carries a JSON evidence block
(`<!-- devin-pipeline:evidence ... -->`) holding the fingerprint, reproduction
and acceptance criteria. The dispatcher reads that block back off the issue, so
a hand-written issue with a valid block is dispatched by exactly the same code
path as a detected one — and an issue without one is refused with an
explanation rather than turned into a vague prompt.

**Labelling is the approval step.** Detectors file freely; nothing spends an
ACU until an issue carries `devin-fix`. That keeps a noisy scan from becoming a
spend incident and gives a human a natural place to intervene.

**Unattended approval is a policy, not a bypass.** A standing policy may add
`devin-fix` itself — when an issue is opened, and in the nightly run for any
backlog — to unclaimed findings at or above a severity floor
(`AUTO_APPROVE_MIN_SEVERITY`, default `high`), at most `AUTO_APPROVE_LIMIT` per
run (default 3 in the workflows, 0 in the CLI) and never while `MAX_IN_FLIGHT`
sessions (default 5) are already live. It goes through the same label, comments
the decision on the issue, and never touches anything a human has claimed,
escalated or already approved — so an evidence-backed issue becomes a session
within a minute of being filed, while the label stays the single place to veto
(`AUTO_APPROVE_LIMIT=0` makes it the only gate again) or fast-track.

**Budgets are configuration, not comments.** Sessions carry `max_acu_limit`,
dispatch is capped per run, and CI feedback is capped per PR. When a budget is
exhausted the pipeline escalates (`needs-human`) instead of retrying.

## Installing it into a repository

The three workflows in `.github/workflows/` are the event surface; copy them
into the repository the pipeline should operate on, add a `DEVIN_API_KEY`
Actions secret, and vendor this package (or `pip install` it) so
`python -m devin_pipeline.pipeline.cli` resolves. Everything else — the ledger
branch, the labels, the dashboard branch — is created on first run.

The detectors read a checkout of the target repository (`REPO_PATH`), so the
workflows check that repository out and point the CLI at it; nothing about the
control plane is Superset-specific except the detector set.

## Detectors

| Detector | Wave | Finds | Verified by |
| --- | --- | --- | --- |
| `npm_audit` | A | Frontend advisories, grouped by the upgrade that clears them | `npm audit --package-lock-only` |
| `osv_python` | A | Pinned PyPI packages with OSV advisories | OSV batch query over `requirements/base.txt` |
| `i18n_placeholders` | B | Translations whose `%(name)s` / `{}` placeholders disagree with the English source (these raise at interpolation time) | the detector itself |
| `engine_spec_metadata` | B | Engine specs with no `engine_name` / `sqlalchemy_uri_placeholder`, so the connection modal shows a generic URI | the detector itself |
| `upstream_mirror` | B, C | Curated upstream `apache/superset` bugs that no scanner surfaces | the reproduction named per entry |

Advisories are grouped by *the upgrade that fixes them* rather than filed one
per CVE: the twenty advisories on master collapse into four upgrades, and four
reviewable PRs beat twenty.

## Triggers

| Event | Workflow | Action |
| --- | --- | --- |
| Nightly schedule / manual | `devin-pipeline-scan.yml` | run detectors, file or refresh issues, auto-approve up to `AUTO_APPROVE_LIMIT` findings, dispatch, settle, publish |
| `issues.opened` | `devin-pipeline-dispatch.yml` | if the issue carries an evidence block that meets the policy and budgets allow, label it `devin-fix` and create a Devin session |
| `issues.labeled` with `devin-fix` | `devin-pipeline-dispatch.yml` | create a Devin session for that issue |
| Every 15 min | `devin-pipeline-dispatch.yml` | poll live sessions, settle them, publish the run report |
| `check_suite.completed` = failure | `devin-pipeline-ci-feedback.yml` | send the failing checks back into the owning session |

## Observable output

- **Issue labels** move `devin-fix` → `devin-working` → `devin-fixed` or `needs-human`.
- **Issue comments** record the session link, the ACU cap, the PR, the summary,
  and the verification transcript the session produced.
- **Run report** (job summary and artifact) opens with a scoreboard —
  dispatched, in flight, fixed with a PR, escalated, not reproducible,
  autonomous resolution rate, CI retries spent, median minutes to settle — then
  tables every tracked issue with its wave, detector, session, attempt count,
  CI retries, outcome and PR.
- **`metrics.json`** (same artifact) carries those figures as JSON, so they can
  be scraped into a dashboard instead of read by eye.
- **Status dashboard** — a standalone `index.html` published to the `gh-pages`
  branch on every run. It is the answer to "how would an engineering leader
  know this is working?": sessions in flight, fixed with a PR, escalated,
  autonomous resolution rate, median time to settle and CI retries as headline
  numbers; a per-hour throughput chart of sessions dispatched vs. settled; and
  a per-issue table giving each session's status, PR, elapsed time, one-line
  trace summary and the verification transcript it ran to prove the fix.
- **Ledger** on the `devin-pipeline-state` branch: one commit per write, so the
  dispatch and settle history is auditable.

Every one of those is derived from the ledger alone, so the dashboard can be
rebuilt at any time from a state file — nothing has to be scraped back out of
GitHub or the Devin API.

## Running it

A guided, step-by-step walkthrough (what to run, what to look at, and why each
step exists) is in [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md).

```bash
pip install -r devin_pipeline/requirements.txt

# See what the detectors find, touching nothing
python -m devin_pipeline.pipeline.cli --dry-run detect
python -m devin_pipeline.pipeline.cli --dry-run detect --only npm_audit --json
# …or as a page: --html findings.html, or --serve to read it at localhost:8000

# File issues, dispatch, monitor, report (needs GITHUB_TOKEN + DEVIN_API_KEY)
python -m devin_pipeline.pipeline.cli file
python -m devin_pipeline.pipeline.cli dispatch --issue 42
python -m devin_pipeline.pipeline.cli monitor
python -m devin_pipeline.pipeline.cli report --out report.md
python -m devin_pipeline.pipeline.cli metrics --out metrics.json
python -m devin_pipeline.pipeline.cli dashboard --out index.html
```

`dashboard`, `report` and `metrics` read the ledger only — no API key needed,
so a reviewer can regenerate the status page from a pulled state file.

Dispatch is gated on the label wherever it is invoked from: `dispatch --issue`
refuses an issue that is not labelled `devin-fix`, and refuses one still
labelled `needs-human`, so neither a detector nor a stray CLI call can spend a
session that was not approved.

`approve` is the unattended approver: `approve --limit 3 --min-severity high`
labels up to three unclaimed, evidence-bearing issues `devin-fix` and comments
why, leaving `dispatch` to start their sessions. With no limit it is a no-op.

`--dry-run` (or `DRY_RUN=1`) makes every write a log line, including session
creation, so the whole flow can be rehearsed without an API key.

### In Docker

The image carries Python and the Node toolchain the `npm_audit` detector shells
out to, so a run needs nothing installed on the host:

```bash
docker build -t devin-pipeline .

# Rehearse the detectors against a checkout mounted read-only
docker run --rm -v "$PWD:/repo:ro" devin-pipeline --dry-run detect

# Drive the real thing; the ledger lives on the mounted volume
docker run --rm -e GITHUB_TOKEN -e DEVIN_API_KEY \
  -e TARGET_REPO=ehersch/superset \
  -v "$PWD:/repo:ro" -v "$PWD/.devin-pipeline:/state" \
  devin-pipeline dispatch --issue 15
```

#### One-shot end-to-end run

`run` is the whole loop in a single container: detect the issues, file them,
create a Devin session per approved issue (`--approve N` first approves up to
`N` unclaimed findings itself), record each session id in the
ledger, then poll every session until it settles and write the dashboard. It
is what the GitHub triggers do across separate events, collapsed into one
process so the system can be demonstrated without a webhook receiver.

```bash
docker run --rm -e GITHUB_TOKEN -e DEVIN_API_KEY \
  -e TARGET_REPO=ehersch/superset \
  -v "$PWD:/repo:ro" -v "$PWD/.devin-pipeline:/state" \
  devin-pipeline -v run --poll-interval 60 --timeout 3600 \
  --dashboard-out /state/dashboard.html
```

It prints each session URL as it is created, logs every issue as it settles,
and ends on the metrics JSON. Add `--dry-run` to walk the same path with no
GitHub or Devin writes.

```text
INFO devin_pipeline.run: filed 3 new issue(s)
INFO devin_pipeline.run: session devin-6a7310… -> https://app.devin.ai/sessions/6a7310…
INFO devin_pipeline.run: dispatched 3 session(s)
INFO devin_pipeline.run: settled issue #14
{"issues_tracked": 10, "in_flight": 0, "fixed_with_pr": 7, …}
```

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TARGET_REPO` | `ehersch/superset` | repository to file against |
| `REPO_PATH` | `.` | checkout the detectors scan |
| `PIPELINE_STATE` | `.devin-pipeline/state.json` | ledger location |
| `MAX_DISPATCH_PER_RUN` | `5` | sessions started per run |
| `MAX_ACU_PER_SESSION` | `40` | per-session ACU cap |
| `MAX_CI_RETRIES` | `2` | CI failures fed back before escalating |
| `MAX_ISSUES_PER_DETECTOR` | `6` | issues one detector may file per run |

Secrets required in the repository: `DEVIN_API_KEY`. `GITHUB_TOKEN` is supplied
by Actions.

## Tests

```bash
pytest devin_pipeline/tests -q
ruff check devin_pipeline
mypy devin_pipeline --ignore-missing-imports
```

The orchestrator tests run the full detect → file → dispatch → monitor →
CI-retry → escalate flow against in-memory GitHub and Devin doubles, and assert
the properties that cost money when they break: filing is idempotent, a live
session is never duplicated, the CI retry budget is enforced, and the ledger
survives a process restart.

## Architecture

![architecture](docs/architecture.png)

Regenerate with `pip install graphviz && python docs/architecture.py` (needs the
`graphviz` system package for `dot`).
