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
"""Core value types shared by the detectors, the dispatcher and the monitor."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

EVIDENCE_OPEN = "<!-- devin-pipeline:evidence"
EVIDENCE_CLOSE = "-->"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"


class Outcome(str, Enum):
    """Terminal classification of a remediation attempt."""

    FIXED = "fixed"
    NEEDS_HUMAN = "needs_human"
    NOT_REPRODUCIBLE = "not_reproducible"


@dataclass(frozen=True)
class Finding:
    """One machine-verifiable defect, as emitted by a detector.

    ``fingerprint`` is what makes the pipeline idempotent: a detector that runs
    every night must recognise the issue it filed yesterday instead of opening a
    duplicate, and must be able to say "this finding is gone" once a fix lands.
    """

    detector: str
    key: str
    title: str
    severity: Severity
    body: str
    wave: str
    reproduce: str
    acceptance: list[str]
    labels: list[str] = field(default_factory=list)
    upstream_issue: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(f"{self.detector}:{self.key}".encode()).hexdigest()
        return digest[:16]

    def evidence_block(self) -> str:
        """A machine-readable block embedded in the issue body.

        The dispatcher reads it back off the issue, so an issue opened by a
        detector and an issue opened by hand are handled by the same code path
        as long as the block is present.
        """
        payload = {
            "fingerprint": self.fingerprint,
            "detector": self.detector,
            "key": self.key,
            "wave": self.wave,
            "severity": self.severity.value,
            "reproduce": self.reproduce,
            "acceptance": self.acceptance,
            "upstream_issue": self.upstream_issue,
            "metadata": self.metadata,
        }
        return f"{EVIDENCE_OPEN}\n{json.dumps(payload, indent=2)}\n{EVIDENCE_CLOSE}"

    def issue_body(self) -> str:
        acceptance = "\n".join(f"- [ ] {item}" for item in self.acceptance)
        return (
            f"{self.body.strip()}\n\n"
            f"## Reproduce\n\n```bash\n{self.reproduce.strip()}\n```\n\n"
            f"## Acceptance criteria\n\n{acceptance}\n\n"
            f"---\n"
            f"Filed automatically by the Devin remediation pipeline "
            f"(detector `{self.detector}`). Label this issue `devin-fix` to "
            f"dispatch a Devin session.\n\n"
            f"{self.evidence_block()}"
        )


@dataclass
class Evidence:
    """The parsed form of a :meth:`Finding.evidence_block`."""

    fingerprint: str
    detector: str
    key: str
    wave: str
    severity: str
    reproduce: str
    acceptance: list[str]
    upstream_issue: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, body: str) -> "Evidence | None":
        start = body.find(EVIDENCE_OPEN)
        if start == -1:
            return None
        end = body.find(EVIDENCE_CLOSE, start)
        if end == -1:
            return None
        raw = body[start + len(EVIDENCE_OPEN) : end].strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return cls(
            fingerprint=payload["fingerprint"],
            detector=payload["detector"],
            key=payload["key"],
            wave=payload.get("wave", "unknown"),
            severity=payload.get("severity", "low"),
            reproduce=payload.get("reproduce", ""),
            acceptance=payload.get("acceptance", []),
            upstream_issue=payload.get("upstream_issue"),
            metadata=payload.get("metadata", {}),
        )


@dataclass
class Attempt:
    """One Devin session dispatched against one issue."""

    session_id: str
    session_url: str
    created_at: str
    status: str = "working"
    pr_url: str | None = None
    outcome: str | None = None
    summary: str | None = None
    verification: str | None = None
    blockers: str | None = None
    ci_retries: int = 0
    finished_at: str | None = None
    # Digest of the structured output this attempt was settled on, so a
    # session reopened for a CI retry is not re-settled on its stale result.
    result_digest: str | None = None


@dataclass
class IssueRecord:
    """Pipeline state for a single issue, persisted between runs."""

    issue_number: int
    fingerprint: str
    detector: str
    wave: str
    title: str
    severity: str
    attempts: list[Attempt] = field(default_factory=list)
    escalated: bool = False

    @property
    def latest(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IssueRecord":
        attempts = [Attempt(**a) for a in data.get("attempts", [])]
        return cls(
            issue_number=data["issue_number"],
            fingerprint=data["fingerprint"],
            detector=data["detector"],
            wave=data["wave"],
            title=data["title"],
            severity=data["severity"],
            attempts=attempts,
            escalated=data.get("escalated", False),
        )


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
