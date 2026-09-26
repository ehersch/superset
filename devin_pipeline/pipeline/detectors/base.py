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
"""Detector protocol and registry.

A detector answers two questions about the repository: *what is broken* and
*how would a machine know it stopped being broken*. Everything downstream —
issue text, the session prompt, the verification loop — is derived from that
answer, which is why every detector must supply a reproduction command and
acceptance criteria rather than prose.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from ..models import Finding

logger = logging.getLogger(__name__)

DetectorFn = Callable[[Path], Iterable[Finding]]
_REGISTRY: dict[str, DetectorFn] = {}


def register(name: str) -> Callable[[DetectorFn], DetectorFn]:
    def decorator(fn: DetectorFn) -> DetectorFn:
        _REGISTRY[name] = fn
        return fn

    return decorator


def registry() -> dict[str, DetectorFn]:
    from . import (  # noqa: F401  (import for side-effect registration)
        engine_spec_metadata,
        i18n_placeholders,
        npm_audit,
        osv_python,
        upstream_mirror,
    )

    return dict(_REGISTRY)


def run(
    cmd: list[str], cwd: Path, timeout: int = 900
) -> subprocess.CompletedProcess[str]:
    logger.info("running %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(  # noqa: S603
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
