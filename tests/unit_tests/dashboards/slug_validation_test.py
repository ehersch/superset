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
from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from superset.commands.dashboard.create import CreateDashboardCommand
from superset.commands.dashboard.exceptions import DashboardInvalidError
from superset.commands.dashboard.update import UpdateDashboardCommand
from superset.daos.dashboard import DashboardDAO
from superset.extensions import security_manager
from superset.models.dashboard import Dashboard

DUPLICATE_SLUG_MESSAGE = "A dashboard with this slug already exists"


def test_create_duplicate_slug_names_slug_field(mocker: MockerFixture) -> None:
    mocker.patch.object(security_manager, "is_admin", return_value=True)
    mocker.patch.object(DashboardDAO, "validate_slug_uniqueness", return_value=False)

    with pytest.raises(DashboardInvalidError) as excinfo:
        CreateDashboardCommand(data={"slug": "taken"}).validate()

    assert excinfo.value.status == 422
    assert excinfo.value.normalized_messages() == {"slug": [DUPLICATE_SLUG_MESSAGE]}


def test_update_duplicate_slug_names_slug_field(mocker: MockerFixture) -> None:
    mocker.patch.object(security_manager, "raise_for_editorship")
    mocker.patch.object(security_manager, "is_admin", return_value=True)
    mocker.patch.object(
        DashboardDAO,
        "find_by_id",
        return_value=Dashboard(id=1, dashboard_title="d", slug="mine", tags=[]),
    )
    mocker.patch.object(
        DashboardDAO, "validate_update_slug_uniqueness", return_value=False
    )

    with pytest.raises(DashboardInvalidError) as excinfo:
        UpdateDashboardCommand(1, {"slug": "taken"}).validate()

    assert excinfo.value.status == 422
    assert excinfo.value.normalized_messages() == {"slug": [DUPLICATE_SLUG_MESSAGE]}
