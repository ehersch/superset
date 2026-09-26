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

An event-driven control plane that finds machine-verifiable defects in this
repository, files them as issues, and remediates them with Devin sessions
created through the [Devin API](https://docs.devin.ai/api-reference/overview).

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

**Budgets are configuration, not comments.** Sessions carry `max_acu_limit`,
dispatch is capped per run, and CI feedback is capped per PR. When a budget is
exhausted the pipeline escalates (`needs-human`) instead of retrying.

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
| Nightly schedule / manual | `devin-pipeline-scan.yml` | run detectors, file or refresh issues |
| `issues.labeled` with `devin-fix` | `devin-pipeline-dispatch.yml` | create a Devin session for that issue |
| Every 15 min | `devin-pipeline-dispatch.yml` | poll live sessions, settle them, publish the run report |
| `check_suite.completed` = failure | `devin-pipeline-ci-feedback.yml` | send the failing checks back into the owning session |

## Observable output

- **Issue labels** move `devin-fix` → `devin-working` → `devin-fixed` or `needs-human`.
- **Issue comments** record the session link, the ACU cap, the PR, the summary,
  and the verification transcript the session produced.
- **Run report** (job summary and artifact) tables every tracked issue with its
  wave, detector, session, attempt count, CI retries, outcome and PR.
- **Ledger** on the `devin-pipeline-state` branch: one commit per write, so the
  dispatch and settle history is auditable.

## Running it

```bash
pip install -r devin_pipeline/requirements.txt

# See what the detectors find, touching nothing
python -m devin_pipeline.pipeline.cli --dry-run detect
python -m devin_pipeline.pipeline.cli --dry-run detect --only npm_audit --json

# File issues, dispatch, monitor, report (needs GITHUB_TOKEN + DEVIN_API_KEY)
python -m devin_pipeline.pipeline.cli file
python -m devin_pipeline.pipeline.cli dispatch --issue 42
python -m devin_pipeline.pipeline.cli monitor
python -m devin_pipeline.pipeline.cli report --out report.md
```

`--dry-run` (or `DRY_RUN=1`) makes every write a log line, including session
creation, so the whole flow can be rehearsed without an API key.

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
```

The orchestrator tests run the full detect → file → dispatch → monitor →
CI-retry → escalate flow against in-memory GitHub and Devin doubles, and assert
the properties that cost money when they break: filing is idempotent, a live
session is never duplicated, the CI retry budget is enforced, and the ledger
survives a process restart.
