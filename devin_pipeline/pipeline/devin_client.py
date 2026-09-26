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
"""Thin client for the Devin REST API (v1).

Endpoints used:

* ``POST /v1/sessions``                      create a session
* ``GET  /v1/sessions/{session_id}``         poll status / structured output
* ``POST /v1/sessions/{session_id}/message`` steer a running session

Only these three are needed: the pipeline treats a Devin session as a durable
worker that it starts, observes and occasionally corrects.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"finished", "blocked", "expired"}


class DevinAPIError(RuntimeError):
    pass


class DevinClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.devin.ai",
        timeout: int = 60,
        dry_run: bool = False,
    ) -> None:
        if not api_key and not dry_run:
            raise DevinAPIError("DEVIN_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = DevinAPIError(
                        f"{method} {path} -> {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                elif response.status_code >= 400:
                    raise DevinAPIError(
                        f"{method} {path} -> {response.status_code}: "
                        f"{response.text[:500]}"
                    )
                else:
                    if not response.content:
                        return None
                    return response.json()
            sleep_for = 2**attempt
            logger.warning("Devin API retry in %ss (%s)", sleep_for, last_error)
            time.sleep(sleep_for)
        raise DevinAPIError(str(last_error))

    def create_session(
        self,
        prompt: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        max_acu_limit: int | None = None,
        structured_output_schema: dict[str, Any] | None = None,
        idempotent: bool = True,
        unlisted: bool = False,
        playbook_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a session.

        ``idempotent=True`` means a retry of the same dispatch (a webhook
        redelivery, a re-run of the workflow) reuses the existing session
        instead of starting a second agent on the same issue; the response
        carries ``is_new_session`` so the caller can tell the difference.
        """
        payload: dict[str, Any] = {"prompt": prompt, "idempotent": idempotent}
        if title:
            payload["title"] = title[:255]
        if tags:
            payload["tags"] = tags
        if max_acu_limit:
            payload["max_acu_limit"] = max_acu_limit
        if structured_output_schema:
            payload["structured_output_schema"] = structured_output_schema
        if unlisted:
            payload["unlisted"] = True
        if playbook_id:
            payload["playbook_id"] = playbook_id

        if self.dry_run:
            fake = f"devin-dryrun-{abs(hash(prompt)) % 10**12:012d}"
            logger.info("[dry-run] would create session %s", title)
            return {
                "session_id": fake,
                "url": f"https://app.devin.ai/sessions/{fake}",
                "is_new_session": True,
            }
        return self._request("POST", "/v1/sessions", json=payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        if self.dry_run:
            return {
                "session_id": session_id,
                "status_enum": "finished",
                "structured_output": {
                    "outcome": "fixed",
                    "summary": "[dry-run] no session was created",
                },
            }
        return self._request("GET", f"/v1/sessions/{session_id}")

    def send_message(self, session_id: str, message: str) -> Any:
        if self.dry_run:
            logger.info("[dry-run] would message %s: %s", session_id, message[:80])
            return None
        return self._request(
            "POST", f"/v1/sessions/{session_id}/message", json={"message": message}
        )


def session_is_terminal(session: dict[str, Any]) -> bool:
    return (session.get("status_enum") or "") in TERMINAL_STATUSES


def session_has_result(session: dict[str, Any]) -> bool:
    """Whether the session has reported the outcome the prompt asked for.

    A session that finishes its task keeps its machine alive and stays
    ``working`` until it is idled or messaged, so waiting for a terminal
    status would leave every successful remediation in flight for hours.
    The structured output is the completion signal; the status is not.
    """
    structured = session.get("structured_output")
    return isinstance(structured, dict) and bool(structured.get("outcome"))


def extract_pr_url(session: dict[str, Any]) -> str | None:
    """Best-effort PR discovery.

    The structured output is authoritative because the prompt asks for it, but
    a session that ran out of budget mid-report may still have opened a PR, so
    fall back to the ``pull_request`` field the API attaches to the session.
    """
    structured = session.get("structured_output") or {}
    if isinstance(structured, dict):
        url = structured.get("pr_url")
        if isinstance(url, str) and url.startswith("http"):
            return url
    pull_request = session.get("pull_request") or {}
    if isinstance(pull_request, dict):
        url = pull_request.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None
