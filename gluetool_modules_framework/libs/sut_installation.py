# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import collections
import re
import os
import gluetool
from gluetool import SoftGlueError
from gluetool.log import log_dict
from gluetool.result import Ok, Error
from gluetool.utils import Result
from gluetool_modules_framework.libs.sentry import ArtifactFingerprintsMixin
from gluetool_modules_framework.libs import run_and_log

from jq import jq

from .artifacts import artifacts_location

# Type annotations
from typing import TYPE_CHECKING, cast, Any, Dict, List, Tuple, Union, Optional, Callable  # noqa

if TYPE_CHECKING:
    import gluetool_modules_framework.libs.guest # noqa

#: Step callback type
StepCallbackType = Callable[[str, gluetool.utils.ProcessOutput], Optional[str]]

#: Describes one command used to SUT installtion
#:
#: :ivar str label: Label used for logging.
#: :ivar str command: Command to execute on the guest, executed once for each item from items.
#:                    It can contain a placeholder ({}) which is substituted by the current item.
#: :ivar list(str) items: Items to execute command with replaced to `command`.
#: :ivar bool ignore_exception: Indicates whether to raise `SUTInstallationFailedError` when command fails.
#: :ivar Callable callback: Callback to additional processing of command output.
SUTStep = collections.namedtuple(
    'SUTStep', ['label', 'command', 'items', 'ignore_exception', 'callback']
)

# Pattern for dnf commands which will be extended with --allowerasing
ALLOW_ERASING_PATTERN = re.compile(r'\b(install|update|reinstall|downgrade)\b')

INSTALL_COMMANDS_FILE = 'sut_install_commands.sh'


class SUTInstallationFailedError(ArtifactFingerprintsMixin, SoftGlueError):
    def __init__(
        self,
        artifact,                        # type: Any
        guest,                           # type: Optional[gluetool_modules_framework.libs.guest.Guest]
        items=None,                      # type: Any
        reason=None,                     # type: Optional[str]
        installation_logs=None,          # type: Optional[str]
        installation_logs_location=None  # type: Optional[str]
    ):
        # type: (...) -> None

        if reason:
            super(SUTInstallationFailedError, self).__init__(
                artifact,
                'Test environment installation failed: {}'.format(reason)
            )
        else:
            super(SUTInstallationFailedError, self).__init__(
                artifact,
                'Test environment installation failed: reason unknown, please escalate'
            )

        self.guest = guest
        self.items = items
        self.reason = reason
        self.installation_logs = installation_logs
        self.installation_logs_location = installation_logs_location


class SUTInstallation(object):

    def __init__(self, module, log_dirpath, artifact, logger=None):
        # type: (gluetool.Module, str, Any, Optional[gluetool.log.ContextAdapter]) -> None

        self.module = module
        self.log_dirpath = log_dirpath
        self.artifact = artifact
        self.steps = []  # type: List[SUTStep]
        self.logger = logger or gluetool.log.Logging.get_logger()

    def add_step(self, label, command, items=None, ignore_exception=False, callback=None):
        # type: (str, str, Union[Optional[str], Optional[List[str]]], bool, Optional[StepCallbackType]) -> None

        if not items:
            items = []

        if not isinstance(items, list):
            items = [items]

        self.steps.append(SUTStep(label, command, items, ignore_exception, callback))

    def run(self, guest):
        # type: (gluetool_modules_framework.libs.guest.NetworkedGuest) -> Result[None, SUTInstallationFailedError]

        commands = []

        try:
            guest.execute('command -v dnf')
            dnf_present = True
        except gluetool.glue.GlueCommandError:
            dnf_present = False

        if not os.path.exists(self.log_dirpath):
            os.mkdir(self.log_dirpath)

        logs_location = artifacts_location(self.module, self.log_dirpath, logger=guest.logger)

        for i, step in enumerate(self.steps):
            guest.info(step.label)

            log_filename = '{}-{}.txt'.format(i, step.label.replace(' ', '-'))
            log_filepath = os.path.join(self.log_dirpath, log_filename)

            command = step.command

            # replace yum with dnf in case dnf is present on guest
            if dnf_present and command.startswith('yum'):
                command = '{}{}'.format('dnf', command[3:])

            # always use `--allowerasing` with `dnf install`
            if command.startswith('dnf'):
                dnf_command = 'dnf'
                if re.search(ALLOW_ERASING_PATTERN, command):
                    dnf_command = 'dnf --allowerasing'
                command = '{}{}'.format(dnf_command, command[3:])

            if not step.items:
                commands.append(command)
                command_failed, error_message, output = run_and_log(
                    [command],  # `command` is a string, we need to send it as List[str]
                    log_filepath,
                    # our `command` is assigned to this `cmd`, and here we convert it
                    # to string to work with guest.execute
                    lambda cmd: guest.execute(cmd[0]),
                    callback=step.callback
                )

                if command_failed and not step.ignore_exception:
                    return Error(
                        SUTInstallationFailedError(
                            self.artifact,
                            guest,
                            reason=error_message,
                            installation_logs=self.log_dirpath,
                            installation_logs_location=logs_location
                        )
                    )

            for item in step.items:
                # `step.command` contains `{}` to indicate place where item is substitute.
                # e.g 'yum install -y {}'.format('ksh')
                final_command = command.format(item)
                commands.append(final_command)

                command_failed, error_message, output = run_and_log(
                    [final_command],  # `final_command` is a string, we need to send it as List[str]
                    log_filepath,
                    # our `final_command` is assigned to this `cmd`, and here we convert it
                    # to string to work with guest.execute
                    lambda cmd: guest.execute(cmd[0]),
                    callback=step.callback
                )

                if not command_failed:
                    continue

                if step.ignore_exception:
                    continue

                if error_message:
                    self.logger.error(error_message)

                    return Error(
                        SUTInstallationFailedError(
                            self.artifact,
                            guest,
                            items=item,
                            reason=error_message,
                            installation_logs=self.log_dirpath,
                            installation_logs_location=logs_location
                        )
                    )

                return Error(
                    SUTInstallationFailedError(
                        self.artifact,
                        guest,
                        items=item,
                        installation_logs=self.log_dirpath,
                        installation_logs_location=logs_location
                    )
                )

        # record the install commands
        with open(os.path.join(self.log_dirpath, INSTALL_COMMANDS_FILE), 'a') as f:
            for command in commands:
                f.write(command + '\n')

        return Ok(None)


def check_ansible_sut_installation(ansible_output,  # type: Dict[str, Any]
                                   guest,  # type: gluetool_modules_framework.libs.guest.NetworkedGuest
                                   artifact,  # type: Any
                                   logger=None  # type: Optional[gluetool.log.ContextAdapter]
                                  ):  # noqa
    # type: (...) -> None
    """
    Checks json output of ansible call. Raises ``SUTInstallationFailedError`` if some of
    ansible installation tasks failed.

    :param ansible_output: output (in json format) to be checked
    :param guest: guest where playbook was run
    :param artifact: Object covering installed artifact
    :param logger: Logger object used to log
    :raises SUTInstallationFailedError: if some of ansible installation tasks failed
    """

    logger = logger or gluetool.log.Logging.get_logger()

    log_dict(logger.debug,
             'ansible output before jq processing',
             ansible_output)

    query = """
          .plays[].tasks[].hosts
        | to_entries[]
        | select(.value.results != null)
        | {
            host: .key,
            items: [
                  .value.results[]
                | select(.failed==true)
                | .item
            ]
          }
        | select(.items != [])""".replace('\n', '')

    failed_tasks = jq(query).transform(ansible_output, multiple_output=True)

    log_dict(logger.debug,
             'ansible output after jq processing',
             failed_tasks)

    if not failed_tasks:
        return

    first_fail = failed_tasks[0]
    failed_modules = first_fail['items']

    guest.warn('Following items have not been installed: {}'.format(','.join(failed_modules)))
    raise SUTInstallationFailedError(artifact, guest, failed_modules)
