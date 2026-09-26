#!/usr/bin/env bash
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
#
# Reads and writes the pipeline ledger on an orphan `devin-pipeline-state`
# branch. A branch (rather than a cache) is deliberate: the ledger survives
# cache eviction, is readable by a human during a demo, and its history shows
# exactly when each session was dispatched and settled.
set -euo pipefail

BRANCH="${STATE_BRANCH:-devin-pipeline-state}"
STATE_DIR="${STATE_DIR:-.devin-pipeline}"
STATE_FILE="${STATE_DIR}/state.json"

pull_state() {
  mkdir -p "${STATE_DIR}"
  if git fetch origin "${BRANCH}" --depth 1 2>/dev/null; then
    git show "FETCH_HEAD:state.json" > "${STATE_FILE}" 2>/dev/null || true
  fi
  [ -f "${STATE_FILE}" ] || echo '{"version": 1, "issues": {}}' > "${STATE_FILE}"
}

push_state() {
  local work remote
  work="$(mktemp -d)"
  remote="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
  git -C "${work}" init -q
  git -C "${work}" config user.name "devin-pipeline[bot]"
  git -C "${work}" config user.email "devin-pipeline@users.noreply.github.com"
  git -C "${work}" remote add origin "${remote}"
  # Each write continues the existing branch: the history of the ledger is the
  # audit log of when every session was dispatched and settled, so a run must
  # append to it rather than replace it.
  if git -C "${work}" fetch -q --depth 1 origin "${BRANCH}" 2>/dev/null; then
    git -C "${work}" checkout -q -B "${BRANCH}" FETCH_HEAD
  else
    git -C "${work}" checkout -q -b "${BRANCH}"
  fi
  cp "${STATE_FILE}" "${work}/state.json"
  git -C "${work}" add state.json
  if git -C "${work}" diff --cached --quiet; then
    rm -rf "${work}"
    return 0
  fi
  git -C "${work}" commit -q -m "state: ${GITHUB_RUN_ID:-local} ($(date -u +%FT%TZ))"
  git -C "${work}" push -q origin "HEAD:refs/heads/${BRANCH}"
  rm -rf "${work}"
}

case "${1:-}" in
  pull) pull_state ;;
  push) push_state ;;
  *) echo "usage: $0 {pull|push}" >&2; exit 2 ;;
esac
