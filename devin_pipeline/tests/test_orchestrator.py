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
"""End-to-end tests of the control plane against fake GitHub and Devin APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from devin_pipeline.pipeline.config import (
    Config,
    DISPATCH_LABEL,
    DONE_LABEL,
    ESCALATE_LABEL,
    IN_PROGRESS_LABEL,
)
from devin_pipeline.pipeline.models import Finding, Severity
from devin_pipeline.pipeline.orchestrator import Pipeline


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[str]] = {}
        self.labels: dict[int, set[str]] = {}
        self.next_number = 1
        self.check_summary = ""

    def ensure_label(self, name: str, color: str, description: str) -> None:
        return None

    def iter_issues(
        self, state: str = "open", labels: str | None = None
    ) -> Iterator[dict[str, Any]]:
        for number, issue in self.issues.items():
            if labels and labels not in self.labels.get(number, set()):
                continue
            yield issue

    def get_issue(self, number: int) -> dict[str, Any]:
        issue = dict(self.issues[number])
        issue["labels"] = [{"name": name} for name in self.labels.get(number, set())]
        return issue

    def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        number = self.next_number
        self.next_number += 1
        issue = {"number": number, "title": title, "body": body}
        self.issues[number] = issue
        self.labels[number] = set(labels or [])
        return issue

    def update_issue(self, number: int, **fields: Any) -> dict[str, Any]:
        self.issues[number].update(fields)
        return self.issues[number]

    def comment(self, number: int, body: str) -> dict[str, Any]:
        self.comments.setdefault(number, []).append(body)
        return {}

    def add_labels(self, number: int, labels: list[str]) -> None:
        self.labels.setdefault(number, set()).update(labels)

    def remove_label(self, number: int, label: str) -> None:
        self.labels.setdefault(number, set()).discard(label)

    def failed_check_summary(self, ref: str, limit: int = 3) -> str:
        return self.check_summary


class FakeDevin:
    def __init__(self, session: dict[str, Any] | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.messages: list[tuple[str, str]] = []
        self.session = session or {"status_enum": "working"}

    def create_session(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.created.append({"prompt": prompt, **kwargs})
        session_id = f"devin-{len(self.created)}"
        return {
            "session_id": session_id,
            "url": f"https://app.devin.ai/sessions/{session_id}",
            "is_new_session": True,
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, **self.session}

    def send_message(self, session_id: str, message: str) -> None:
        self.messages.append((session_id, message))


def build(tmp_path: Path, devin: FakeDevin) -> tuple[Pipeline, FakeGitHub]:
    config = Config(
        repo="ehersch/superset",
        repo_path=tmp_path,
        state_path=tmp_path / "state.json",
        github_token="x",  # noqa: S106
        devin_api_key="x",  # noqa: S106
        dry_run=True,
    )
    pipeline = Pipeline(config)
    github = FakeGitHub()
    pipeline.github = github  # type: ignore[assignment]
    pipeline.devin = devin  # type: ignore[assignment]
    return pipeline, github


def finding(key: str = "k1") -> Finding:
    return Finding(
        detector="npm_audit",
        key=key,
        title=f"security(frontend): upgrade {key}",
        severity=Severity.CRITICAL,
        body="body",
        wave="A",
        reproduce="npm audit --package-lock-only",
        acceptance=["no advisories remain"],
        labels=["devin-fix", "security"],
    )


def test_filing_is_idempotent_across_runs(tmp_path: Path) -> None:
    pipeline, github = build(tmp_path, FakeDevin())
    assert len(pipeline.file_issues([finding()])) == 1
    assert len(pipeline.file_issues([finding()])) == 0
    assert len(github.issues) == 1


def test_refiling_refreshes_a_changed_body_without_opening_a_duplicate(
    tmp_path: Path,
) -> None:
    pipeline, github = build(tmp_path, FakeDevin())
    pipeline.file_issues([finding()])
    updated = Finding(**{**finding().__dict__, "body": "12 packages, was 9"})
    pipeline.file_issues([updated])
    assert len(github.issues) == 1
    assert "12 packages" in github.issues[1]["body"]


def test_dispatch_creates_one_session_and_moves_the_labels(tmp_path: Path) -> None:
    devin = FakeDevin()
    pipeline, github = build(tmp_path, devin)
    pipeline.file_issues([finding()])
    github.add_labels(1, [DISPATCH_LABEL])

    attempt = pipeline.dispatch_issue(1)

    assert attempt is not None
    assert len(devin.created) == 1
    assert DISPATCH_LABEL not in github.labels[1]
    assert IN_PROGRESS_LABEL in github.labels[1]
    prompt = devin.created[0]["prompt"]
    assert "no advisories remain" in prompt
    assert "npm audit --package-lock-only" in prompt
    assert devin.created[0]["max_acu_limit"] == pipeline.config.max_acu_per_session


def test_dispatch_does_not_start_a_second_session_while_one_is_live(
    tmp_path: Path,
) -> None:
    devin = FakeDevin()
    pipeline, github = build(tmp_path, devin)
    pipeline.file_issues([finding()])
    github.add_labels(1, [DISPATCH_LABEL])
    pipeline.dispatch_issue(1)
    github.add_labels(1, [DISPATCH_LABEL])
    pipeline.dispatch_issue(1)
    assert len(devin.created) == 1


def test_filing_never_applies_the_approval_label(tmp_path: Path) -> None:
    pipeline, github = build(tmp_path, FakeDevin())
    pipeline.file_issues([finding()])
    assert DISPATCH_LABEL not in github.labels[1]


def test_dispatch_refuses_an_unlabelled_issue(tmp_path: Path) -> None:
    devin = FakeDevin()
    pipeline, _ = build(tmp_path, devin)
    pipeline.file_issues([finding()])
    assert pipeline.dispatch_issue(1) is None
    assert devin.created == []


def test_dispatch_refuses_an_issue_a_human_escalated(tmp_path: Path) -> None:
    devin = FakeDevin()
    pipeline, github = build(tmp_path, devin)
    pipeline.file_issues([finding()])
    github.add_labels(1, [DISPATCH_LABEL, ESCALATE_LABEL])
    assert pipeline.dispatch_issue(1) is None
    assert devin.created == []
    assert DISPATCH_LABEL not in github.labels[1]


def test_dispatch_refuses_an_issue_without_evidence(tmp_path: Path) -> None:
    devin = FakeDevin()
    pipeline, github = build(tmp_path, devin)
    github.create_issue("hand written", "please fix the thing", labels=[DISPATCH_LABEL])
    assert pipeline.dispatch_issue(1) is None
    assert not devin.created
    assert "no evidence block" in github.comments[1][0]


def test_monitor_marks_a_verified_fix_done_with_the_pr(tmp_path: Path) -> None:
    devin = FakeDevin(
        {
            "status_enum": "finished",
            "structured_output": {
                "outcome": "fixed",
                "pr_url": "https://github.com/ehersch/superset/pull/7",
                "summary": "bumped po2json",
                "verification": "npm audit -> 0 advisories",
            },
        }
    )
    pipeline, github = build(tmp_path, devin)
    pipeline.file_issues([finding()])
    github.add_labels(1, [DISPATCH_LABEL])
    pipeline.dispatch_issue(1)

    settled = pipeline.monitor()

    assert len(settled) == 1
    assert DONE_LABEL in github.labels[1]
    assert IN_PROGRESS_LABEL not in github.labels[1]
    assert "pull/7" in github.comments[1][-1]
    assert settled[0].escalated is False


def test_monitor_escalates_a_session_that_could_not_finish(tmp_path: Path) -> None:
    devin = FakeDevin(
        {
            "status_enum": "blocked",
            "structured_output": {
                "outcome": "needs_human",
                "summary": "upgrade requires a breaking API change",
                "blockers": "deck.gl 9 renames the layer props",
                "verification": "build fails",
            },
        }
    )
    pipeline, github = build(tmp_path, devin)
    pipeline.file_issues([finding()])
    github.add_labels(1, [DISPATCH_LABEL])
    pipeline.dispatch_issue(1)

    settled = pipeline.monitor()

    assert settled[0].escalated is True
    assert ESCALATE_LABEL in github.labels[1]
    assert "deck.gl 9 renames" in github.comments[1][-1]
    # A blocked session stays blocked in the API; polling again must not
    # repost the same escalation.
    assert pipeline.monitor() == []


def test_ci_failure_is_sent_back_to_the_owning_session_until_the_budget_runs_out(
    tmp_path: Path,
) -> None:
    devin = FakeDevin()
    pipeline, github = build(tmp_path, devin)
    pipeline.config.max_ci_retries = 1
    github.check_summary = "- **python-lint**: ruff failed"
    pipeline.file_issues([finding()])
    github.add_labels(1, [DISPATCH_LABEL])
    attempt = pipeline.dispatch_issue(1)
    assert attempt is not None
    attempt.pr_url = "https://github.com/ehersch/superset/pull/7"

    assert pipeline.handle_ci_failure(attempt.pr_url, "deadbeef") is True
    assert devin.messages
    assert "ruff failed" in devin.messages[0][1]
    assert attempt.ci_retries == 1

    # Budget exhausted: the second failure escalates instead of retrying.
    assert pipeline.handle_ci_failure(attempt.pr_url, "deadbeef") is False
    assert len(devin.messages) == 1
    assert ESCALATE_LABEL in github.labels[1]


def test_ci_failure_ignores_pull_requests_the_pipeline_does_not_own(
    tmp_path: Path,
) -> None:
    pipeline, _ = build(tmp_path, FakeDevin())
    assert pipeline.handle_ci_failure("https://github.com/x/y/pull/1", "sha") is False


def test_state_survives_a_process_restart(tmp_path: Path) -> None:
    devin = FakeDevin()
    pipeline, github = build(tmp_path, devin)
    pipeline.file_issues([finding()])
    github.add_labels(1, [DISPATCH_LABEL])
    pipeline.dispatch_issue(1)

    reloaded, _ = build(tmp_path, devin)
    record = reloaded.state.get(1)
    assert record is not None
    assert record.latest is not None
    assert record.latest.session_id == "devin-1"


def test_report_lists_every_tracked_issue(tmp_path: Path) -> None:
    devin = FakeDevin()
    pipeline, github = build(tmp_path, devin)
    pipeline.file_issues([finding()])
    github.add_labels(1, [DISPATCH_LABEL])
    pipeline.dispatch_issue(1)
    report = pipeline.report()
    assert "Devin remediation pipeline" in report
    assert "npm_audit" in report
    assert "devin-1" in report
    assert "Autonomous resolution rate" in report


def test_metrics_count_the_lifecycle(tmp_path: Path) -> None:
    devin = FakeDevin(
        {
            "status_enum": "finished",
            "structured_output": {
                "outcome": "fixed",
                "pr_url": "https://github.com/ehersch/superset/pull/7",
                "summary": "bumped po2json",
                "verification": "clean",
            },
        }
    )
    pipeline, github = build(tmp_path, devin)
    pipeline.file_issues([finding()])
    github.add_labels(1, [DISPATCH_LABEL])
    pipeline.dispatch_issue(1)

    assert pipeline.metrics()["in_flight"] == 1
    pipeline.monitor()
    stats = pipeline.metrics()
    assert stats["fixed_with_pr"] == 1
    assert stats["in_flight"] == 0
    assert stats["autonomous_resolution_rate"] == 1.0


def test_dashboard_renders_the_ledger(tmp_path: Path) -> None:
    devin = FakeDevin(
        {
            "status_enum": "finished",
            "structured_output": {
                "outcome": "fixed",
                "pr_url": "https://github.com/ehersch/superset/pull/7",
                "summary": "bumped po2json",
                "verification": "npm audit --production: 0 vulnerabilities",
            },
        }
    )
    pipeline, github = build(tmp_path, devin)
    pipeline.file_issues([finding()])
    github.add_labels(1, [DISPATCH_LABEL])
    pipeline.dispatch_issue(1)
    pipeline.monitor()

    html = pipeline.dashboard()

    assert "<!doctype html>" in html
    assert "https://github.com/ehersch/superset/pull/7" in html
    assert "npm audit --production: 0 vulnerabilities" in html
    assert "bumped po2json" in html


@pytest.mark.parametrize("budget", [0, 2])
def test_dispatch_respects_the_per_run_budget(tmp_path: Path, budget: int) -> None:
    devin = FakeDevin()
    pipeline, github = build(tmp_path, devin)
    pipeline.config.max_dispatch_per_run = budget
    for index in range(3):
        pipeline.file_issues([finding(f"k{index}")])
    for number in github.issues:
        github.add_labels(number, [DISPATCH_LABEL])

    pipeline.dispatch_labelled()

    assert len(devin.created) == budget
