# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import re

import gluetool
from gluetool.log import log_dict
from gluetool.result import Ok, Error
from gluetool_modules_framework.infrastructure.copr import CoprTask
from gluetool_modules_framework.libs.guest_setup import guest_setup_log_dirpath, GuestSetupOutput, GuestSetupStage, \
    SetupGuestReturnType
from gluetool_modules_framework.libs.sut_installation import SUTInstallation
from gluetool_modules_framework.libs.guest import NetworkedGuest
from gluetool_modules_framework.libs.test_schedule import TestScheduleEntry
from gluetool_modules_framework.testing_farm.testing_farm_request import Artifact

# Type annotations
from typing import cast, Any, List, Optional  # noqa

# accepted artifact types from testing farm request
TESTING_FARM_ARTIFACT_TYPES = ['fedora-copr-build']

# Default path to downloading the packages
DEFAULT_DOWNLOAD_PATH = "/var/share/test-artifacts"


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
        },
        'download-path': {
            'help': 'Path of the directory where all the packages will be downloaded to (default: %(default)s).',
            'type': str,
            'default': DEFAULT_DOWNLOAD_PATH
        }
    }

    shared_functions = ['setup_guest']

    def setup_guest(
        self,
        guest: NetworkedGuest,
        schedule_entry: Optional[TestScheduleEntry] = None,
        stage: GuestSetupStage = GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
        log_dirpath: Optional[str] = None,
        **kwargs: Any
    ) -> SetupGuestReturnType:

        download_path = cast(str, self.option('download-path'))

        self.require_shared('tasks')

        log_dirpath = guest_setup_log_dirpath(guest, log_dirpath)

        r_overloaded_guest_setup_output: SetupGuestReturnType = self.overloaded_shared(
            'setup_guest',
            guest,
            stage=stage,
            log_dirpath=log_dirpath,
            **kwargs
        )

        if r_overloaded_guest_setup_output is None:
            r_overloaded_guest_setup_output = Ok([])

        if r_overloaded_guest_setup_output.is_error:
            return r_overloaded_guest_setup_output

        if stage != GuestSetupStage.ARTIFACT_INSTALLATION:
            return r_overloaded_guest_setup_output

        # Filter artifacts of the acceptable type from guest's TestingEnvironment
        artifacts: List[Artifact] = []
        if guest.environment and guest.environment.artifacts:
            artifacts = [
                artifact for artifact in guest.environment.artifacts
                if artifact.type in TESTING_FARM_ARTIFACT_TYPES
            ]

        builds = cast(Optional[List[CoprTask]], self.shared('tasks', task_ids=[artifact.id for artifact in artifacts]))

        # no artifact to install
        if not builds:
            return r_overloaded_guest_setup_output

        # excluded packages taken from testing environment
        excluded_packages: List[str] = []
        if guest.environment and guest.environment.excluded_packages:
            excluded_packages = guest.environment.excluded_packages
            log_dict(guest.logger.info, 'Excluded packages', excluded_packages)

        guest_setup_output = r_overloaded_guest_setup_output.unwrap() or []

        installation_log_dirpath = os.path.join(
            log_dirpath,
            '{}-{}'.format(self.option('log-dir-name'), guest.name)
        )

        # TODO: `builds[0]` might be misleading - it is passing a single artifact to the `SUTInstallation` class even
        # though the object is used to process all artifacts. This shouldn't affect the functionality, the single passed
        # artifact is used only for logging purposes.
        sut_installation = SUTInstallation(self, installation_log_dirpath, builds[0], logger=guest.logger)

        # create artifacts directory
        sut_installation.add_step(
            'Create artifacts directory',
            'mkdir -pv {}'.format(download_path),
            ignore_exception=True
        )

        rpm_urls: List[str] = []

        try:
            guest.execute('command -v dnf')
            has_dnf = True
        except gluetool.glue.GlueCommandError:
            has_dnf = False

        for number, (build, artifact) in enumerate(zip(builds, artifacts), 1):
            sut_installation.add_step(
                'Download copr repository',
                'curl -v {{}} --retry 5 --output /etc/yum.repos.d/copr_build-{}-{}.repo'.format(
                    build.project.replace('/', '_'), number
                ),
                items=build.repo_url
            )

            # download all artifacts, including excluded
            sut_installation.add_step(
                'Download rpms from copr',
                (
                    'cd {} && '
                    'curl -sL --retry 5 --remote-name-all -w "Downloaded: %{{url_effective}}\\n" {}'
                ).format(download_path, ' '.join(build.rpm_urls + build.srpm_urls))
            )

            if artifact.install is False:
                continue

            copr_build_rpm_urls = build.rpm_urls

            # exclude packages if requested before installation flow, note that we are matching URLs,
            # so search with '/' prefix
            if excluded_packages:
                # create regexp for excluding packages, note that we will be filtering URLs, thus the slash
                excluded_packages_regexp = '|'.join(['/{}'.format(package) for package in excluded_packages])
                excluded_packages_regexp_compiled = re.compile(r'({})'.format(excluded_packages_regexp))
                copr_build_rpm_urls = [
                    rpm_url
                    for rpm_url in build.rpm_urls
                    if not re.search(excluded_packages_regexp_compiled, rpm_url)
                ]

            # reinstall command has to be called for each rpm separately, hence list of rpms is used
            if copr_build_rpm_urls:
                if has_dnf:
                    # HACK: this is really awkward wrt. error handling:
                    #       https://bugzilla.redhat.com/show_bug.cgi?id=1831022
                    sut_installation.add_step(
                        'Reinstall packages', 'dnf -y reinstall {} || true', items=copr_build_rpm_urls
                    )
                else:
                    sut_installation.add_step('Reinstall packages', 'yum -y reinstall {}',
                                              items=copr_build_rpm_urls, ignore_exception=True)

            # install command is called just once with all rpms followed, hence list of
            # rpms is joined to one item
            rpm_urls.extend(copr_build_rpm_urls)

        joined_rpm_urls = ' '.join(rpm_urls)

        if joined_rpm_urls:
            if has_dnf:
                sut_installation.add_step('Install packages', 'dnf -y install {}', items=joined_rpm_urls)
            else:
                # yum install refuses downgrades, do it explicitly
                sut_installation.add_step('Downgrade packages', 'yum -y downgrade {}',
                                          items=joined_rpm_urls, ignore_exception=True)
                sut_installation.add_step('Install packages', 'yum -y install {}',
                                          items=joined_rpm_urls, ignore_exception=True)

        for build, artifact in zip(builds, artifacts):
            if artifact.install is False:
                continue

            rpm_names = build.rpm_names

            if excluded_packages:
                # create regexp for excluding packages, note that we will filtering rpm names,
                # so start with beginning of line
                excluded_packages_regexp = '|'.join(['^{}'.format(package) for package in excluded_packages])
                excluded_packages_regexp_compiled = re.compile(r'({})'.format(excluded_packages_regexp))
                rpm_names = [
                    rpm_name
                    for rpm_name in build.rpm_names
                    if not re.search(excluded_packages_regexp_compiled, rpm_name)
                ]

            if rpm_names:
                sut_installation.add_step('Verify packages installed', 'rpm -q {}', items=rpm_names)

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
