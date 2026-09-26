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
"""Mirror curated upstream apache/superset issues into the fork.

The other detectors find their own work. This one carries the human-selected
product bugs (wave C) and the failing-test debt (wave B) that no scanner will
surface, and it enforces the same contract on them: each entry must name a
reproduction command and acceptance criteria that a script can evaluate, so a
mirrored bug is dispatched and verified exactly like a detected one.

Entries are curated deliberately. An upstream issue without a deterministic
reproduction is triage work for a human, not a session prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..models import Finding, Severity
from .base import register

UPSTREAM = "https://github.com/apache/superset/issues/{number}"


@dataclass(frozen=True)
class Curated:
    number: int
    title: str
    severity: Severity
    wave: str
    summary: str
    reproduce: str
    acceptance: tuple[str, ...]
    labels: tuple[str, ...] = ()


CURATED: tuple[Curated, ...] = (
    Curated(
        number=44436,
        title="fix(tests): six restore-version command unit tests fail on master",
        severity=Severity.HIGH,
        wave="B",
        summary=(
            "Six unit tests in the restore-version command suite fail on a clean "
            "checkout of master. They redden a required check for every unrelated "
            "pull request, which trains contributors to ignore the signal."
        ),
        reproduce="pytest tests/unit_tests/commands/ -k restore -q",
        acceptance=(
            "`pytest tests/unit_tests/commands/ -k restore -q` passes",
            "The fix is in the implementation or fixture, not weaker assertions",
            "No other unit test regresses",
        ),
        labels=("tests",),
    ),
    Curated(
        number=44642,
        title=(
            "fix(reports): text reports render -0.00 for negative values "
            "that round to zero"
        ),
        severity=Severity.MODERATE,
        wave="C",
        summary=(
            "A small negative value formatted to two decimals renders as `-0.00` in "
            "text reports. The sign is meaningless at that precision and reads as a "
            "data error to recipients."
        ),
        reproduce=(
            "pytest tests/unit_tests/reports -q  "
            "# add a case asserting -0.001 formats as 0.00"
        ),
        acceptance=(
            "A negative value that rounds to zero renders without a sign",
            "Values that do not round to zero keep their sign",
            "A unit test covers both cases",
        ),
        labels=("reports",),
    ),
    Curated(
        number=44632,
        title=(
            "fix(explore): 'Edit dataset' is disabled for non-Admin users "
            "who can edit the dataset"
        ),
        severity=Severity.HIGH,
        wave="C",
        summary=(
            "Explore disables the 'Edit dataset' action for users who are not Admin "
            "even when the dataset permissions grant them edit rights, so the UI "
            "contradicts the permission the API would allow."
        ),
        reproduce=(
            "npm run test -- --testPathPattern 'explore.*[Dd]ataset' "
            "# in superset-frontend"
        ),
        acceptance=(
            "The action is enabled exactly when the dataset's own edit "
            "permission is granted",
            "Admin behaviour is unchanged",
            "A Jest test covers the non-Admin-with-permission case",
        ),
        labels=("explore",),
    ),
    Curated(
        number=44304,
        title=(
            "fix(dashboard): duplicate slug save fails with a generic "
            "'unknown reason' error"
        ),
        severity=Severity.MODERATE,
        wave="C",
        summary=(
            "Saving a dashboard with a slug that is already taken surfaces a generic "
            "failure instead of naming the conflict, leaving the user to guess which "
            "field is wrong."
        ),
        reproduce="pytest tests/unit_tests/dashboards -q -k slug",
        acceptance=(
            "A duplicate slug returns a validation error that names the slug field",
            "The frontend renders that message instead of the generic fallback",
            "A test asserts the specific error",
        ),
        labels=("dashboard",),
    ),
    Curated(
        number=44305,
        title=(
            "fix(dashboard): adding a chart does not update the dashboard's "
            "Last modified"
        ),
        severity=Severity.LOW,
        wave="C",
        summary=(
            "Adding a chart to a dashboard mutates the dashboard but leaves "
            "`changed_on` untouched, so the dashboard list sorts and displays a stale "
            "modification time."
        ),
        reproduce="pytest tests/unit_tests/dashboards -q -k changed_on",
        acceptance=(
            "Adding a chart updates the dashboard's `changed_on`",
            "The dashboard list reflects the new timestamp",
            "A test asserts the timestamp moves",
        ),
        labels=("dashboard",),
    ),
    Curated(
        number=42617,
        title=(
            "fix(i18n): alert/report type is interpolated untranslated into "
            "localized toasts"
        ),
        severity=Severity.LOW,
        wave="C",
        summary=(
            "Localized toast messages interpolate the literal English word 'Alert' or "
            "'Report' into an otherwise translated sentence, producing mixed-language "
            "output in every non-English locale."
        ),
        reproduce=(
            "npm run test -- --testPathPattern 'alert.*toast|report.*toast' "
            "# in superset-frontend"
        ),
        acceptance=(
            "The type word is translated, or the sentence has per-type ids",
            "English output is unchanged",
            "A Jest test covers a non-English locale",
        ),
        labels=("i18n",),
    ),
)


@register("upstream_mirror")
def detect(repo: Path) -> Iterator[Finding]:
    del repo  # the curated set is independent of the working tree
    for entry in CURATED:
        url = UPSTREAM.format(number=entry.number)
        yield Finding(
            detector="upstream_mirror",
            key=f"upstream:{entry.number}",
            title=entry.title,
            severity=entry.severity,
            wave=entry.wave,
            labels=["devin-fix", *entry.labels],
            body=(f"{entry.summary}\n\nMirrored from upstream {url}."),
            reproduce=entry.reproduce,
            acceptance=list(entry.acceptance),
            upstream_issue=entry.number,
            metadata={"upstream_url": url},
        )
