# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import gluetool
from gluetool.action import Action
from gluetool.log import log_dict
from gluetool.result import Ok, Error

from gluetool_modules_framework.libs.artifacts import DEFAULT_DOWNLOAD_PATH, package_list_path
from gluetool_modules_framework.libs.guest_setup import guest_setup_log_dirpath, GuestSetupOutput, GuestSetupStage, \
    SetupGuestReturnType
from gluetool_modules_framework.libs.sut_installation import SUTInstallation
from gluetool_modules_framework.libs.guest import NetworkedGuest
from gluetool_modules_framework.libs.repo import create_repo
from gluetool_modules_framework.testing_farm.testing_farm_request import TestingFarmRequest, Artifact

from typing import Any, List, Optional, cast, Dict  # noqa

# accepted artifact types from testing farm request
TESTING_FARM_ARTIFACT_TYPES = ['fedora-koji-build', 'redhat-brew-build']


class InstallKojiBuildExecute(gluetool.Module):
    """
    Installs packages from specified rhel module on given guest. Calls given ansible playbook
    which downloads repofile and installs module.
    """

    name = 'install-koji-build-execute'
    description = 'Install one or more koji builds on given guest'

    shared_functions = ['setup_guest', 'setup_guest_install_koji_build_execute']

    options = {
        'log-dir-name': {
            'help': 'Name of directory where outputs of installation commands will be stored (default: %(default)s).',
            'type': str,
            'default': 'artifact-installation'
        },
        'download-i686-builds': {
            'help': 'If set, download both x86_64 and i686 packages on x86_64 guest',
            'action': 'store_true'
        },
        'download-path': {
            'help': 'Path of the directory where all the packages will be downloaded to (default: %(default)s).',
            'type': str,
            'default': DEFAULT_DOWNLOAD_PATH
        }
    }

    def _extract_artifacts(self, guest: NetworkedGuest) -> List[Artifact]:
        """
        Extracts artifacts of acceptable types from guest's TestingEnvironment
        """
        artifacts = []
        if guest.environment and guest.environment.artifacts:
            artifacts = [
                artifact for artifact in guest.environment.artifacts
                if artifact.type in TESTING_FARM_ARTIFACT_TYPES
            ]
        return artifacts

    def setup_guest_install_koji_build_execute(
        self,
        guest: NetworkedGuest,
        stage: GuestSetupStage = GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
        log_dirpath: Optional[str] = None,
        r_overloaded_guest_setup_output: Optional[SetupGuestReturnType] = None,
        forced_artifacts: Optional[List[Artifact]] = None,
        **kwargs: Any
    ) -> SetupGuestReturnType:

        self.require_shared('evaluate_instructions', 'testing_farm_request')

        log_dirpath = guest_setup_log_dirpath(guest, log_dirpath)

        r_overloaded_guest_setup_output = r_overloaded_guest_setup_output or Ok([])

        if r_overloaded_guest_setup_output.is_error or stage != GuestSetupStage.ARTIFACT_INSTALLATION:
            return r_overloaded_guest_setup_output

        download_path = cast(str, self.option('download-path'))

        artifacts = forced_artifacts if forced_artifacts else self._extract_artifacts(guest)

        # no artifacts to test
        if not artifacts:
            return r_overloaded_guest_setup_output

        # excluded packages
        excluded_packages: List[str] = []
        if guest.environment:
            excluded_packages = guest.environment.excluded_packages or []

        if excluded_packages:
            log_dict(self.info, 'Excluded packages', excluded_packages)

        guest_setup_output = r_overloaded_guest_setup_output.unwrap() or []

        installation_log_dirpath = os.path.join(
            log_dirpath,
            '{}-{}'.format(self.option('log-dir-name'), guest.name)
        )

        request = cast(TestingFarmRequest, self.shared('testing_farm_request'))

        sut_installation = SUTInstallation(self, installation_log_dirpath, request, logger=guest)

        assert guest.environment is not None

        arch = guest.environment.arch

        rpms_lists_to_skip_install = []

        try:
            guest.execute('type bootc && sudo bootc status && ((sudo bootc status --format yaml | grep -e "booted: null" -e "image: null") && exit 1 || exit 0)')  # noqa: E501
            has_bootc = True
        except gluetool.glue.GlueCommandError:
            has_bootc = False

        for artifact in artifacts:
            koji_command = 'koji' if 'fedora' in artifact.type else 'brew'

            if arch == 'x86_64' and self.option('download-i686-builds'):
                download_arches = 'x86_64 --arch i686'
            else:
                download_arches = cast(str, arch)

            sut_installation.add_step(
                'Download task id {}'.format(artifact.id),
                (
                    'set -o pipefail; '
                    '( {0} download-build --debuginfo --task-id --arch noarch --arch {2} --arch src {1} || '
                    '{0} download-task --arch noarch --arch {2} --arch src {1} ) | '
                    'egrep Downloading | cut -d " " -f 3 | tee rpms-list-{1}'
                ).format(koji_command, artifact.id, download_arches),
                local=has_bootc
            )

            if artifact.install is False:
                rpms_lists_to_skip_install.append('rpms-list-{}'.format(artifact.id))

        # Copy all rpms to the destination directory and make them available in a repo. The files are duplicated to
        # avoid breaking anything relying on them being present under the original path.
        # Package names are appended to the repo package list once successfuly copied.
        if not has_bootc:
            sut_installation.add_step('Copy rpms to the repo directory', (
                f'mkdir -pv {download_path}; cat rpms-list-* | xargs -n1 bash -c '
                f'"cp -t {download_path} \\$1 && '
                f'echo $(basename \\$1) >> {package_list_path(basepath=download_path)}" --'
            ))
            create_repo(sut_installation, 'test-artifacts', download_path)

        excluded_packages_regexp = '|'.join(['^{} '.format(package) for package in excluded_packages])

        sut_installation.add_step(
            'Get package list',
            (
                'ls *[^.src].rpm | '
                'sed -r "s/(.*)-.*-.*/\\1 \\0/" | '
                '{}'  # Do not install excluded packages in the tmt plan
                '{}'  # Do not install packages with "install: false" specified in the TF API
                '{}'  # Do not install i686 builds
                'awk "{{print \\$2}}" | '
                'tee rpms-list'
            ).format(
                'egrep -v "({})" | '.format(excluded_packages_regexp) if excluded_packages_regexp else '',
                ''.join(['grep -Fv "$(cat {})" | '.format(rpm) for rpm in rpms_lists_to_skip_install]),
                'egrep -v "i686" | ' if arch == 'x86_64' and self.option('download-i686-builds') else '',
            ),
            local=has_bootc
        )

        try:
            guest.execute('command -v dnf')
            has_dnf = True
        except gluetool.glue.GlueCommandError:
            has_dnf = False

        if not has_bootc:
            if has_dnf:
                # HACK: this is *really* awkward wrt. error handling:
                # https://bugzilla.redhat.com/show_bug.cgi?id=1831022
                sut_installation.add_step(
                    'Reinstall packages',
                    'dnf -y reinstall $(cat rpms-list) || true'
                )

                sut_installation.add_step(
                    'Install packages',
                    r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];"""
                    'then dnf -y install $(cat rpms-list);'
                    'else echo "Nothing to install, rpms-list is empty"; fi'
                )
            else:
                sut_installation.add_step(
                    'Reinstall packages',
                    'yum -y reinstall $(cat rpms-list)',
                    ignore_exception=True,
                )

                # yum install refuses downgrades, do it explicitly
                sut_installation.add_step(
                    'Downgrade packages',
                    'yum -y downgrade $(cat rpms-list)',
                    ignore_exception=True,
                )

                sut_installation.add_step(
                    'Install packages',
                    'yum -y install $(cat rpms-list)',
                    ignore_exception=True,
                )
            # Use printf to correctly quote the package name, we encountered '^' in the NVR, which is actually a valid
            # character in NVR - https://docs.fedoraproject.org/en-US/packaging-guidelines/Versioning/#_snapshots
            #
            # Explicitely pass delimiter for xargs to mitigate special handling of quotes, which would break the
            # quoting done previously by printf

            # The step won't work for image mode because rpms-list is gone between reboots.
            sut_installation.add_step(
                'Verify all packages installed',
                r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];"""
                "then sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\\n' | xargs -d'\\n' rpm -q;"
                "else echo 'Nothing to verify, rpms-list is empty'; fi"
            )

        else:
            command = (
                "cat rpms-list | xargs realpath | tee rpms-list-paths && "
                "if [ -s rpms-list-paths ] && grep -q '\\S' rpms-list-paths; then "
                "{} -vvv run provision --how connect --guest {} --key {} --port {} prepare --how install "
                "$(awk '{{print \"--package=\"$0}}' rpms-list-paths); else echo 'Nothing to install'; fi"
            ).format(" ".join(self.shared('tmt_command')), guest.hostname, guest.key, guest.port)

            sut_installation.add_step(
                label='Install koji build with TMT',
                command=command,
                items=[],
                ignore_exception=False,
                callback=None,
                local=True,
                env={'DEBUG': '1'}
            )

        assert request is not None

        with Action(
            'installing rpm artifacts',
            parent=Action.current_action(),
            logger=guest.logger,
            tags={
                'guest': {
                    'hostname': guest.hostname,
                    'environment': guest.environment.serialize_to_json()
                },
                'artifact-id': request.id,
                'artifact-type': request.ARTIFACT_NAMESPACE
            }
        ):
            sut_result = sut_installation.run(guest)

        guest_setup_output += [
            GuestSetupOutput(
                stage=stage,
                label='build installation',
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

    def setup_guest(
        self,
        guest: NetworkedGuest,
        stage: GuestSetupStage = GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
        log_dirpath: Optional[str] = None,
        **kwargs: Any
    ) -> Any:

        log_dirpath = guest_setup_log_dirpath(guest, log_dirpath)

        r_overloaded_guest_setup_output = self.overloaded_shared(
            'setup_guest',
            guest,
            stage=stage,
            log_dirpath=log_dirpath,
            **kwargs
        )

        return self.setup_guest_install_koji_build_execute(
            guest,
            stage,
            log_dirpath,
            r_overloaded_guest_setup_output,
            **kwargs
        )
