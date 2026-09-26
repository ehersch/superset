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
"""Session prompts and the structured output contract.

Two things make a prompt here different from a chat message: the acceptance
criteria come from the detector rather than from the prose, and the session is
required to answer in a schema the pipeline can branch on. A session that
cannot honestly report ``fixed`` must report ``needs_human`` with a reason —
the pipeline treats an unreachable fix as a routing decision, not a failure.
"""

from __future__ import annotations

from typing import Any

from .models import Evidence

STRUCTURED_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["fixed", "needs_human", "not_reproducible"],
            "description": (
                "fixed only if every acceptance criterion was verified by running it"
            ),
        },
        "pr_url": {
            "type": "string",
            "description": "URL of the opened PR, empty if none",
        },
        "summary": {
            "type": "string",
            "description": "Two sentences: what changed and why",
        },
        "verification": {
            "type": "string",
            "description": "The exact commands run to verify, with their results",
        },
        "blockers": {
            "type": "string",
            "description": "What a human must decide, when outcome is not 'fixed'",
        },
    },
    "required": ["outcome", "summary", "verification"],
}


def build_prompt(
    *,
    repo: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    evidence: Evidence,
    base_branch: str = "master",
) -> str:
    criteria = "\n".join(f"{i}. {c}" for i, c in enumerate(evidence.acceptance, 1))
    return f"""\
You are remediating a single filed issue in the Apache Superset fork `{repo}`.

## Issue #{issue_number}: {issue_title}

{issue_body}

## Reproduction

Run this first and confirm you observe the problem before changing anything:

```bash
{evidence.reproduce}
```

If the problem does not reproduce, stop, open no PR, and report
`outcome: "not_reproducible"` with what you observed instead.

## Acceptance criteria

{criteria}

You must run each criterion, not reason about it. Paste the commands and their
results into the `verification` field.

## Rules

- Branch from `{base_branch}`, named `devin/fix-{evidence.fingerprint}`.
- Scope the diff to this issue. No drive-by refactors, no unrelated formatting,
  no version bumps that the issue does not name.
- Do not weaken, skip, or delete a test to make a check pass. If a test is
  wrong, say so in `blockers` and report `needs_human`.
- Follow `AGENTS.md`: type hints on new Python, TypeScript (never `any`) on the
  frontend, Apache license headers on new files, and `pre-commit run` on the
  staged diff before pushing.
- Open a pull request against `{base_branch}` with a Conventional Commits title
  and the repository PR template. Reference `Fixes #{issue_number}` in the body.
- If you cannot satisfy every criterion, still open the PR as a draft if the
  partial work is useful, and report `needs_human` with the specific decision a
  human owes you.

Report your result in the required structured output.
"""


def build_ci_feedback(
    *, pr_url: str, attempt: int, max_attempts: int, summary: str
) -> str:
    return f"""\
CI is failing on your pull request {pr_url} (retry {attempt} of {max_attempts}).

{summary}

Fix the cause of these failures and push to the same branch. Do not disable,
skip, or weaken the failing checks. If the failures are pre-existing on
`master` and unrelated to your change, prove it (run the same check on the base
commit), say so, and report `needs_human` instead of pushing a workaround.
"""
