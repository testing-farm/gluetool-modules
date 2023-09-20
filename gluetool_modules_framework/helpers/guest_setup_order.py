# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
from gluetool.utils import PatternMap
from gluetool.glue import GlueError
from gluetool.result import Ok
from gluetool_modules_framework.libs.guest_setup import guest_setup_log_dirpath, GuestSetupStage
from gluetool_modules_framework.libs.guest import NetworkedGuest

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
            'help': 'Mapping betwee artifact type and related guest setup shared function (default: none).',
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

        guest_setup_output = r_overloaded_guest_setup_output or Ok([])

        artifacts = []
        if guest.environment and guest.environment.artifacts:
            artifacts = guest.environment.artifacts

        if not artifacts:
            return guest_setup_output

        for artifact in sorted(artifacts, key=lambda x: x.order):

            kwargs['r_overloaded_guest_setup_output'] = guest_setup_output

            guest_setup_name = self.get_guest_setup(artifact.type)
            self.require_shared(guest_setup_name)

            self.info('Running guest setup for {} artifact'.format(artifact.type))
            guest_setup_output = self.shared(
                guest_setup_name,
                guest,
                stage=stage,
                log_dirpath=log_dirpath,
                **kwargs
            )

        return guest_setup_output
