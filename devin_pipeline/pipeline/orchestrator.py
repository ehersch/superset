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
"""The control plane: detect -> file -> dispatch -> monitor -> report.

Each phase is separately invocable and safe to re-run. That is the property
that lets the same code back three different triggers (a nightly schedule, a
label webhook, a check-suite webhook) without any of them needing to know what
the others did.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime
from hashlib import sha256
from typing import Any

from .config import (
    Config,
    DISPATCH_LABEL,
    DONE_LABEL,
    ESCALATE_LABEL,
    IN_PROGRESS_LABEL,
    LABELS,
)
from .dashboard import render as render_dashboard, render_findings
from .detectors.base import registry
from .devin_client import (
    DevinClient,
    extract_pr_url,
    session_has_result,
    session_is_terminal,
    TERMINAL_STATUSES,
)
from .github_client import GitHubClient
from .models import Attempt, Evidence, Finding, IssueRecord, Outcome, utcnow
from .prompts import build_ci_feedback, build_prompt, STRUCTURED_OUTPUT_SCHEMA
from .state import StateStore

logger = logging.getLogger(__name__)

PR_NUMBER = re.compile(r"/pull/(\d+)")


def _digest(structured: dict[str, Any]) -> str:
    return sha256(
        json.dumps(structured, sort_keys=True, default=str).encode()
    ).hexdigest()


def _minutes_between(start: str, end: str) -> float:
    delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return delta.total_seconds() / 60


class Pipeline:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.github = GitHubClient(
            config.github_token, config.repo, dry_run=config.dry_run
        )
        self._devin: DevinClient | None = None
        self.state = StateStore(config.state_path)

    @property
    def devin(self) -> DevinClient:
        """Built on first use so key-less phases (detect, file, report) run."""
        if self._devin is None:
            self._devin = DevinClient(
                self.config.devin_api_key,
                base_url=self.config.devin_base_url,
                dry_run=self.config.dry_run,
            )
        return self._devin

    @devin.setter
    def devin(self, client: DevinClient) -> None:
        self._devin = client

    # -- detect ---------------------------------------------------------

    def detect(self, only: list[str] | None = None) -> list[Finding]:
        findings: list[Finding] = []
        for name, detector in registry().items():
            if only and name not in only:
                continue
            try:
                produced = list(detector(self.config.repo_path))
            except Exception:
                # One broken detector must not take the run down; the others
                # still have work to file.
                logger.exception("detector %s failed", name)
                continue
            limit = self.config.max_issues_per_detector
            if len(produced) > limit:
                logger.warning(
                    "detector %s produced %s findings; filing the %s most severe",
                    name,
                    len(produced),
                    limit,
                )
                produced = produced[:limit]
            findings.extend(produced)
        return findings

    # -- file -----------------------------------------------------------

    def _existing_by_fingerprint(self) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for issue in self.github.iter_issues(state="all"):
            evidence = Evidence.parse(issue.get("body") or "")
            if evidence:
                index.setdefault(evidence.fingerprint, issue)
        return index

    def file_issues(self, findings: Iterable[Finding]) -> list[dict[str, Any]]:
        for name, color, description in LABELS:
            self.github.ensure_label(name, color, description)
        existing = self._existing_by_fingerprint()
        filed = []
        for finding in findings:
            match = existing.get(finding.fingerprint)
            if match:
                # The issue is the ledger for the finding; refresh the evidence
                # so counts stay honest, but never reopen what a human closed.
                if (match.get("body") or "") != finding.issue_body():
                    self.github.update_issue(match["number"], body=finding.issue_body())
                logger.info(
                    "finding %s already filed as #%s",
                    finding.fingerprint,
                    match["number"],
                )
                continue
            # Filing never carries the dispatch label: labelling is the human
            # approval step, and a detector must not be able to spend ACUs.
            labels = [label for label in finding.labels if label != DISPATCH_LABEL]
            issue = self.github.create_issue(
                finding.title, finding.issue_body(), labels=labels
            )
            filed.append(issue)
            logger.info("filed #%s %s", issue.get("number"), finding.title)
        return filed

    # -- dispatch -------------------------------------------------------

    def dispatch_issue(self, issue_number: int) -> Attempt | None:
        issue = self.github.get_issue(issue_number)
        labels = {label["name"] for label in issue.get("labels", [])}
        if DISPATCH_LABEL not in labels:
            # The approval gate is the label, wherever the dispatch came from:
            # a direct CLI call must not be a way around it.
            logger.info("#%s is not labelled %s", issue_number, DISPATCH_LABEL)
            return None
        if ESCALATE_LABEL in labels:
            self.github.comment(
                issue_number,
                f"This issue is still labelled `{ESCALATE_LABEL}` from an earlier "
                f"attempt. Remove that label to let the pipeline try again — "
                f"escalation means a human decided something, and re-labelling "
                f"alone should not spend another session.",
            )
            self.github.remove_label(issue_number, DISPATCH_LABEL)
            return None
        evidence = Evidence.parse(issue.get("body") or "")
        if evidence is None:
            self.github.comment(
                issue_number,
                "The Devin pipeline cannot dispatch this issue: it has no evidence "
                "block, so there is no reproduction command or acceptance criteria "
                "to verify a fix against. Add one, or file it through a detector.",
            )
            self.github.remove_label(issue_number, DISPATCH_LABEL)
            return None

        record = self.state.get(issue_number) or IssueRecord(
            issue_number=issue_number,
            fingerprint=evidence.fingerprint,
            detector=evidence.detector,
            wave=evidence.wave,
            title=issue.get("title", ""),
            severity=evidence.severity,
        )
        latest = record.latest
        if latest and latest.status not in {"finished", "expired", "failed"}:
            logger.info(
                "#%s already has live session %s", issue_number, latest.session_id
            )
            return latest

        prompt = build_prompt(
            repo=self.config.repo,
            issue_number=issue_number,
            issue_title=issue.get("title", ""),
            issue_body=issue.get("body", ""),
            evidence=evidence,
            base_branch=self.config.base_branch,
        )
        session = self.devin.create_session(
            prompt,
            title=f"[{self.config.repo}#{issue_number}] {issue.get('title', '')}",
            tags=["superset-pipeline", f"wave-{evidence.wave}", evidence.detector],
            max_acu_limit=self.config.max_acu_per_session,
            structured_output_schema=STRUCTURED_OUTPUT_SCHEMA,
        )
        attempt = Attempt(
            session_id=session["session_id"],
            session_url=session.get("url", ""),
            created_at=utcnow(),
        )
        record.attempts.append(attempt)
        self.state.put(record)
        self.state.save()

        self.github.add_labels(issue_number, [IN_PROGRESS_LABEL])
        self.github.remove_label(issue_number, DISPATCH_LABEL)
        self.github.comment(
            issue_number,
            f"Dispatched Devin session [`{attempt.session_id}`]({attempt.session_url}) "
            f"(attempt {len(record.attempts)}, "
            f"ACU cap {self.config.max_acu_per_session}).\n\n"
            f"It must reproduce the problem first, then satisfy every acceptance "
            f"criterion above by running it. Progress is posted here.",
        )
        return attempt

    def dispatch_labelled(self) -> list[Attempt]:
        attempts: list[Attempt] = []
        for issue in self.github.iter_issues(state="open", labels=DISPATCH_LABEL):
            if len(attempts) >= self.config.max_dispatch_per_run:
                logger.info("dispatch budget reached for this run")
                break
            attempt = self.dispatch_issue(issue["number"])
            if attempt:
                attempts.append(attempt)
        return attempts

    # -- monitor --------------------------------------------------------

    def monitor(self) -> list[IssueRecord]:
        settled = []
        for record in self.state.active():
            attempt = record.latest
            if attempt is None:
                continue
            session = self.devin.get_session(attempt.session_id)
            attempt.status = session.get("status_enum") or attempt.status
            attempt.pr_url = extract_pr_url(session) or attempt.pr_url
            structured = session.get("structured_output") or {}
            digest = _digest(structured)
            fresh_result = (
                session_has_result(session) and digest != attempt.result_digest
            )
            if not (session_is_terminal(session) or fresh_result):
                continue
            attempt.result_digest = digest
            if fresh_result:
                attempt.outcome = structured.get("outcome") or Outcome.NEEDS_HUMAN.value
                attempt.summary = structured.get("summary") or ""
            else:
                # A session that ends without reporting again — after CI feedback,
                # or with no structured output at all — has nothing new to claim.
                attempt.outcome = Outcome.NEEDS_HUMAN.value
                attempt.summary = "session ended without reporting a new result"
                structured = {**structured, "outcome": Outcome.NEEDS_HUMAN.value}
            attempt.finished_at = utcnow()
            self._settle(record, attempt, structured)
            settled.append(record)
        settled.extend(self.reconsider())
        self.state.save()
        return settled

    def reconsider(self) -> list[IssueRecord]:
        """Re-settle escalations whose session reported again afterwards.

        Escalating is a question, not a verdict, and the session stays alive
        holding its context. When a human answers it there, this is what turns
        that answer into a PR, a label and a dashboard row — without anyone
        re-running the pipeline by hand.
        """
        changed: list[IssueRecord] = []
        for record in self.state.escalated():
            attempt = record.latest
            if attempt is None:
                continue
            # The stored status is whatever the session reported when it
            # escalated; a session that was `blocked` on a question is working
            # again once the question is answered, so ask the API, not the
            # ledger. The digest is what keeps this from reposting.
            session = self.devin.get_session(attempt.session_id)
            structured = session.get("structured_output") or {}
            digest = _digest(structured)
            if not session_has_result(session) or digest == attempt.result_digest:
                continue
            attempt.status = session.get("status_enum") or attempt.status
            attempt.pr_url = extract_pr_url(session) or attempt.pr_url
            attempt.result_digest = digest
            attempt.outcome = structured.get("outcome") or Outcome.NEEDS_HUMAN.value
            attempt.summary = structured.get("summary") or ""
            attempt.finished_at = utcnow()
            self._settle(record, attempt, structured)
            changed.append(record)
        return changed

    def _settle(
        self, record: IssueRecord, attempt: Attempt, structured: dict[str, Any]
    ) -> None:
        number = record.issue_number
        self.github.remove_label(number, IN_PROGRESS_LABEL)
        verification = (structured.get("verification") or "").strip()
        blockers = (structured.get("blockers") or "").strip()
        attempt.verification = verification
        attempt.blockers = blockers

        if attempt.outcome == Outcome.FIXED.value and attempt.pr_url:
            record.escalated = False
            self.github.remove_label(number, ESCALATE_LABEL)
            self.github.add_labels(number, [DONE_LABEL])
            self.github.comment(
                number,
                f"Devin session [`{attempt.session_id}`]({attempt.session_url}) "
                f"opened {attempt.pr_url}.\n\n"
                f"**Summary** — {attempt.summary}\n\n"
                f"<details><summary>Verification</summary>\n\n```\n{verification[:4000]}\n```\n</details>",
            )
            return

        record.escalated = True
        self.github.add_labels(number, [ESCALATE_LABEL])
        reason = blockers or attempt.summary or "the session ended without a result"
        pr_line = f"\n\nPartial work: {attempt.pr_url}" if attempt.pr_url else ""
        self.github.comment(
            number,
            f"Devin session [`{attempt.session_id}`]({attempt.session_url}) ended as "
            f"`{attempt.outcome}` and needs a human decision.\n\n"
            f"**Blocker** — {reason}{pr_line}",
        )

    # -- CI feedback ----------------------------------------------------

    def handle_ci_failure(self, pr_url: str, head_sha: str) -> bool:
        """Feed a failing check suite back into the session that opened the PR.

        Returning the failure to the same session is what makes this a loop
        rather than a one-shot: the agent keeps its context and its branch, and
        the pipeline only escalates once the retry budget is spent.
        """
        record = self._owner_of(pr_url)
        if record is None or record.latest is None:
            # The PR may have been opened since the last poll, so the ledger
            # has no URL for it yet. Refresh live sessions once and re-check.
            self._refresh_pr_urls()
            record = self._owner_of(pr_url)
        if record is None or record.latest is None:
            logger.info("no tracked session owns %s", pr_url)
            return False
        attempt = record.latest
        if attempt.status in TERMINAL_STATUSES:
            # A dead session cannot act on feedback; messaging it would burn a
            # retry and change nothing. A settled-but-live session can: the
            # pipeline settles on the reported outcome, and the session stays
            # up afterwards holding its branch and its context.
            logger.info("session %s is no longer live", attempt.session_id)
            return False
        if attempt.ci_retries >= self.config.max_ci_retries:
            record.escalated = True
            self.state.save()
            self.github.add_labels(record.issue_number, [ESCALATE_LABEL])
            self.github.comment(
                record.issue_number,
                f"CI is still failing on {pr_url} after "
                f"{attempt.ci_retries} automated retries. Escalating — the "
                f"remaining failures need a human decision.",
            )
            return False

        summary = self.github.failed_check_summary(head_sha)
        if not summary:
            return False
        attempt.ci_retries += 1
        # The PR is back in the agent's hands, so the issue returns to the
        # in-flight set and the next poll settles it on the retried outcome.
        attempt.finished_at = None
        attempt.outcome = None
        self.github.remove_label(record.issue_number, DONE_LABEL)
        self.github.add_labels(record.issue_number, [IN_PROGRESS_LABEL])
        self.state.save()
        self.devin.send_message(
            attempt.session_id,
            build_ci_feedback(
                pr_url=pr_url,
                attempt=attempt.ci_retries,
                max_attempts=self.config.max_ci_retries,
                summary=summary,
            ),
        )
        self.github.comment(
            record.issue_number,
            f"CI failed on {pr_url}; sent the failures back to session "
            f"[`{attempt.session_id}`]({attempt.session_url}) "
            f"(retry {attempt.ci_retries}/{self.config.max_ci_retries}).",
        )
        return True

    def _owner_of(self, pr_url: str) -> IssueRecord | None:
        return next(
            (
                rec
                for rec in self.state.records.values()
                if rec.latest and rec.latest.pr_url == pr_url
            ),
            None,
        )

    def _refresh_pr_urls(self) -> None:
        for record in self.state.active():
            attempt = record.latest
            if attempt is None or attempt.pr_url:
                continue
            session = self.devin.get_session(attempt.session_id)
            attempt.pr_url = extract_pr_url(session) or attempt.pr_url
        self.state.save()

    # -- report ---------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        """The numbers an engineering leader would ask for.

        Everything is derived from the ledger, so the same figures come out of
        a scheduled run, a local run against a pulled ledger, and the report
        artifact attached to a workflow run.
        """
        records = list(self.state.records.values())
        attempts = [att for rec in records for att in rec.attempts]
        settled = [att for att in attempts if att.finished_at]
        fixed = [
            att for att in settled if att.outcome == Outcome.FIXED.value and att.pr_url
        ]
        durations = [
            _minutes_between(att.created_at, att.finished_at)
            for att in settled
            if att.finished_at
        ]
        resolved = len(fixed) / len(settled) if settled else 0.0
        return {
            "issues_tracked": len(records),
            "dispatched": len(attempts),
            "in_flight": len(attempts) - len(settled),
            "settled": len(settled),
            "fixed_with_pr": len(fixed),
            "escalated": sum(1 for rec in records if rec.escalated),
            "not_reproducible": sum(
                1 for att in settled if att.outcome == Outcome.NOT_REPRODUCIBLE.value
            ),
            "autonomous_resolution_rate": round(resolved, 3),
            "ci_retries_spent": sum(att.ci_retries for att in attempts),
            "sessions_needing_ci_retry": sum(
                1 for att in attempts if att.ci_retries > 0
            ),
            "median_minutes_to_settle": (
                round(sorted(durations)[len(durations) // 2], 1) if durations else None
            ),
        }

    def findings_page(self, findings: list[Finding]) -> str:
        """Detector output as a standalone HTML page."""
        return render_findings(self.config.repo, findings)

    def dashboard(self) -> str:
        """The ledger as a standalone HTML status page."""
        return render_dashboard(
            self.config.repo, list(self.state.records.values()), self.metrics()
        )

    def report(self) -> str:
        records = sorted(self.state.records.values(), key=lambda r: r.issue_number)
        rows = [
            "| Issue | Wave | Detector | Session | Attempts "
            "| CI retries | Outcome | PR |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        counts: dict[str, int] = {}
        for record in records:
            attempt = record.latest
            outcome = (attempt.outcome if attempt else None) or (
                attempt.status if attempt else "not dispatched"
            )
            counts[outcome] = counts.get(outcome, 0) + 1
            session = (
                f"[`{attempt.session_id[:12]}`]({attempt.session_url})"
                if attempt
                else "—"
            )
            rows.append(
                f"| [#{record.issue_number}]"
                f"(https://github.com/{self.config.repo}"
                f"/issues/{record.issue_number}) "
                f"| {record.wave} | `{record.detector}` | {session} "
                f"| {len(record.attempts)} | {attempt.ci_retries if attempt else 0} "
                f"| {outcome} "
                f"| {attempt.pr_url if attempt and attempt.pr_url else '—'} |"
            )
        tally = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
        stats = self.metrics()
        rate = f"{stats['autonomous_resolution_rate'] * 100:.0f}%"
        median = stats["median_minutes_to_settle"]
        scoreboard = [
            "| Metric | Value |",
            "| --- | --- |",
            f"| Issues tracked | {stats['issues_tracked']} |",
            f"| Sessions dispatched | {stats['dispatched']} |",
            f"| In flight | {stats['in_flight']} |",
            f"| Fixed with a PR | {stats['fixed_with_pr']} |",
            f"| Escalated to a human | {stats['escalated']} |",
            f"| Not reproducible | {stats['not_reproducible']} |",
            f"| Autonomous resolution rate | {rate} |",
            f"| Sessions needing a CI retry | {stats['sessions_needing_ci_retry']} |",
            f"| CI retries spent | {stats['ci_retries_spent']} |",
            f"| Median minutes to settle | {median if median is not None else '—'} |",
        ]
        return (
            f"# Devin remediation pipeline — run report\n\n"
            f"Repository: `{self.config.repo}`  \n"
            f"Generated: {utcnow()}  \n"
            f"Tracked issues: {len(records)} ({tally or 'none'})\n\n"
            f"## Scoreboard\n\n" + "\n".join(scoreboard) + "\n\n"
            "## Per-issue ledger\n\n" + "\n".join(rows) + "\n"
        )
