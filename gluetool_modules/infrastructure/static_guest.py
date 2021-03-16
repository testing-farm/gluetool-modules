# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import collections
import re
import socket
from concurrent.futures import ThreadPoolExecutor, wait, Future

import gluetool
from gluetool import GlueError
from gluetool.result import Ok
from gluetool.utils import Command, IncompatibleOptionsError
from gluetool_modules.libs.guest import NetworkedGuest, Guest, GuestConnectionError
from gluetool_modules.libs.guest_setup import GuestSetupOutput

from gluetool_modules.libs.testing_environment import TestingEnvironment

# Type annotations
from typing import Any, Dict, List, Optional, NamedTuple, Set, Union, cast  # noqa
from typing_extensions import Literal

# SSH connection defaults
DEFAULT_SSH_USER = 'root'
DEFAULT_SSH_OPTIONS = ['UserKnownHostsFile=/dev/null', 'StrictHostKeyChecking=no']

# wait_alive defaults
DEFAULT_BOOT_TIMEOUT = 10
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_ECHO_TIMEOUT = 10

# Hostnames recognized as localhost connections
LOCALHOST_GUEST_HOSTNAMES = ['127.0.0.1', 'localhost']

#: Generic provisioner capabilities.
#: Follows :doc:`Provisioner Capabilities Protocol </protocols/provisioner-capabilities>`.
ProvisionerCapabilities = collections.namedtuple('ProvisionerCapabilities', ['available_arches'])

# without the 'type: ignore', mypy reports: 'error: Missing type parameters for generic type "Future"', however, adding
# a type parameter in python older than 3.9 results in "TypeError: 'type' object has no attribute '__getitem__'" while
# running the code
WaitType = NamedTuple('WaitType', (
    ('done', Set[Future]),  # type: ignore
    ('not_done', Set[Future])  # type: ignore
))


class StaticGuest(NetworkedGuest):
    """
    StaticGuest is like py:class:`gluetool_modules.libs.guests.NetworkedGuest`, just it does allow degraded services.
    """

    def _is_allowed_degraded(self, service):
        # type: (Any) -> Literal[True]
        return True

    def __init__(self, module, fqdn, **kwargs):
        # type: (CIStaticGuest, str, **Any) -> None
        super(StaticGuest, self).__init__(module, fqdn, **kwargs)

        try:
            # we expect the machines to be already booted really, timeouts are low
            self.wait_alive(
                boot_timeout=module.option('boot-timeout'), boot_tick=2,
                connect_timeout=module.option('connect-timeout'), connect_tick=2,
                echo_timeout=module.option('echo-timeout'), echo_tick=2)

        except (socket.gaierror, GlueError) as error:
            raise GlueError("Error connecting to guest '{}': {}".format(self, error))

        # populate guest architecture from the OS`
        arch = self.execute('arch').stdout
        if not arch:
            raise GlueError('Error retrieving guest architecture')
        self.environment = TestingEnvironment(arch=arch.rstrip(), compose=None)


class StaticLocalhostGuest(Guest):
    """
    StaticLocalhostGuest provides access to the local machine under the same user running the module.
    """

    def _is_allowed_degraded(self, service):
        # type: (Any) -> Literal[True]
        return True

    def __init__(self, module, fqdn, **kwargs):
        # type: (CIStaticGuest, str, **Any) -> None
        super(StaticLocalhostGuest, self).__init__(module, fqdn, **kwargs)

        # populate guest architecture from the OS`
        arch_output = self.execute('arch')
        assert arch_output.stdout
        self.environment = TestingEnvironment(arch=arch_output.stdout.rstrip(), compose=None)

        self.hostname = fqdn

    def execute(self, cmd, **kwargs):
        # type: (str, **Any) -> gluetool.utils.ProcessOutput
        """
        Execute a command on the guest. Should behave like `utils.run_command`.
        """

        return Command([cmd], logger=self.logger).run(**kwargs)

    def setup(self, variables=None, **kwargs):
        # type: (Optional[Dict[str, Any]], **Any) -> Any

        # pylint: disable=arguments-differ
        if not self._module.has_shared('setup_guest'):
            raise gluetool.GlueError("Module 'guest-setup' is required to actually set the guests up.")

        return self._module.shared('setup_guest', self, variables=variables, **kwargs)

    def __repr__(self):
        # type: () -> str

        return self.hostname


class CIStaticGuest(gluetool.Module):
    """
    Provides connection to static guests specified on the command line. The provisioner capabilities are auto-detected
    from the connected machines.
    """
    name = 'static-guest'

    options = [
        ('General Options', {
            'guest': {
                'help': """
                    Guest connection details, in form '[user@]hostname[:port]. Use 'localhost' or '127.0.0.1' to connect
                    to localhost instead. Default user for localhost is the same as the one running the pipeline.
                    In other cases the default user is 'root' and port 22.
                """,
                'action': 'append'
            },
            'guest-setup': {
                'help': 'Run guest setup after adding the guest. Useful for testing guest-setup related modules.',
                'action': 'store_true'
            },
            'ssh-key': {
                'help': 'SSH key to use to connect to the guests. Does not apply for localhost.'
            }
        }),
        ('Timeouts', {
            'boot-timeout': {
                'help': 'Wait SECONDS for a guest to finish its booting process (default: %(default)s)',
                'type': int,
                'default': DEFAULT_BOOT_TIMEOUT,
                'metavar': 'SECONDS'
            },
            'connect-timeout': {
                'help': 'Wait SECOND for a guest to become reachable over network (default: %(default)s)',
                'type': int,
                'default': DEFAULT_CONNECT_TIMEOUT,
                'metavar': 'SECONDS'
            },
            'echo-timeout': {
                'help': 'Wait SECOND for a guest shell to become available (default: %(default)s)',
                'type': int,
                'default': DEFAULT_ECHO_TIMEOUT,
                'metavar': 'SECONDS'
            },
        })
    ]

    shared_functions = ['provision', 'provisioner_capabilities']
    required_options = ('guest',)

    def __init__(self, *args, **kwargs):
        # type: (*Any, **Any) -> None
        super(CIStaticGuest, self).__init__(*args, **kwargs)

        # All guests connected
        self._guests = []  # type: List[StaticGuest]

    def sanity(self):
        # type: () -> None
        if not self.option('guest'):
            return

        for guest in self.option('guest'):
            if guest not in LOCALHOST_GUEST_HOSTNAMES and not self.option('ssh-key'):
                raise IncompatibleOptionsError("Option 'ssh-key' is required")

    def guest_remote(self, guest):
        # type: (str) -> StaticGuest
        """
        Connect to a guest and return a StaticGuest instance.

        :returns: A connected guest
        """

        match = re.match(r'^(?:([^@]+)@)?([^:@ ]+)(?::([0-9]+))?$', guest)
        if not match:
            raise GlueError("'{}' is not a valid hostname".format(guest))

        port = None  # type: Optional[str]
        (user, hostname, port) = match.groups()

        user = user or DEFAULT_SSH_USER
        port = port or None  # default is 22 from NetworkedGuest

        self.info("adding guest '{}' and checking for its connection".format(guest))
        static_guest = StaticGuest(
            self, hostname,
            name=hostname, username=user, port=port, key=self.option('ssh-key'),
            options=DEFAULT_SSH_OPTIONS)

        return static_guest

    def guest_localhost(self, guest):
        # type: (str) -> StaticLocalhostGuest
        """
        Connect to a localhost guest and return a StaticLocalhostGuest instance.

        :returns: A connected guest
        """

        self.info("adding guest '{}'".format(guest))
        localhost_guest = StaticLocalhostGuest(self, guest)

        return localhost_guest

    def provisioner_capabilities(self):
        # type: () -> ProvisionerCapabilities
        """
        Return description of Static Guest provisioner capabilities.

        Follows :doc:`Provisioner Capabilities Protocol </protocols/provisioner-capabilities>`.
        """

        return ProvisionerCapabilities(
            available_arches=[
                # note that arch returns with newline, we need to strip it
                guest.environment.arch for guest in self._guests if guest.environment is not None
            ]
        )

    def provision(self, environment, count=1, **kwargs):
        # type: (TestingEnvironment, int, **Any) -> List[StaticGuest]
        """
        Returns a list of N static guests, where N is specified by the parameter ``count``.

        :param tuple environment: Description of the environment caller wants to provision.
        :param int count: Number of guests the client module is asking for.
        :rtype: list(StaticGuest)
        :returns: A list of connected guests.
        """

        # Return requested number of guests. If the do not exist, blow up
        # NOTE: distro is currently ignored
        returned_guests = [
            guest for guest in self._guests if guest.environment and guest.environment.arch == environment.arch
        ][0:count]

        if len(returned_guests) != count:
            raise GlueError("Did not find {} guest(s) with architecture '{}'.".format(count, environment.arch))

        return returned_guests

    def guest_connect(self, guest):
        # type: (str) -> Union[StaticGuest, StaticLocalhostGuest]
        if guest in LOCALHOST_GUEST_HOSTNAMES:
            return self.guest_localhost(guest)

        return self.guest_remote(guest)

    def execute(self):
        # type: () -> None
        with ThreadPoolExecutor(thread_name_prefix="connect-thread") as executor:
            futures = {executor.submit(self.guest_connect, guest) for guest in self.option('guest')}

            wait_result = cast(WaitType, wait(futures))

            for future in wait_result.done:
                guest = future.result()
                self.info("added guest '{}' with architecture '{}'".format(guest, guest.environment.arch))

                if self.option('guest-setup'):
                    guest.setup()

                self._guests.append(guest)
