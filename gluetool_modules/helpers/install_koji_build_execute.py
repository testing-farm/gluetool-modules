import json
import os
import re
import gluetool
from gluetool.action import Action
from gluetool.log import log_dict
from gluetool.result import Ok, Error
from gluetool.utils import Command, normalize_shell_option, render_template
from gluetool import GlueError

from gluetool_modules.libs.guest_setup import guest_setup_log_dirpath, GuestSetupOutput, GuestSetupStage
from gluetool_modules.libs.sut_installation import SUTInstallation

# accepted artifact types from testing farm request
TESTING_FARM_ARTIFACT_TYPES = ['fedora-koji-build', 'redhat-brew-build']


class InstallKojiBuildExecute(gluetool.Module):
    """
    Installs packages from specified rhel module on given guest. Calls given ansible playbook
    which downloads repofile and installs module.
    """

    name = 'install-koji-build-execute'
    description = 'Install one or more koji builds on given guest'

    shared_functions = ('setup_guest',)

    options = {
        'log-dir-name': {
            'help': 'Name of directory where outputs of installation commands will be stored (default: %(default)s).',
            'type': str,
            'default': 'artifact-installation'
        },
    }

    def __init__(self, *args, **kwargs):
        super(InstallKojiBuildExecute, self).__init__(*args, **kwargs)
        self.request = None
        self.request_artifacts = None

    @gluetool.utils.cached_property
    def installation_workarounds(self):
        if not self.option('installation-workarounds'):
            return []

        return gluetool.utils.load_yaml(self.option('installation-workarounds'), logger=self.logger)

    def setup_guest(self,
        guest,
        schedule_entry=None,
        stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
        log_dirpath=None,
        **kwargs
    ):
        # type: (Guest, GuestSetupStage, Optional[TestScheduleEntry], Optional[str], **Any) -> SetupGuestReturnType

        self.require_shared('evaluate_instructions')

        log_dirpath = guest_setup_log_dirpath(guest, log_dirpath)

        r_overloaded_guest_setup_output = self.overloaded_shared(
            'setup_guest',
            guest,
            schedule_entry=schedule_entry,
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

        # no artifacts to test
        if not self.request_artifacts:
            return r_overloaded_guest_setup_output

        excluded_packages = schedule_entry.excludes if schedule_entry and hasattr(schedule_entry, 'excludes') else []

        if excluded_packages:
            assert schedule_entry
            log_dict(schedule_entry.logger.info, 'excluded_packages', excluded_packages)

        guest_setup_output = r_overloaded_guest_setup_output.unwrap() or []

        installation_log_dirpath = os.path.join(
            log_dirpath,
            '{}-{}'.format(self.option('log-dir-name'), guest.name)
        )

        sut_installation = SUTInstallation(self, installation_log_dirpath, self.request, logger=guest)

        # callback for 'commands' item in installation_workarounds
        def _add_step_callback(instruction, command, argument, context):
            for step in argument:
                sut_installation.add_step(step['label'], step['command'])

        self.shared('evaluate_instructions', self.installation_workarounds, {
            'steps': _add_step_callback,
        })

        # TODO: hack, for multi-arch suppport, actually the arch should come from guest I guess ...
        try:
            arch = self.shared('testing_farm_request').environments_requested[0]['arch']
        except (AttributeError, KeyError, IndexError):
            arch = 'x86_64'

        for artifact in self.request_artifacts:
            koji_command = 'koji' if 'fedora' in artifact['type'] else 'brew'

            sut_installation.add_step(
                'Download task id {}'.format(artifact['id']),
                (
                    '{0} download-build --debuginfo --task-id --arch noarch --arch {2} --arch src {1} || '
                    '{0} download-task --arch noarch --arch {2} --arch src {1}'
                ).format(koji_command, artifact['id'], arch)
            )

        excluded_packages_regexp = '|'.join(['^{} '.format(package) for package in excluded_packages])

        sut_installation.add_step(
            'Get package list',
            (
                'ls *[^.src].rpm | '
                'sed -r "s/(.*)-.*-.*/\\1 \\0/" | '
                '{}'
                'awk "{{print $2}}" | '
                'tee rpms-list'
            ).format(
                'egrep -v "({})" | '.format(excluded_packages_regexp)
                if excluded_packages_regexp else ''
            )
        )

        sut_installation.add_step(
            'Reinstall packages',
            'yum -y reinstall $(cat rpms-list)',
            ignore_exception=True,
            allow_erasing=True
        )

        sut_installation.add_step(
            'Downgrade packages',
            'yum -y downgrade $(cat rpms-list)',
            ignore_exception=True,
            allow_erasing=True
        )

        sut_installation.add_step(
            'Update packages',
            'yum -y update $(cat rpms-list)',
            ignore_exception=True,
            allow_erasing=True
        )

        sut_installation.add_step(
            'Install packages',
            'yum -y install $(cat rpms-list)',
            ignore_exception=True,
            allow_erasing=True
        )

        sut_installation.add_step(
            'Verify all packages installed',
            "sed 's/.rpm$//' rpms-list | xargs rpm -q"
        )

        with Action(
            'installing rpm artifacts',
            parent=Action.current_action(),
            logger=guest.logger,
            tags={
                'guest': {
                    'hostname': guest.hostname,
                    'environment': guest.environment.serialize_to_json()
                },
                'artifact-id': self.request.id,
                'artifact-type': self.request.ARTIFACT_NAMESPACE
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

    def execute(self):
        if not self.has_shared('testing_farm_request'):
            return

        # extract ids from the request
        self.request = self.shared('testing_farm_request')

        if not self.request.environments_requested[0]['artifacts']:
            return

        # TODO: currently we support only installation of koji builds, ignore other artifacts
        # TODO: environment should be coming from test scheduler later
        self.request_artifacts = [
            artifact for artifact in self.request.environments_requested[0]['artifacts']
            if artifact['type'] in TESTING_FARM_ARTIFACT_TYPES
        ]
