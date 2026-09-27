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
"""Runtime configuration, read from the environment.

Budgets are configuration rather than constants because the failure mode this
pipeline has to avoid is not a wrong patch — a wrong patch is caught by review
— but an unbounded spend loop: sessions retrying CI forever, or a detector
filing a hundred issues and dispatching all of them at once.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DISPATCH_LABEL = "devin-fix"
IN_PROGRESS_LABEL = "devin-working"
DONE_LABEL = "devin-fixed"
ESCALATE_LABEL = "needs-human"

LABELS = (
    (DISPATCH_LABEL, "5319e7", "Dispatch a Devin session to remediate this issue"),
    (IN_PROGRESS_LABEL, "fbca04", "A Devin session is currently working on this issue"),
    (DONE_LABEL, "0e8a16", "Remediated by Devin; a pull request is open"),
    (
        ESCALATE_LABEL,
        "b60205",
        "Devin could not complete this; a human decision is required",
    ),
)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw and raw.strip() else default


@dataclass
class Config:
    repo: str = field(
        default_factory=lambda: os.environ.get("TARGET_REPO", "ehersch/superset")
    )
    base_branch: str = field(
        default_factory=lambda: os.environ.get("BASE_BRANCH", "master")
    )
    repo_path: Path = field(
        default_factory=lambda: Path(os.environ.get("REPO_PATH", ".")).resolve()
    )
    state_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("PIPELINE_STATE", ".devin-pipeline/state.json")
        )
    )
    github_token: str = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN", "")
    )
    devin_api_key: str = field(
        default_factory=lambda: os.environ.get("DEVIN_API_KEY", "")
    )
    devin_base_url: str = field(
        default_factory=lambda: os.environ.get("DEVIN_BASE_URL", "https://api.devin.ai")
    )
    # Budgets
    max_dispatch_per_run: int = field(
        default_factory=lambda: _int("MAX_DISPATCH_PER_RUN", 5)
    )
    max_acu_per_session: int = field(
        default_factory=lambda: _int("MAX_ACU_PER_SESSION", 40)
    )
    max_ci_retries: int = field(default_factory=lambda: _int("MAX_CI_RETRIES", 2))
    # Unattended approval. 0 keeps the `devin-fix` label as the only gate; a
    # positive limit lets a scheduled run label that many unclaimed issues per
    # run itself, provided they are at least this severe.
    auto_approve_limit: int = field(
        default_factory=lambda: _int("AUTO_APPROVE_LIMIT", 0)
    )
    auto_approve_min_severity: str = field(
        default_factory=lambda: os.environ.get("AUTO_APPROVE_MIN_SEVERITY", "high")
    )
    max_in_flight: int = field(default_factory=lambda: _int("MAX_IN_FLIGHT", 5))
    max_issues_per_detector: int = field(
        default_factory=lambda: _int("MAX_ISSUES_PER_DETECTOR", 6)
    )
    dry_run: bool = field(
        default_factory=lambda: (
            os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
        )
    )
