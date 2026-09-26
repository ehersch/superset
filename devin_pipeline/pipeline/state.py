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
"""JSON-file state store.

The GitHub issue is the source of truth for *what* should be fixed; this store
only remembers *what the pipeline did* — which sessions it started, how many CI
retries it has spent, whether it already escalated. It is deliberately a plain
file so a run can be replayed, inspected or committed as an artifact.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import IssueRecord


class StateStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.records: dict[int, IssueRecord] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text())
        self.records = {
            int(number): IssueRecord.from_dict(record)
            for number, record in data.get("issues", {}).items()
        }

    def save(self) -> None:
        payload = {
            "version": 1,
            "issues": {
                str(number): record.to_dict() for number, record in self.records.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: a crashed run must not leave a half-written ledger that
        # would make the next run re-dispatch sessions it already paid for.
        with tempfile.NamedTemporaryFile(
            "w", dir=self.path.parent, delete=False, encoding="utf-8"
        ) as handle:
            json.dump(payload, handle, indent=2)
            temp_name = handle.name
        os.replace(temp_name, self.path)

    def get(self, issue_number: int) -> IssueRecord | None:
        return self.records.get(issue_number)

    def put(self, record: IssueRecord) -> None:
        self.records[record.issue_number] = record

    def active(self) -> list[IssueRecord]:
        """Records whose most recent session has not reached a terminal state."""
        out = []
        for record in self.records.values():
            latest = record.latest
            if latest and latest.status not in {"finished", "expired", "failed"}:
                out.append(record)
        return out
