# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
from gluetool.utils import PatternMap
from gluetool.glue import GlueError
from gluetool.result import Ok
from gluetool_modules_framework.libs.guest_setup import guest_setup_log_dirpath, GuestSetupStage, SetupGuestReturnType
from gluetool_modules_framework.libs.guest import NetworkedGuest
from gluetool_modules_framework.testing_farm.testing_farm_request import Artifact

# Type annotations
from typing import Any, List, Optional  # noqa


class GuestSetupOrder(gluetool.Module):
    """
    Executes guest setup functions in a given order.
    The module takes `order` values in artifacts and executes
    related guest setup functions in that order.
    """

    name = 'guest-setup-order'
    description = 'Executes guest setup functions in a given order'

    options = {
        'artifact-guest-setup-map': {
            'help': 'Mapping between artifact type and related guest setup shared function (default: none).',
            'metavar': 'FILE'
        },
    }

    shared_functions = ['setup_guest']

    def get_guest_setup(self, artifact_type: str) -> str:
        """
        Convert artifact type to guest setup function name.
        """
        pattern_map_filepath = self.option('artifact-guest-setup-map')

        self.logger.debug("attempt to map artifact type '{}' to guest setup function using '{}' pattern map".format(
            artifact_type,
            pattern_map_filepath
        ))

        pattern_map = PatternMap(
            pattern_map_filepath,
            logger=self.logger
        )

        guest_setup_name = pattern_map.match(artifact_type)
        self.logger.debug("artifact type '{}' was mapped to '{}'".format(artifact_type, guest_setup_name))

        return guest_setup_name

    def sanity(self) -> None:
        if not self.option('artifact-guest-setup-map'):
            raise GlueError('The artifact-guest-setup-map option is required.')

    def setup_guest(
        self,
        guest: NetworkedGuest,
        stage: GuestSetupStage = GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
        log_dirpath: Optional[str] = None,
        **kwargs: Any
    ) -> Any:

        log_dirpath = guest_setup_log_dirpath(guest, log_dirpath)

        # First call setup_guest from guest-setup module
        r_overloaded_guest_setup_output = self.overloaded_shared(
            'setup_guest',
            guest,
            stage=stage,
            log_dirpath=log_dirpath,
            **kwargs
        )

        # We don't want to run artifact installation if there
        # was an error or if we are not in ARTIFACT_INSTALLATION stage
        if getattr(r_overloaded_guest_setup_output, 'is_error', None) or stage != GuestSetupStage.ARTIFACT_INSTALLATION:
            return r_overloaded_guest_setup_output

        guest_setup_output = r_overloaded_guest_setup_output or Ok([])

        artifacts = []
        if guest.environment and guest.environment.artifacts:
            artifacts = guest.environment.artifacts

        if not artifacts:
            return guest_setup_output

        def _run_guest_setup(
            guest_setup_output: SetupGuestReturnType,
            artifacts_group: List[Artifact],
            artifacts_group_type: str
        ) -> SetupGuestReturnType:

            kwargs['r_overloaded_guest_setup_output'] = guest_setup_output

            guest_setup_name = self.get_guest_setup(artifacts_group_type)
            self.require_shared(guest_setup_name)

            self.info('Running guest setup for {} artifact'.format(artifacts_group_type))
            guest_setup_output = self.shared(
                guest_setup_name,
                guest,
                stage=stage,
                log_dirpath=log_dirpath,
                forced_artifacts=artifacts_group,
                **kwargs
            )

            return guest_setup_output

        artifacts_group: List[Artifact] = []

        for artifact in sorted(artifacts, key=lambda x: x.order):

            # Get a type of artifacts group based on first artifact
            artifacts_group_type = artifacts_group[0].type if artifacts_group else None

            # Append artifacts group if it's empty or if it's the same type
            if not artifacts_group_type or artifacts_group_type == artifact.type:
                artifacts_group.append(artifact)
                continue

            # If it's a different type, execute guest setup for the previous group
            _run_guest_setup(guest_setup_output, artifacts_group, artifacts_group_type)

            # Reinitialize artifacts group with the current artifact
            artifacts_group = [artifact]

        # Execute guest setup for the last group
        _run_guest_setup(guest_setup_output, artifacts_group, artifacts_group[0].type)

        return guest_setup_output
