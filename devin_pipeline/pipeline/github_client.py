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
"""Minimal GitHub REST client for the issue surface the pipeline touches."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import requests

logger = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(
        self,
        token: str,
        repo: str,
        base_url: str = "https://api.github.com",
        dry_run: bool = False,
    ) -> None:
        self.repo = repo
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        for attempt in range(4):
            response = self.session.request(method, url, timeout=60, **kwargs)
            if response.status_code == 403 and "rate limit" in response.text.lower():
                time.sleep(2**attempt * 5)
                continue
            if response.status_code >= 400:
                raise GitHubError(
                    f"{method} {url} -> {response.status_code}: {response.text[:400]}"
                )
            if not response.content:
                return None
            return response.json()
        raise GitHubError(f"{method} {url} kept failing")

    # -- issues ---------------------------------------------------------

    def iter_issues(
        self, state: str = "open", labels: str | None = None
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            params: dict[str, Any] = {"state": state, "per_page": 100, "page": page}
            if labels:
                params["labels"] = labels
            batch = self._request("GET", f"/repos/{self.repo}/issues", params=params)
            if not batch:
                return
            for item in batch:
                if "pull_request" not in item:
                    yield item
            page += 1

    def get_issue(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.repo}/issues/{number}")

    def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        if self.dry_run:
            logger.info("[dry-run] would create issue: %s", title)
            return {"number": -1, "html_url": "", "title": title}
        return self._request(
            "POST",
            f"/repos/{self.repo}/issues",
            json={"title": title, "body": body, "labels": labels or []},
        )

    def update_issue(self, number: int, **fields: Any) -> dict[str, Any]:
        if self.dry_run:
            logger.info("[dry-run] would update issue #%s: %s", number, fields)
            return {"number": number}
        return self._request(
            "PATCH", f"/repos/{self.repo}/issues/{number}", json=fields
        )

    def comment(self, number: int, body: str) -> dict[str, Any]:
        if self.dry_run:
            logger.info("[dry-run] would comment on #%s: %s", number, body[:80])
            return {}
        return self._request(
            "POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body}
        )

    def add_labels(self, number: int, labels: list[str]) -> Any:
        if self.dry_run:
            logger.info("[dry-run] would label #%s with %s", number, labels)
            return None
        return self._request(
            "POST",
            f"/repos/{self.repo}/issues/{number}/labels",
            json={"labels": labels},
        )

    def remove_label(self, number: int, label: str) -> None:
        if self.dry_run:
            logger.info("[dry-run] would remove %s from #%s", label, number)
            return
        try:
            self._request(
                "DELETE", f"/repos/{self.repo}/issues/{number}/labels/{label}"
            )
        except GitHubError as exc:  # label may already be gone
            logger.debug("remove_label: %s", exc)

    def ensure_label(self, name: str, color: str, description: str) -> None:
        if self.dry_run:
            return
        try:
            self._request("GET", f"/repos/{self.repo}/labels/{name}")
        except GitHubError:
            self._request(
                "POST",
                f"/repos/{self.repo}/labels",
                json={"name": name, "color": color, "description": description[:100]},
            )

    # -- pull requests / checks ----------------------------------------

    def get_pull(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.repo}/pulls/{number}")

    def check_runs(self, ref: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            f"/repos/{self.repo}/commits/{ref}/check-runs",
            params={"per_page": 100},
        )
        return data.get("check_runs", []) if data else []

    def failed_check_summary(self, ref: str, limit: int = 3) -> str:
        failures = [
            run
            for run in self.check_runs(ref)
            if run.get("conclusion") in {"failure", "timed_out"}
        ]
        if not failures:
            return ""
        lines = []
        for run in failures[:limit]:
            output = run.get("output") or {}
            lines.append(
                f"- **{run.get('name')}**: "
                f"{output.get('title') or run.get('conclusion')}\n"
                f"  {(output.get('summary') or '')[:600]}\n"
                f"  {run.get('html_url')}"
            )
        return "\n".join(lines)
