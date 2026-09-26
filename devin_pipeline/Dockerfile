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
# The control plane, runnable anywhere the Actions workflows are not:
#
#   docker build -f devin_pipeline/Dockerfile -t devin-pipeline .
#   docker run --rm devin-pipeline --dry-run detect
#   docker run --rm -e GITHUB_TOKEN -e DEVIN_API_KEY \
#     -v "$PWD/.devin-pipeline:/state" -e PIPELINE_STATE=/state/state.json \
#     devin-pipeline monitor
FROM python:3.11-slim

# Node is only needed by the npm_audit detector, which shells out to
# `npm audit --package-lock-only` against the checked-out lockfile.
RUN apt-get update \
  && apt-get install -y --no-install-recommends git nodejs npm \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY devin_pipeline/requirements.txt devin_pipeline/requirements.txt
RUN pip install --no-cache-dir -r devin_pipeline/requirements.txt

COPY devin_pipeline devin_pipeline

ENV PYTHONUNBUFFERED=1 \
    PIPELINE_STATE=/state/state.json \
    REPO_PATH=/repo
VOLUME ["/state", "/repo"]

ENTRYPOINT ["python", "-m", "devin_pipeline.pipeline.cli"]
CMD ["--dry-run", "detect"]
