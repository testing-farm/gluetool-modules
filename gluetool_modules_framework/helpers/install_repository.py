# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import re
from dataclasses import dataclass

from functools import cmp_to_key
from version_utils.rpm import compare_packages

import gluetool
from gluetool.action import Action
from gluetool.log import log_dict
from gluetool.result import Ok, Error
from gluetool.utils import Command
from gluetool.glue import GlueCommandError, GlueError

from gluetool_modules_framework.libs.artifacts import splitFilename
from gluetool_modules_framework.libs.guest_setup import guest_setup_log_dirpath, GuestSetupOutput, GuestSetupStage
from gluetool_modules_framework.libs.sut_installation import SUTInstallation
from gluetool_modules_framework.libs.guest import NetworkedGuest
from gluetool_modules_framework.testing_farm.testing_farm_request import TestingFarmRequest

from typing import Any, cast, List, Optional

# accepted artifact types from testing farm request
REPOSITORY_ARTIFACT_TYPE = 'repository'
REPOSITORY_FILE_ARTIFACT_TYPE = 'repository-file'

# Default path to downloading the packages
DEFAULT_DOWNLOAD_PATH = "/var/share/test-artifacts"


@dataclass
class RepositoryArtifact:
    url: str
    packages: Optional[List[str]]


@dataclass
class PackageDetails:
    url: str
    rpm: str
    name: str


class InstallRepository(gluetool.Module):
    """
    Installs packages from specified artifact repository on given guest.
    Downloads all RPMs to a given path (default is {}) and installs them.
    """.format(DEFAULT_DOWNLOAD_PATH)

    name = 'install-repository'
    description = 'Install packages from a given test artifacts repository'

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
        },
    }

    shared_functions = ['setup_guest']

    # If there are several different versions of the same package, keep the latest one
    def _filter_latest_packages(self, packages: List[str]) -> List[str]:
        filtered_packages = []
        package_details_list = []

        for package in packages:
            # skip srpm package
            if package.endswith('src.rpm'):
                continue

            # Remove url part, keep only rpm names
            rpm = package.split('/')[-1]

            splitted_filename = splitFilename(rpm)

            package_details = PackageDetails(
                url=package,
                rpm=rpm,
                name=splitted_filename[0],
            )
            package_details_list.append(package_details)

        unique_package_names = list(set([package_details.name for package_details in package_details_list]))

        for package_name in unique_package_names:

            available_packages = [
                package_details for package_details in package_details_list if package_details.name == package_name
            ]

            # Type ignore here, lambdas syntax does not support annotations
            latest_package = sorted(
                available_packages,
                key=cmp_to_key(lambda x, y: compare_packages(x.rpm, y.rpm)),  # type: ignore
                reverse=True
            )[0]
            filtered_packages.append(latest_package)

        # Sorted here is just for consistent results in tests
        return sorted([package.url for package in filtered_packages])

    def _install_repository_artifacts(
        self,
        guest: NetworkedGuest,
        sut_installation: SUTInstallation,
        artifacts: List[RepositoryArtifact]
    ) -> None:

        download_path = self.option('download-path')

        sut_installation.add_step('Create artifacts directory', 'mkdir -pv {}'.format(download_path),
                                  ignore_exception=True)
        packages = []

        for artifact in artifacts:

            # First, get locations of RPMs on worker
            repoquery_cmd = [
                'dnf',
                'repoquery',
                '-q',
                '--queryformat',
                '"%{{name}}"',
                '--repofrompath=artifacts-repo,{}'.format(artifact.url),
                '--repo',
                'artifacts-repo',
                '--location',
                '--disable-modular-filtering'
            ]
            try:
                output = Command(repoquery_cmd).run()

            except GlueCommandError as exc:
                assert exc.output.stderr is not None
                raise GlueError('Fetching location of RPMs failed: {} cmd: {}'.format(exc.output.stderr, repoquery_cmd))

            if not output.stdout:
                self.warning('No packages have been found in {}'.format(artifact.url))
                continue

            output_packages = output.stdout.strip('\n').split('\n')

            output_packages = self._filter_latest_packages(output_packages)

            if artifact.packages:
                log_dict(
                    self.info,
                    "installing only following packages from repository '{}'".format(artifact.url),
                    artifact.packages
                )

                for out_package in output_packages:
                    if any([artifact_package in out_package for artifact_package in artifact.packages]):
                        packages.append(out_package)

            else:
                packages += output_packages

        sut_installation.add_step('Download packages',
                                  'cd {}; echo {} | xargs -n1 curl -sO'.format(download_path, ' '.join(packages)),
                                  ignore_exception=True)

        # Remove .src.rpm packages
        packages = [package for package in packages if ".src.rpm" not in package]

        # filter excluded packages
        if guest.environment and guest.environment.excluded_packages:
            excluded_packages = guest.environment.excluded_packages
            log_dict(guest.logger.info, 'Excluded packages', excluded_packages)

            excluded_packages_regexp = '|'.join(['/{}'.format(package) for package in excluded_packages])

            packages = [
                rpm_file
                for rpm_file in packages
                if not re.search(excluded_packages_regexp, rpm_file)
            ]

        packages_str = ' '.join(packages)

        # note: the `SUTInstallation` library does the magic of using DNF where it is needed \o/
        sut_installation.add_step('Reinstall packages',
                                  'yum -y reinstall {}'.format(packages_str), ignore_exception=True)
        sut_installation.add_step('Downgrade packages',
                                  'yum -y downgrade {}'.format(packages_str), ignore_exception=True)
        sut_installation.add_step('Update packages',
                                  'yum -y update {}'.format(packages_str), ignore_exception=True)
        sut_installation.add_step('Install packages',
                                  'yum -y install {}'.format(packages_str), ignore_exception=True)

        sut_installation.add_step(
            'Verify all packages installed',
            'basename --suffix=.rpm {} | xargs rpm -q'.format(packages_str)
        )

    def _install_repository_file_artifacts(
        self,
        sut_installation: SUTInstallation,
        repository_file_artifacts: List[str]
    ) -> None:

        for repository_file in repository_file_artifacts:
            sut_installation.add_step(
                'Download repository file',
                'curl --output-dir /etc/yum.repos.d -LO {}'.format(repository_file)
            )

    def setup_guest(
        self,
        guest: NetworkedGuest,
        stage: GuestSetupStage = GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
        log_dirpath: Optional[str] = None,
        **kwargs: Any
    ) -> Any:

        self.require_shared('evaluate_instructions', 'testing_farm_request')
        request = cast(TestingFarmRequest, self.shared('testing_farm_request'))

        log_dirpath = guest_setup_log_dirpath(guest, log_dirpath)

        r_overloaded_guest_setup_output = self.overloaded_shared(
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

        assert guest.environment

        # Get `repository-file` artifacts from TestingEnvironment
        repository_file_artifacts: List[str] = []
        if guest.environment and guest.environment.artifacts:
            repository_file_artifacts = [
                artifact['id'] for artifact in guest.environment.artifacts
                if artifact['type'] == REPOSITORY_FILE_ARTIFACT_TYPE
            ]

        # Get `repository` artifacts from TestingEnvironment
        repository_artifacts: List[RepositoryArtifact] = []
        if guest.environment and guest.environment.artifacts:
            repository_artifacts = [
                RepositoryArtifact(artifact['id'], artifact.get('packages'))
                for artifact in guest.environment.artifacts
                if artifact['type'] == REPOSITORY_ARTIFACT_TYPE
            ]

        # no artifacts to install
        if not (repository_artifacts or repository_file_artifacts):
            return r_overloaded_guest_setup_output

        # get setup from overloaded shared functions
        guest_setup_output = r_overloaded_guest_setup_output.unwrap() or []

        installation_log_dirpath = os.path.join(
            log_dirpath,
            '{}-{}'.format(self.option('log-dir-name'), guest.name)
        )

        sut_installation = SUTInstallation(self, installation_log_dirpath, request, logger=guest.logger)

        # the repository files need to be installed first, they are mainly used to include sidetag repositories
        if repository_file_artifacts:
            self._install_repository_file_artifacts(sut_installation, repository_file_artifacts)

        if repository_artifacts:
            self._install_repository_artifacts(guest, sut_installation, repository_artifacts)

        with Action(
                'installing repositories',
                parent=Action.current_action(),
                logger=guest.logger,
                tags={
                    'guest': {
                        'hostname': guest.hostname,
                        'environment': guest.environment.serialize_to_json()
                    },
                    'request-id': request.id,
                }
        ):
            sut_result = sut_installation.run(guest)

        guest_setup_output += [
            GuestSetupOutput(
                stage=stage,
                label='repository installation',
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
