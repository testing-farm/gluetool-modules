# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os

import gluetool
from gluetool.result import Ok, Error
from gluetool_modules_framework.libs.guest_setup import guest_setup_log_dirpath, GuestSetupOutput, GuestSetupStage, \
    SetupGuestReturnType
from gluetool_modules_framework.libs.sut_installation import SUTInstallation

# Type annotations
from typing import Any, List, Optional  # noqa
from gluetool_modules_framework.libs.guest import NetworkedGuest

# accepted artifact types from testing farm request
TESTING_FARM_ARTIFACT_TYPES = ['fedora-copr-build']


class InstallCoprBuild(gluetool.Module):
    """
    Installs build packages on given guest.
    """

    name = 'install-copr-build'
    description = 'Install build packages on given guest'

    options = {
        'log-dir-name': {
            'help': 'Name of directory where outputs of installation commands will be stored (default: %(default)s).',
            'type': str,
            'default': 'artifact-installation'
        }
    }

    shared_functions = ['setup_guest']

    def __init__(self, *args, **kwargs):
        # type: (Any, Any) -> None
        super(InstallCoprBuild, self).__init__(*args, **kwargs)
        self.request_builds = []  # type: List[str]

    def setup_guest(self, guest, stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION, log_dirpath=None, **kwargs):
        # type: (NetworkedGuest, GuestSetupStage, Optional[str], **Any) -> SetupGuestReturnType

        log_dirpath = guest_setup_log_dirpath(guest, log_dirpath)

        r_overloaded_guest_setup_output = self.overloaded_shared(
            'setup_guest',
            guest,
            stage=stage,
            log_dirpath=log_dirpath,
            **kwargs
        )  # type: SetupGuestReturnType

        if r_overloaded_guest_setup_output is None:
            r_overloaded_guest_setup_output = Ok([])

        if r_overloaded_guest_setup_output.is_error:
            return r_overloaded_guest_setup_output

        if stage != GuestSetupStage.ARTIFACT_INSTALLATION:
            return r_overloaded_guest_setup_output

        builds = self.request_builds or self.shared('tasks')

        # no artifact to install
        if not builds:
            return r_overloaded_guest_setup_output

        guest_setup_output = r_overloaded_guest_setup_output.unwrap() or []

        installation_log_dirpath = os.path.join(
            log_dirpath,
            '{}-{}'.format(self.option('log-dir-name'), guest.name)
        )

        sut_installation = SUTInstallation(self, installation_log_dirpath, builds[0], logger=guest.logger)
        rpm_urls = []  # type: List[str]

        for number, build in enumerate(builds, 1):
            sut_installation.add_step(
                'Download copr repository',
                'curl {{}} --retry 5 --output /etc/yum.repos.d/copr_build-{}-{}.repo'.format(
                    build.project.replace('/', '_'), number
                ),
                items=build.repo_url
            )

            # reinstall command has to be called for each rpm separately, hence list of rpms is used
            sut_installation.add_step('Reinstall packages', 'yum -y reinstall {}',
                                      items=build.rpm_urls, ignore_exception=True)

            # downgrade, update and install commands are called just once with all rpms followed, hence list of
            # rpms is joined to one item
            rpm_urls.extend(build.rpm_urls)

        joined_rpm_urls = ' '.join(rpm_urls)

        sut_installation.add_step('Download packages', 'curl --retry 5 -LO {}',
                                  items=rpm_urls)
        sut_installation.add_step('Downgrade packages', 'yum -y downgrade {}',
                                  items=joined_rpm_urls, ignore_exception=True)
        sut_installation.add_step('Update packages', 'yum -y update {}',
                                  items=joined_rpm_urls, ignore_exception=True)
        sut_installation.add_step('Install packages', 'yum -y install {}',
                                  items=joined_rpm_urls, ignore_exception=True)

        for build in builds:
            sut_installation.add_step('Verify packages installed', 'rpm -q {}', items=build.rpm_names)

        sut_result = sut_installation.run(guest)

        guest_setup_output += [
            GuestSetupOutput(
                stage=stage,
                label='Copr build(s) installation',
                log_path=installation_log_dirpath,
                additional_data=sut_installation
            )
        ]

        if sut_result.is_error:
            assert sut_result.error is not None

            return Error((
                guest_setup_output,
                sut_result.error
            ))

        return Ok(guest_setup_output)

    def execute(self):
        # type: () -> None

        # we definitely need these shared functions available for the module to function
        self.require_shared('primary_task', 'tasks')

        # if no testing farm request, nothing to initialize from
        if not self.has_shared('testing_farm_request'):
            return

        # extract ids from the request
        self.request = self.shared('testing_farm_request')

        if not self.request.environments_requested[0]['artifacts']:
            return

        artifact_ids = [
            artifact['id'] for artifact in self.request.environments_requested[0]['artifacts']
            if artifact['type'] in TESTING_FARM_ARTIFACT_TYPES
        ]

        self.request_builds = self.shared('tasks', task_ids=artifact_ids)
