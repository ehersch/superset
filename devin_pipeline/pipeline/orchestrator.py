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

import logging
import re
from typing import Any, Iterable

from .config import (
    Config,
    DISPATCH_LABEL,
    DONE_LABEL,
    ESCALATE_LABEL,
    IN_PROGRESS_LABEL,
    LABELS,
)
from .detectors.base import registry
from .devin_client import DevinClient, extract_pr_url, session_is_terminal
from .github_client import GitHubClient
from .models import Attempt, Evidence, Finding, IssueRecord, Outcome, utcnow
from .prompts import build_ci_feedback, build_prompt, STRUCTURED_OUTPUT_SCHEMA
from .state import StateStore

logger = logging.getLogger(__name__)

PR_NUMBER = re.compile(r"/pull/(\d+)")


class Pipeline:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.github = GitHubClient(
            config.github_token, config.repo, dry_run=config.dry_run
        )
        self.devin = DevinClient(
            config.devin_api_key,
            base_url=config.devin_base_url,
            dry_run=config.dry_run,
        )
        self.state = StateStore(config.state_path)

    # -- detect ---------------------------------------------------------

    def detect(self, only: list[str] | None = None) -> list[Finding]:
        findings: list[Finding] = []
        for name, detector in registry().items():
            if only and name not in only:
                continue
            try:
                produced = list(detector(self.config.repo_path))
            except Exception:  # noqa: BLE001 - one broken detector must not
                # take the run down; the others still have work to file.
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
            issue = self.github.create_issue(
                finding.title, finding.issue_body(), labels=finding.labels
            )
            filed.append(issue)
            logger.info("filed #%s %s", issue.get("number"), finding.title)
        return filed

    # -- dispatch -------------------------------------------------------

    def dispatch_issue(self, issue_number: int) -> Attempt | None:
        issue = self.github.get_issue(issue_number)
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
            if not session_is_terminal(session):
                continue
            structured = session.get("structured_output") or {}
            attempt.outcome = structured.get("outcome") or Outcome.NEEDS_HUMAN.value
            attempt.summary = structured.get("summary") or ""
            attempt.finished_at = utcnow()
            self._settle(record, attempt, structured)
            settled.append(record)
        self.state.save()
        return settled

    def _settle(
        self, record: IssueRecord, attempt: Attempt, structured: dict[str, Any]
    ) -> None:
        number = record.issue_number
        self.github.remove_label(number, IN_PROGRESS_LABEL)
        verification = (structured.get("verification") or "").strip()
        blockers = (structured.get("blockers") or "").strip()

        if attempt.outcome == Outcome.FIXED.value and attempt.pr_url:
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
        record = next(
            (
                rec
                for rec in self.state.records.values()
                if rec.latest and rec.latest.pr_url == pr_url
            ),
            None,
        )
        if record is None or record.latest is None:
            logger.info("no tracked session owns %s", pr_url)
            return False
        attempt = record.latest
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

    # -- report ---------------------------------------------------------

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
        return (
            f"# Devin remediation pipeline — run report\n\n"
            f"Repository: `{self.config.repo}`  \n"
            f"Generated: {utcnow()}  \n"
            f"Tracked issues: {len(records)} ({tally or 'none'})\n\n"
            + "\n".join(rows)
            + "\n"
        )
