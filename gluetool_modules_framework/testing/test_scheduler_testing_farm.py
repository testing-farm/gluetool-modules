# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool_modules_framework.libs

from typing import cast
from typing import Optional

import gluetool
from gluetool import GlueError
from gluetool.action import Action
from gluetool.log import log_dict

from gluetool_modules_framework.testing.test_scheduler_baseosci import ProvisionerCapabilities
from gluetool_modules_framework.testing_farm.testing_farm_request import TestingFarmRequest
from gluetool_modules_framework.libs.test_schedule import TestSchedule


class TestSchedulerTestingFarm(gluetool.Module):
    """
    Prepares "test schedule" for other modules to perform. A schedule is a list of "test schedule entries"
    (see :py:class:`libs.test_schdule.TestScheduleEntry`).
    """

    name = 'test-scheduler-testing-farm'
    description = 'Prepares "test schedule" in Testing Farm for other modules to perform.'

    shared_functions = ['test_schedule']

    _schedule: Optional[TestSchedule] = None

    def test_schedule(self) -> Optional[TestSchedule]:
        """
        Returns schedule for runners. It tells runner which schedules
        it should run on which guest.

        :returns: TestSchedule
        """

        return self._schedule

    def execute(self) -> None:

        self.require_shared('testing_farm_request', 'create_test_schedule', 'provisioner_capabilities')

        provisioner_capabilities = cast(
            ProvisionerCapabilities,
            self.shared('provisioner_capabilities')
        )

        log_dict(self.debug, 'provisioner capabilities', provisioner_capabilities)

        # ... these are arches supported by the provisioner...
        supported_arches = provisioner_capabilities.available_arches if provisioner_capabilities else []
        log_dict(self.debug, 'supported arches', supported_arches)

        testing_farm_request = cast(
            TestingFarmRequest,
            self.shared('testing_farm_request')
        )

        requested_arches = [
            environment.arch for environment in testing_farm_request.environments_requested if environment.arch
        ]
        log_dict(self.debug, 'requested arches', requested_arches)

        # Make sure all requested architectures are supported.
        # Refuse to run otherwise.
        # We might want to revisit this policy later.
        if supported_arches is not gluetool_modules_framework.libs.ANY:
            assert isinstance(supported_arches, list)

            for arch in requested_arches:
                if arch not in supported_arches:
                    raise GlueError("The architecture '{}' is unsupported, cannot continue".format(arch))

        testing_environments = []

        for environment_requested in testing_farm_request.environments_requested:
            if environment_requested.arch in requested_arches:
                testing_environments.append(environment_requested)

        with Action('creating test schedule', parent=Action.current_action(), logger=self.logger):
            schedule = self.shared('create_test_schedule', testing_environment_constraints=testing_environments)

        if not schedule:
            raise GlueError('Test schedule is empty')

        self._schedule = schedule
