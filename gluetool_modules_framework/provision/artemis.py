# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import collections
import re
import six
import sys
import os
import time

import attrs
import cattrs
import gluetool
import gluetool.utils
import gluetool_modules_framework.libs
import requests
import urllib3.exceptions
from simplejson import JSONDecodeError
from contextlib import nullcontext

from gluetool import GlueError, SoftGlueError
from gluetool.log import log_blob, log_dict, LoggerMixin
from gluetool.result import Result
from gluetool.utils import (
    dump_yaml,
    treat_url,
    normalize_multistring_option,
    wait,
    normalize_bool_option,
    normalize_path,
    render_template,
    from_yaml,
    load_yaml
)
from gluetool_modules_framework.libs.threading import RepeatTimer
from gluetool_modules_framework.libs.guest import NetworkedGuest
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from typing import Any, Dict, List, Optional, Set, Tuple, cast  # noqa

# As defined in artemis-cli, https://gitlab.com/testing-farm/artemis/-/blob/main/cli/src/tft/artemis_cli/artemis_cli.py
API_FEATURE_VERSIONS: Dict[str, str] = {
    'supported-baseline': '0.0.16',
    'arch-under-hw': '0.0.19',
    'hw-constraints': '0.0.19',
    'skip-prepare-verify-ssh': '0.0.24',
    'log-types': '0.0.26',
    'hw-constraints-disk-as-list': '0.0.27',
    'hw-constraints-network': '0.0.28',
    'hw-constraints-boot-method': '0.0.32',
    'hw-constraints-hostname': '0.0.38',
    'hw-constraints-cpu-extra': '0.0.46',
    'hw-constraints-cpu-processors': '0.0.47',
    'hw-constraints-compatible-distro': '0.0.48',
    'hw-constraints-kickstart': '0.0.53',
    'fixed-hw-validation': '0.0.55',
    'user-defined-watchdog-delay': '0.0.56',
    'fixed-hw-virtualization-hypervisor': '0.0.58',
    'hw-constraints-cpu-flag': '0.0.67',
    'hw-constraints-zcrypt': '0.0.69',
    'hw-constraints-disk-model-name': '0.0.69',
    'guest-log-blobs': '0.0.70',
    'security-group-rules': '0.0.72',
    'guest-reboot': '0.0.74',
}

SUPPORTED_API_VERSIONS: Set[str] = set(API_FEATURE_VERSIONS.values())

EVENT_LOG_SUFFIX = '-artemis-guest-log.yaml'

DEFAULT_PRIORIY_GROUP = 'default-priority'
DEFAULT_READY_TIMEOUT = 300
DEFAULT_READY_TICK = 3
DEFAULT_READY_TIMEOUT_FROM_PIPELINE = 'store_true'
DEFAULT_ACTIVATION_TIMEOUT = 240
DEFAULT_ACTIVATION_TICK = 5
DEFAULT_API_CALL_TIMEOUT = 60
DEFAULT_API_CALL_TICK = 1
DEFAULT_ECHO_TIMEOUT = 240
DEFAULT_ECHO_TICK = 10
DEFAULT_BOOT_TIMEOUT = 240
DEFAULT_BOOT_TICK = 10
DEFAULT_SSH_OPTIONS = ['UserKnownHostsFile=/dev/null', 'StrictHostKeyChecking=no', 'PreferredAuthentications=publickey']
DEFAULT_SNAPSHOT_READY_TIMEOUT = 600
DEFAULT_SNAPSHOT_READY_TICK = 10
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_GUEST_LOG_TICK = 60

#: Artemis provisioner capabilities.
#: Follows :doc:`Provisioner Capabilities Protocol </protocols/provisioner-capabilities>`.
ProvisionerCapabilities = collections.namedtuple('ProvisionerCapabilities', ['available_arches'])


@attrs.define(kw_only=True)
class ArtemisGuestLog:
    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    type: str = attrs.field(validator=attrs.validators.instance_of(str))
    filename: str = attrs.field(validator=attrs.validators.instance_of(str))
    datetime_filename: str = attrs.field(validator=attrs.validators.instance_of(str))
    save_empty: bool = attrs.field(default=True, validator=attrs.validators.instance_of(bool))
    content: Optional[str] = None

    @filename.validator
    @datetime_filename.validator
    def validator_contains_guestname(self, attribute: 'ArtemisGuestLog', value: str) -> None:
        if '{guestname}' not in value:
            raise GlueError("Field '{}' is missing '{{guestname}}' string.".format(attribute.name))

    @datetime_filename.validator
    def validator_contains_datetime(self, attribute: 'ArtemisGuestLog', value: str) -> None:
        if '{datetime}' not in value:
            raise GlueError("Field '{}' is missing '{{datetime}}' string".format(attribute.name))


@attrs.define(kw_only=True)
class SecurityGroupRule:
    type: str = attrs.field(validator=attrs.validators.instance_of(str))
    port_min: int = attrs.field(validator=attrs.validators.instance_of(int))
    port_max: int = attrs.field(validator=attrs.validators.instance_of(int))
    protocol: str = attrs.field(validator=attrs.validators.instance_of(str))
    cidr: str = attrs.field(validator=attrs.validators.instance_of(str))


ArtemisGuestLogs = List[ArtemisGuestLog]
SecurityGroupRules = List[SecurityGroupRule]


class ArtemisResourceError(GlueError):
    def __init__(self, error: Optional[str] = None) -> None:
        error = error or "Artemis resource ended in 'error' state"
        super(ArtemisResourceError, self).__init__(error)


class PipelineCancelled(GlueError):
    def __init__(self) -> None:
        super(PipelineCancelled, self).__init__('Pipeline was cancelled, aborting')


class ArtemisAPIError(SoftGlueError):
    def __init__(self, response: Any, error: Optional[str] = None) -> None:

        self.status_code = response.status_code
        self.json: Dict[str, str] = {}
        self.text = six.ensure_str(response.text.encode('ascii', 'replace'))
        self._errors = error

        # We will look at response's headers to try to guess if response's content is json serializable
        # If yes, we will expect it to either have 'message' or 'errors' key, it's value could be used in exception
        # If no, we will use raw text in exception instead
        headers = {key.lower(): response.headers[key] for key in response.headers}

        if headers.get('content-type') and 'application/json' in headers['content-type']:
            try:
                self.json = response.json()
            except JSONDecodeError as exc:
                self.json['errors'] = str(exc)

        super(ArtemisAPIError, self).__init__(
            'Call to Artemis API failed, HTTP {}: {}'.format(
                self.status_code, self.errors))

    @property
    def errors(self) -> str:

        if self._errors:
            return self._errors

        if self.json.get('message'):
            return self.json['message']

        if self.json.get('errors'):
            return self.json['errors']

        return self.text


class ArtemisAPI(object):
    ''' Class that allows RESTful communication with Artemis API '''

    def __init__(self, module: 'ArtemisProvisioner', api_url: str, api_version: str, timeout: int, tick: int) -> None:

        self.module = module
        self.url = treat_url(api_url)
        self.version = api_version
        self.timeout = timeout
        self.tick = tick
        self.check_if_artemis()

    def api_call(self,
                 endpoint: str,
                 method: str = 'GET',
                 expected_status_codes: Optional[List[int]] = None,
                 data: Optional[Dict[str, Any]] = None) -> requests.Response:

        # default expected status code is 200
        expected_status_codes = expected_status_codes or [200]

        def _api_call() -> Result[Optional[requests.Response], str]:

            _request = getattr(requests, method.lower(), None)
            if _request is None:
                return Result.Error('Unknown HTTP method {}'.format(method))

            try:
                response = _request('{}v{}/{}'.format(self.url, self.version, endpoint), json=data)

            # Catch all urllib3 and requests exceptions
            # https://urllib3.readthedocs.io/en/latest/reference/urllib3.exceptions.html#urllib3.exceptions.HTTPError
            # https://requests.readthedocs.io/en/latest/api/#exceptions
            # TFT-1755 - Added to workaround DNS problems with podman
            # TFT-2656 - Fix retrying of all possible HTTP errors when talking to Artemis
            except (urllib3.exceptions.HTTPError, requests.exceptions.RequestException) as error:
                fqcn = '{}.{}'.format(error.__module__, error.__class__.__qualname__)
                self.module.debug('Retrying Artemis API call due to {} exception'.format(fqcn), sentry=True)
                return Result.Error('{}: {}'.format(fqcn, str(error)))

            finally:
                if self.module.pipeline_cancelled and not self.module.destroying:
                    return Result.Ok(None)

            assert expected_status_codes is not None
            if response.status_code in expected_status_codes:
                return Result.Ok(response)

            return Result.Error('Artemis API error: {}'.format(ArtemisAPIError(response)))

        try:
            response = wait('api_call', _api_call, timeout=self.timeout, tick=self.tick)

        except GlueError as exc:
            raise GlueError('Artemis API call failed: {}'.format(exc))

        if response is None:
            raise PipelineCancelled()

        return response

    def check_if_artemis(self) -> None:
        '''
        Checks if `url` actually points to ArtemisAPI by calling '/guests' endpoint (which should always return a list)
        '''

        def error(response: Any) -> ArtemisAPIError:
            err_msg = 'URL {} does not point to Artemis API. Expected list, got {}' \
                .format(self.url, six.ensure_str(response.text.encode('ascii', 'replace')))
            err = ArtemisAPIError(response, error=err_msg)
            return err

        response = self.api_call('guests/')

        if not isinstance(response.json(), list):
            raise error(response)

    def create_guest(self,
                     environment: TestingEnvironment,
                     pool: Optional[str] = None,
                     keyname: Optional[str] = None,
                     priority: Optional[str] = None,
                     user_data: Optional[Dict[str, Any]] = None,
                     post_install_script: Optional[str] = None,
                     watchdog_dispatch_delay: Optional[int] = None,
                     watchdog_period_delay: Optional[int] = None,
                     security_group_rules_ingress: Optional[SecurityGroupRules] = None,
                     security_group_rules_egress: Optional[SecurityGroupRules] = None,
                     ) -> Any:
        '''
        Submits a guest request to Artemis API.

        :param tuple environment: description of the environment caller wants to provision.
            Follows :doc:`Testing Environment Protocol </protocols/testing-environment>`.

        :param str pool: name of the pool

        :param str keyname: name of key stored in Artemis configuration.

        :param str priority: Priority group of the guest request.
            See Artemis API docs for more.

        :param int watchdog_dispatch_delay: How long (seconds) before the guest "is-alive" watchdog is dispatched.

        :param int watchdog_period_delay: How often (seconds) check that the guest "is-alive".

        :rtype: dict
        :returns: Artemis API response serialized as dictionary or ``None`` in case of failure.
        '''

        compose = environment.compose
        snapshots = environment.snapshots
        pool = pool or environment.pool
        kickstart = environment.kickstart

        post_install_script_contents = None
        if post_install_script:
            post_install_script_contents = self.module.expand_post_install_script(post_install_script)

        # TODO: yes, semver will make this much better... Or better, artemis-cli package provide an easy-to-use
        # bit of code to construct the payload.
        if self.version >= API_FEATURE_VERSIONS['arch-under-hw']:
            data: Dict[str, Any] = {
                'keyname': keyname,
                'environment': {
                    'hw': {
                        'arch': environment.arch
                    },
                    'os': {
                        'compose': compose
                    },
                    'snapshots': snapshots
                },
                'priority_group': priority,
                'post_install_script': post_install_script_contents,
                'user_data': user_data
            }

            if pool:
                data['environment']['pool'] = pool

            hardware = self.module.hw_constraints or environment.hardware

            if hardware:
                data['environment']['hw']['constraints'] = hardware

            if self.version >= API_FEATURE_VERSIONS['skip-prepare-verify-ssh']:
                data['skip_prepare_verify_ssh'] = normalize_bool_option(self.module.option('skip-prepare-verify-ssh'))

        elif self.version >= API_FEATURE_VERSIONS['supported-baseline']:
            data = {
                'keyname': keyname,
                'environment': {
                    'arch': environment.arch,
                    'os': {},
                    'snapshots': snapshots
                },
                'priority_group': priority,
                'post_install_script': post_install_script_contents
            }

            if pool:
                data['environment']['pool'] = pool

            data['environment']['os']['compose'] = compose

            data['user_data'] = user_data

        else:
            # Note that this should never happen, because we check the requested version in sanity()
            raise GlueError('unsupported API version {}'.format(self.version))

        if self.version >= API_FEATURE_VERSIONS['security-group-rules']:
            data['security_group_rules_ingress'] = security_group_rules_ingress
            data['security_group_rules_egress'] = security_group_rules_egress

        if self.version >= API_FEATURE_VERSIONS['hw-constraints-kickstart']:
            data['environment']['kickstart'] = kickstart or self.module.kickstart or {}

        if self.version >= API_FEATURE_VERSIONS['user-defined-watchdog-delay']:
            if watchdog_dispatch_delay is not None:
                data['watchdog_dispatch_delay'] = watchdog_dispatch_delay
            if watchdog_period_delay is not None:
                data['watchdog_period_delay'] = watchdog_period_delay
        elif watchdog_dispatch_delay is not None or watchdog_period_delay is not None:
            raise GlueError('User defined watchdog is unsupported in current API version {}'.format(self.version))

        log_dict(self.module.debug, 'guest data', data)

        # TFT-3851 - if pipeline was cancelled, do not start a new guest provisioning
        if self.module.pipeline_cancelled:
            raise PipelineCancelled()

        response = self.api_call('guests/', method='POST', expected_status_codes=[201, 400], data=data)

        if response.status_code == 400:
            raise GlueError('Guest creation failed, HTTP {}: {}'.format(
                response.status_code,
                response.json()
            ))

        return response.json()

    def inspect_guest(self, guest_id: str) -> Any:
        '''
        Requests Artemis API for data about a specific guest.

        :param str guest_id: Artemis guestname (or guest id).
            See Artemis API docs for more.

        :rtype: dict
        :returns: Artemis API response serialized as dictionary or ``None`` in case of failure.
        '''

        return self.api_call('guests/{}'.format(guest_id)).json()

    def inspect_guest_events(self, guest_id: str) -> Any:
        '''
        Requests Artemis API for data about a specific guest's events.

        :param str guest_id: Artemis guestname (or guest id).
            See Artemis API docs for more.

        :rtype: list
        :returns: Artemis API response serialized as list or ``None`` in case of failure.
        '''

        return self.api_call('guests/{}/events'.format(guest_id)).json()

    def get_guest_events(self, guest: 'ArtemisGuest') -> List[Any]:
        '''
        Fetch all guest's events from Artemis API.

        :param str guest: Artemis guest

        :rtype: list
        :returns: Artemis API response as JSON or Result in case of failure.
        '''
        max_page = 10000
        page_size = 25
        events: List[Any] = []
        for page in range(1, max_page):
            uri = 'guests/{}/events?page_size={}&page={}'.format(guest.artemis_id, page_size, page)
            response = self.api_call(uri).json()
            events = events + response
            if len(response) < page_size:
                break
        else:
            guest.error('Max ({}) pages reached. Artemis guest event log too long.'.format(max_page))

        return events

    def dump_events(self, guest: 'ArtemisGuest') -> None:
        events = self.get_guest_events(guest)

        tmpname = '{}.tmp'.format(guest.event_log_path)

        dump_yaml(events, tmpname)

        filesize = os.path.getsize(guest.event_log_path) if os.path.exists(guest.event_log_path) else 0
        tmpsize = os.path.getsize(tmpname) if os.path.exists(tmpname) else 0

        if tmpsize > filesize:
            os.rename(tmpname, guest.event_log_path)
        else:
            os.remove(tmpname)

    def cancel_guest(self, guest_id: str) -> Any:
        '''
        Requests Artemis API to cancel guest provision (or, in case a guest os already provisioned, return the guest).

        :param str guest_id: Artemis guestname (or guest id).
            See Artemis API docs for more.

        :rtype: Response
        :returns: Artemis API response or ``None`` in case of failure.
        '''

        return self.api_call('guests/{}'.format(guest_id), method='DELETE', expected_status_codes=[204, 404])

    def create_snapshot(self, guest_id: str, start_again: bool = True) -> Any:
        '''
        Requests Aremis API to create a snapshot of a guest.

        :param str guest_id: Artemis guestname (or guest_id).
            See Artemis API docs for more.

        :param bool start_again: If true artemis will start a guest after snapshot creating

        :rtype: dict
        :returns: Artemis API response serialized as dictionary or ``None`` in case of failure.
        '''

        data = {'start_again': start_again}

        return self.api_call('guests/{}/snapshots'.format(guest_id),
                             method='POST',
                             data=data,
                             expected_status_codes=[201]
                             ).json()

    def inspect_snapshot(self, guest_id: str, snapshot_id: str) -> Any:
        '''
        Requests Artemis API for data about a specific snapshot.

        :param str guest_id: Artemis guestname (or guest id).
        :param str snaphsot_id: Artemis snapshotname (or snapshot id).
            See Artemis API docs for more.

        :rtype: dict
        :returns: Artemis API response serialized as dictionary or ``None`` in case of failure.
        '''

        return self.api_call('guests/{}/snapshots/{}'.format(guest_id, snapshot_id)).json()

    def restore_snapshot(self, guest_id: str, snapshot_id: str) -> Any:
        '''
        Requests Artemis API to restore a guest to a snapshot.

        :param str guest_id: Artemis guestname (or guest id).
        :param str snaphsot_id: Artemis snapshotname (or snapshot id).
            See Artemis API docs for more.

        :rtype: dict
        :returns: Artemis API response serialized as dictionary or ``None`` in case of failure.
        '''

        return self.api_call('guests/{}/snapshots/{}/restore'.format(guest_id, snapshot_id),
                             method='POST',
                             expected_status_codes=[201]
                             ).json()

    def cancel_snapshot(self, guest_id: str, snapshot_id: str) -> Any:
        '''
        Requests Artemis API to cancel snapshot creating
        (or, in case a snapshot is already provisioned, delete the snapshot).

        :param str guest_id: Artemis guestname (or guest id).
        :param str snaphsot_id: Artemis snapshotname (or snapshot id).
            See Artemis API docs for more.

        :rtype: Response
        :returns: Artemis API response or ``None`` in case of failure.
        '''

        return self.api_call('guests/{}/snapshots/{}'.format(guest_id, snapshot_id),
                             method='DELETE',
                             expected_status_codes=[204, 404])


class ArtemisSnapshot(LoggerMixin):
    def __init__(self,
                 module: 'ArtemisProvisioner',
                 name: str,
                 guest: 'ArtemisGuest'
                 ) -> None:
        super(ArtemisSnapshot, self).__init__(module.logger)

        self._module = module
        self.name = name
        self.guest = guest

    def __repr__(self) -> str:
        return '<ArtemisSnapshot(name="{}")>'.format(self.name)

    def wait_snapshot_ready(self, timeout: int, tick: int) -> None:

        try:
            wait('snapshot_ready', self._check_snapshot_ready, timeout=timeout, tick=tick)

        except GlueError as exc:
            raise GlueError("Snapshot couldn't be ready: {}".format(exc))

    def _check_snapshot_ready(self) -> Result[bool, str]:

        snapshot_state = None

        assert self._module.api

        try:
            snapshot_data = self._module.api.inspect_snapshot(self.guest.artemis_id, self.name)

            snapshot_state = snapshot_data['state']

            if snapshot_state == 'ready':
                return Result.Ok(True)

            if snapshot_state == 'error':
                raise ArtemisResourceError()

        except ArtemisResourceError:
            six.reraise(*sys.exc_info())

        except PipelineCancelled:
            six.reraise(*sys.exc_info())

        except GlueError as e:
            self.warn('Exception raised: {}'.format(e))

        return Result.Error("Couldn't get snapshot {}".format(self.name))

    def release(self) -> None:
        assert self._module.api
        self._module.api.cancel_snapshot(self.guest.artemis_id, self.name)


class ArtemisGuest(NetworkedGuest):

    def __init__(self,
                 module: 'ArtemisProvisioner',
                 guestname: str,
                 hostname: Optional[str],
                 environment: TestingEnvironment,
                 port: Optional[int] = None,
                 username: Optional[str] = None,
                 key: Optional[str] = None,
                 options: Optional[List[str]] = None,
                 workdir: Optional[str] = None,
                 guest_logs: Optional[ArtemisGuestLogs] = None,
                 **kwargs: Optional[Dict[str, Any]]
                 ):

        super(ArtemisGuest, self).__init__(module,
                                           hostname,
                                           environment=environment,
                                           name=guestname,
                                           port=port,
                                           username=username,
                                           key=key,
                                           options=options)
        assert module.api

        self.artemis_id = guestname
        self._snapshots: List[ArtemisSnapshot] = []
        self.module: ArtemisProvisioner = module
        self.api: ArtemisAPI = module.api
        self.workdir = workdir or ''
        self.event_log_path = os.path.join(self.workdir, '{}{}'.format(guestname, EVENT_LOG_SUFFIX))
        self.guest_logs_timer: Optional[RepeatTimer] = None
        self.guest_logs: Optional[ArtemisGuestLogs] = guest_logs or None

    def __str__(self) -> str:
        return 'ArtemisGuest({}, {}@{}, {})'.format(self.artemis_id, self.username, self.hostname, self.environment)

    @property
    def event_log_error(self) -> Optional[str]:
        if not os.path.exists(self.event_log_path):
            self.warn('Skipping event log analysis, no event log found for the guest')
            return None

        return self.module.event_log_error(load_yaml(self.event_log_path))

    def _check_ip_ready(self) -> Result[bool, str]:

        try:
            guest_data = self.api.inspect_guest(self.artemis_id)
            guest_state = guest_data['state']
            guest_address = guest_data['address']

            if guest_state == 'ready':
                if guest_address:
                    return Result.Ok(True)

            if guest_state == 'error':
                raise ArtemisResourceError(error=self.event_log_error)

        except ArtemisResourceError:
            six.reraise(*sys.exc_info())

        except PipelineCancelled:
            six.reraise(*sys.exc_info())

        except GlueError as e:
            self.warn('Exception raised: {}'.format(e))

        return Result.Error("Couldn't get address for guest {}".format(self.artemis_id))

    def _wait_ready(self, timeout: int, tick: int) -> None:
        '''
        Wait till the guest is ready to be provisioned, which it's IP/hostname is available
        '''

        try:
            self.wait('ip_ready', self._check_ip_ready, timeout=timeout, tick=tick)

        except GlueError as exc:
            raise GlueError("Guest couldn't be provisioned: {}".format(exc))

    def _wait_alive(
        self,
        connect_socket_timeout: Optional[int] = None,
        connect_timeout: Optional[int] = None,
        connect_tick: Optional[int] = None,
        echo_timeout: Optional[int] = None,
        echo_tick: Optional[int] = None,
        boot_timeout: Optional[int] = None,
        boot_tick: Optional[int] = None
    ) -> None:
        '''
        Wait till the guest is alive. That covers several checks.
        '''

        try:
            self.wait_alive(connect_socket_timeout=connect_socket_timeout or self.module.option('connection-timeout'),
                            connect_timeout=connect_timeout or self.module.option('activation-timeout'),
                            connect_tick=connect_tick or self.module.option('activation-tick'),
                            echo_timeout=echo_timeout or self.module.option('echo-timeout'),
                            echo_tick=echo_tick or self.module.option('echo-tick'),
                            boot_timeout=boot_timeout or self.module.option('boot-timeout'),
                            boot_tick=boot_tick or self.module.option('boot-tick'))

        except GlueError as exc:
            raise GlueError('Guest failed to become alive: {}'.format(exc))

    @property
    def supports_snapshots(self) -> bool:
        assert self.environment
        return self.environment.snapshots

    def setup(self, variables: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        """
        Custom setup for Artemis guests. Add a hostname in case there is none.

        :param dict variables: dictionary with GUEST_HOSTNAME and/or GUEST_DOMAINNAME keys
        """
        variables = variables or {}

        # Our playbooks require hostname and domainname.
        # If not set, create them - some tests may depend on resolvable hostname.
        if 'GUEST_HOSTNAME' not in variables:
            assert self.hostname
            variables['GUEST_HOSTNAME'] = re.sub(r'10\.(\d+)\.(\d+)\.(\d+)', r'host-\1-\2-\3', self.hostname)

        if 'GUEST_DOMAINNAME' not in variables:
            variables['GUEST_DOMAINNAME'] = 'host.example.com'

        if 'IMAGE_NAME' not in variables:
            assert self.environment
            variables['IMAGE_NAME'] = self.environment.compose

        return super(ArtemisGuest, self).setup(variables=variables, **kwargs)

    def create_snapshot(self, start_again: bool = True) -> ArtemisSnapshot:
        """
        Creates a snapshot from the current running image of the guest.

        All created snapshots are deleted automatically during destruction.

        :rtype: ArtemisSnapshot
        :returns: newly created snapshot.
        """
        response = self.api.create_snapshot(self.artemis_id, start_again)

        snapshot = ArtemisSnapshot(cast(ArtemisProvisioner, self._module), response.get('snapshotname'), self)

        snapshot.wait_snapshot_ready(self._module.option('snapshot-ready-timeout'),
                                     self._module.option('snapshot-ready-tick'))

        # The snapshot is ready, but the guest hasn't started yet
        self._wait_ready(self._module.option('ready-timeout'),
                         self._module.option('ready-tick'))

        self._snapshots.append(snapshot)

        self.info("image snapshot '{}' created".format(snapshot.name))

        return snapshot

    def restore_snapshot(self, snapshot: ArtemisSnapshot) -> 'ArtemisGuest':
        """
        Rebuilds server with the given snapshot.

        :param snapshot: :py:class:`ArtemisSnapshot` instance.
        :rtype: ArtemisGuest
        :returns: server instance rebuilt from given snapshot.
        """

        self.info("rebuilding server with snapshot '{}'".format(snapshot.name))

        self.api.restore_snapshot(self.artemis_id, snapshot.name)
        snapshot.wait_snapshot_ready(self._module.option('snapshot-ready-timeout'),
                                     self._module.option('snapshot-ready-tick'))

        self.info("image snapshot '{}' restored".format(snapshot.name))

        return self

    def _release_snapshots(self) -> None:
        for snapshot in self._snapshots:
            snapshot.release()

        if self._snapshots:
            self.info('Successfully released all {} snapshots'.format(len(self._snapshots)))

        self._snapshots = []

    def _release_instance(self) -> None:
        self.api.cancel_guest(self.artemis_id)

    def destroy(self) -> None:
        '''
        Destroy the guest.
        '''
        # TFT-3841 - make sure guest destroy is not interrupted
        with self._module.shared('pipeline_cancellation_lock') or nullcontext():
            self.stop_guest_logging()

            if self._module.option('keep'):
                self.api.dump_events(self)
                self.warn("keeping guest provisioned as requested")
                return

            self.info('destroying guest')

            self._release_snapshots()
            self._release_instance()
            cast(ArtemisProvisioner, self._module).remove_from_list(self)

            self.info('successfully released')

            # Dump events after destroy
            self.api.dump_events(self)

    def _save_guest_log(self, filename: str, data: str) -> None:
        filepath = os.path.join(self.workdir, filename)
        temporary_filepath = os.path.join(self.workdir, f'{filename}.tmp')

        # Save first into a temporary file - if we fail to save the content,
        # we won't break the existing file, if there's any.
        with open(temporary_filepath, 'w') as f:
            f.write(data)

        # Now *atomically* rename the temporary file - if we succeed, everything
        # is fine; if we fail, the original file remains unharmed.
        os.rename(temporary_filepath, filepath)

    def gather_guest_log(self, log: ArtemisGuestLog) -> None:
        """
        Gather a single guest log and save it to the log file.
        """
        # get guest log
        response = self.api.api_call(
            'guests/{}/logs/{}'.format(self.artemis_id, log.type),
            expected_status_codes=[200, 404, 409]
        )

        # ask for fresh log if needed
        if response.status_code in [404, 409]:
            # ask for fresh logs
            self.api.api_call(
                'guests/{}/logs/{}'.format(self.artemis_id, log.type),
                method='POST',
                expected_status_codes=[202, 409]
            )
            return

        if self.module.api and self.module.api.version >= API_FEATURE_VERSIONS['guest-log-blobs']:
            blob_infos: List[Dict[str, str]] = response.json().get('blobs', [])

            if not blob_infos:
                # Do not save empty log of requested
                if not log.save_empty:
                    return

                content = '<no {} available>'.format(log.name)

            else:
                content_components: List[str] = []

                for blob_info in blob_infos:
                    content_components += [
                        f'# -- Acquired at {blob_info["ctime"]} --',
                        '',
                        blob_info['content'],
                        ''
                    ]

                content = '\n'.join(content_components)

        else:
            blob: Optional[str] = response.json().get('blob')

            if not blob:
                # Do not save empty log of requested
                if not log.save_empty:
                    return

                content = '<no {} available>'.format(log.name)

            else:
                content = blob

        # nothing todo in case there is no change in the guest log
        if content == log.content:
            return

        # save main guest log
        log_blob(self.debug, 'saving latest {}'.format(log.name), content)
        log.content = content
        guest_log_file = log.filename.format(guestname=self.artemis_id)
        self._save_guest_log(guest_log_file, content)

        if normalize_bool_option(self.module.option('guest-logs-without-history')):
            return

        updated = response.json().get('updated')
        if not updated:
            return

        # save guest log with datetime, in case a real log was retrieved
        log_datetime = updated.replace(' ', '-')
        self._save_guest_log(
            log.datetime_filename.format(
                guestname=self.artemis_id,
                datetime=log_datetime
            ),
            content
        )

    def gather_guest_logs(self) -> None:
        """
        Gather all configured guest logs.
        """
        assert self.guest_logs
        for log in self.guest_logs:
            self.gather_guest_log(log)

    def stop_guest_logging(self) -> None:
        """
        Stop gathering of logs for the guest.
        """
        if self.guest_logs_timer is None or not self.guest_logs:
            return

        self.debug('Stopping guest logging')

        self.guest_logs_timer.cancel()
        self.guest_logs_timer = None

        self.gather_guest_logs()

    def start_guest_logging(self) -> None:
        '''
        Start gathering of configured logs for the guest.
        '''
        if self.guest_logs_timer:
            return

        if not self.guest_logs:
            return

        self.debug('Starting guest logging')

        self.gather_guest_logs()

        self.guest_logs_timer = RepeatTimer(
            self.module.option('guest-log-tick'),
            self.gather_guest_logs
        )

        self.guest_logs_timer.start()


class ArtemisProvisioner(gluetool.Module):
    ''' Provisions guest via Artemis API '''
    name = 'artemis'
    description = 'Provisions guest via Artemis API'
    options = [
        ('API options', {
            'api-url': {
                'help': '''
                        Artemis API url. Accepts also Jinja templates which will be rendered using
                        `eval_context` shared method.
                        ''',
                'metavar': 'URL',
                'type': str
            },
            'api-version': {
                'help': 'Artemis API version',
                'metavar': 'URL',
                'type': str
            },
            'key': {
                'help': 'Desired guest key name',
                'metavar': 'KEYNAME',
                'type': str
            },
            'arch': {
                'help': 'Desired guest architecture',
                'metavar': 'ARCH',
                'type': str
            },
            'priority-group': {
                'help': 'Desired guest priority group (default: %(default)s)',
                'metavar': 'PRIORITY_GROUP',
                'type': str,
                'default': DEFAULT_PRIORIY_GROUP
            },
            'user-data-vars-template-file': {
                'help': 'YAML containing mapping templates to be stored in the user-data field (default: none)',
                'type': str,
                'default': None
            }
        }),
        ('Common options', {
            'keep': {
                'help': '''Keep instance(s) running, do not destroy. No reservation records are created and it is
                           expected from the user to cleanup the instance(s).''',
                'action': 'store_true'
            },
            'provision': {
                'help': 'Provision given number of guests',
                'metavar': 'COUNT',
                'type': int
            },
            'wait': {
                'help': '''Wait given number of SECONDS before destroying the guests.
                           Useful for testing. Works only with the --provision option.''',
                'metavar': 'SECONDS',
                'type': int
            }
        }),
        ('Error options', {
            'error-events': {
                'help': """
                        List of last guest log events provided to the error template rendering.
                        Specified by the event name.
                        """,
                'metavar': 'EVENT1,EVENT2,...',
                'action': 'append',
                'default': []
            },
            'error-template-file': {
                'help': """
                        Jinja2 template used to generate Artemis error message.
                        The template has access to last Artemis guest log events specified by
                        the ``error-events`` option. The events are accessible under the ``event``
                        dict variable in the template.
                        """,
                'metavar': 'JINJA_TEMPLATE',
                'type': str
            },
            'analyze-event-log': {
                'help': """
                        Only analyze the given event log and print the error message.
                        Can be a remote HTTP URL.
                        Useful for testing.
                        """,
                'metavar': 'FILE_OR_URL',
                'type': str
            }
        }),
        ('Guest options', {
            'ssh-options': {
                'help': 'SSH options (default: none).',
                'action': 'append',
                'default': []
            },
            'ssh-key': {
                'help': 'SSH key that is used to connect to the machine',
                'type': str
            }
        }),
        ('Provisioning options', {
            'compose': {
                'help': 'Desired guest compose',
                'metavar': 'COMPOSE',
                'type': str
            },
            'hw-constraint': {
                'help': """
                        HW requirements, expresses as key/value pairs. Keys can consist of several properties,
                        e.g. ``disk.space='>= 40 GiB'``, such keys will be merged in the resulting environment
                        with other keys sharing the path: ``cpu.family=79`` and ``cpu.model=6`` would be merged,
                        not overwriting each other (default: none).
                        """,
                'metavar': 'KEY1.KEY2=VALUE',
                'type': str,
                'action': 'append',
                'default': []
            },
            'kickstart-pre-install': {
                'help': 'Pre installation part, corresponding to ``%%pre`` in ks file.',
                'type': str,
            },
            'kickstart-script': {
                'help': 'Main body of a kickstart file.',
                'type': str,
            },
            'kickstart-post-install': {
                'help': 'Post installation part, corresponding to ``%%post`` in ks file.',
                'type': str,
            },
            'kickstart-metadata': {
                'help': 'Specified metadata can change the interpretation of the ks file.',
                'type': str,
            },
            'kickstart-kernel-options': {
                'help': 'Options to be passed to the kernel command line when the installer is booted.',
                'type': str,
            },
            'kickstart-kernel-options-post': {
                'help': 'Options to be passed to the kernel command line after the installation.',
                'type': str,
            },
            'pool': {
                'help': 'Desired pool',
                'metavar': 'POOL',
                'type': str
            },
            'setup-provisioned': {
                'help': "Setup guests after provisioning them. See 'guest-setup' module",
                'action': 'store_true'
            },
            'skip-prepare-verify-ssh': {
                'help': 'Skip verifiction of SSH connection in prepare state',
                'action': 'store_true'
            },
            'snapshots': {
                'help': 'Choose a pool with snapshot support',
                'action': 'store_true'
            },
            'post-install-script': {
                'help': 'A post install script to run after vm becomes ready (default: %(default)s)',
                'metavar': 'POST_INSTALL_SCRIPT',
                'type': str,
                'default': ''
            },
            'guest-logs-enable': {
                'help': 'Enable gathering of logs from Artemis guests.',
                'action': 'store_true'
            },
            'guest-logs-config': {
                'help': 'Configuration of the Artemis guest logs gathering.',
                'type': str,
            },
            'guest-logs-without-history': {
                'help': 'If set, only one copy of guest log will be stored, no intermediate snapshots.',
                'action': 'store_true'
            },
        }),
        ('Timeout options', {
            'connect-timeout': {
                'help': 'Socket connection timeout for testing guest connection (default: %(default)s)',
                'metavar': 'CONNECT_TIMEOUT',
                'type': int,
                'default': DEFAULT_CONNECT_TIMEOUT
            },
            'ready-timeout': {
                'help': 'Timeout for guest to become ready (default: %(default)s)',
                'metavar': 'READY_TIMEOUT',
                'type': int,
                'default': DEFAULT_READY_TIMEOUT
            },
            'ready-timeout-from-pipeline': {
                'help': 'Override ready-timeout with the pipeline timeout.',
                'metavar': 'READY_TIMEOUT_FROM_PIPELINE',
                'type': bool,
                'default': DEFAULT_READY_TIMEOUT_FROM_PIPELINE
            },
            'ready-timeout-from-pipeline-offset': {
                'help': 'Subtract this amount from the pipeline timeout to wait for guest to become ready.',
                'metavar': 'READY_TIMEOUT_FROM_PIPELINE_OFFSET',
                'type': int,
            },
            'ready-tick': {
                'help': 'Check every READY_TICK seconds if a guest has become ready (default: %(default)s)',
                'metavar': 'READY_TICK',
                'type': int,
                'default': DEFAULT_READY_TICK
            },
            'activation-timeout': {
                'help': 'Timeout for guest to become active (default: %(default)s)',
                'metavar': 'ACTIVATION_TIMEOUT',
                'type': int,
                'default': DEFAULT_ACTIVATION_TIMEOUT
            },
            'activation-tick': {
                'help': 'Check every ACTIVATION_TICK seconds if a guest has become active (default: %(default)s)',
                'metavar': 'ACTIVATION_TICK',
                'type': int,
                'default': DEFAULT_ACTIVATION_TICK
            },
            'api-call-timeout': {
                'help': 'Timeout for Artemis API calls (default: %(default)s)',
                'metavar': 'API_CALL_TIMEOUT',
                'type': int,
                'default': DEFAULT_API_CALL_TIMEOUT
            },
            'api-call-tick': {
                'help': 'Check every API_CALL_TICK seconds for Artemis API response (default: %(default)s)',
                'metavar': 'API_CALL_TICK',
                'type': int,
                'default': DEFAULT_API_CALL_TICK
            },
            'echo-timeout': {
                'help': 'Timeout for guest echo (default: %(default)s)',
                'metavar': 'ECHO_TIMEOUT',
                'type': int,
                'default': DEFAULT_ECHO_TIMEOUT
            },
            'echo-tick': {
                'help': 'Echo guest every ECHO_TICK seconds (default: %(default)s)',
                'metavar': 'ECHO_TICK',
                'type': int,
                'default': DEFAULT_ECHO_TICK
            },
            'boot-timeout': {
                'help': 'Timeout for guest boot (default: %(default)s)',
                'metavar': 'BOOT_TIMEOUT',
                'type': int,
                'default': DEFAULT_BOOT_TIMEOUT
            },
            'boot-tick': {
                'help': 'Check every BOOT_TICK seconds if a guest has boot (default: %(default)s)',
                'metavar': 'BOOT_TICK',
                'type': int,
                'default': DEFAULT_BOOT_TICK
            },
            'guest-log-tick': {
                'help': 'Gather guest log every GUEST_LOG_TICK seconds (default: %(default)s)',
                'metavar': 'GUEST_LOG_TICK',
                'type': int,
                'default': DEFAULT_GUEST_LOG_TICK
            },
            'snapshot-ready-timeout': {
                'help': 'Timeout for snapshot to become ready (default: %(default)s)',
                'metavar': 'SNAPSHOT_READY_TIMEOUT',
                'type': int,
                'default': DEFAULT_SNAPSHOT_READY_TIMEOUT
            },
            'snapshot-ready-tick': {
                'help': 'Check every SNAPSHOT_READY_TICK seconds if a snapshot has become ready (default: %(default)s)',
                'metavar': 'SNAPSHOT_READY_TICK',
                'type': int,
                'default': DEFAULT_SNAPSHOT_READY_TICK
            },
            'watchdog-dispatch-delay': {
                'help': 'How long (seconds) before the guest\'s "is-alive" watchdog is dispatched',
                'metavar': 'WATCHDOG_DISPATCH_DELAY',
                'type': int
            },
            'watchdog-period-delay': {
                'help': 'How often (seconds) check that the guest "is-alive"',
                'metavar': 'WATCHDOG_PERIOD_DELAY',
                'type': int
            }
        })
    ]

    required_options = ('api-url', 'api-version', 'key', 'priority-group', 'ssh-key')

    shared_functions = ['provision', 'provisioner_capabilities', 'artemis_api_options']

    destroying = False  # Flag indicating that this gluetool module is being destroyed

    def artemis_api_options(self) -> Dict[str, Any]:
        return {
            'api-url': self.api_url,
            'api-version': self.api_version,
            'ssh-key': self.option('ssh-key'),
            'key': self.option('key'),
            'post-install-script': self.post_install_script,
            'skip-prepare-verify-ssh': self.option('skip-prepare-verify-ssh'),
            'ready-timeout': self.option('ready-timeout'),
            'ready-tick': self.option('ready-tick'),
            'api-call-timeout': self.option('api-call-timeout'),
            'user-data-vars-template-file': self.option('user-data-vars-template-file')
        }

    @property
    def api_url(self) -> str:
        return render_template(self.option('api-url') or '', **self.shared('eval_context'))

    @property
    def api_version(self) -> str:
        return render_template(self.option('api-version') or '', **self.shared('eval_context'))

    @property
    def error_events(self) -> List[str]:
        return normalize_multistring_option(self.option('error-events'))

    @property
    def error_template_file(self) -> Optional[str]:
        if not self.option('error-template-file'):
            return None

        return normalize_path(self.option('error-template-file'))

    @gluetool.utils.cached_property
    def guest_event_log(self) -> Any:
        """
        Load guest event log to analyze from a remote URL or a file.
        """
        analyzed_log = self.option('analyze-event-log')

        if not analyzed_log:
            return None

        if analyzed_log.startswith('http'):

            self.info("Downloading '{}'".format(analyzed_log))
            with gluetool.utils.requests() as req:
                response = req.get(analyzed_log)

            if response.status_code != 200:
                raise GlueError("Failed to download guest event log '{}'".format(analyzed_log))

            if 'eventname: created' not in response.text:
                raise GlueError("The url '{}' does not contain an Artemis guest event log".format(analyzed_log))

            return from_yaml(response.text)
        else:
            return load_yaml(normalize_path(analyzed_log))

    def expand_post_install_script(self, post_install_script: str) -> str:
        """
        Converts post_install_script, which can either be a filename or a script itself, into the script by reading the
        file.

        :param str post_install_script: script or filepath of the script.
        :rtype: str
        """
        if os.path.isfile(post_install_script):
            with open(normalize_path(post_install_script)) as f:
                return f.read()
        # NOTE(ivasilev) Remove possible string escaping
        return post_install_script.replace('\\n', '\n')

    @property
    def post_install_script(self) -> str:
        return self.expand_post_install_script(self.option('post-install-script'))

    @property
    def kickstart(self) -> Dict[str, str]:
        kickstart = {}
        if self.option('kickstart-pre-install'):
            kickstart['pre-install'] = self.option('kickstart-pre-install')

        if self.option('kickstart-script'):
            kickstart['script'] = self.option('kickstart-script')

        if self.option('kickstart-post-install'):
            kickstart['post-install'] = self.option('kickstart-post-install')

        if self.option('kickstart-metadata'):
            kickstart['metadata'] = self.option('kickstart-metadata')

        if self.option('kickstart-kernel-options'):
            kickstart['kernel-options'] = self.option('kickstart-kernel-options')

        if self.option('kickstart-kernel-options-post'):
            kickstart['kernel-options-post'] = self.option('kickstart-kernel-options-post')

        return kickstart

    @gluetool.utils.cached_property
    def hw_constraints(self) -> Optional[Dict[str, Any]]:

        normalized_constraints = gluetool.utils.normalize_multistring_option(self.option('hw-constraint'))

        if not normalized_constraints:
            return None

        constraints: Dict[str, Any] = {}

        for raw_constraint in normalized_constraints:
            path, value = raw_constraint.split('=', 1)

            if not path or not value:
                raise GlueError('Cannot parse HW constraint: {}'.format(raw_constraint))

            # Walk the path, step by step, and initialize containers along the way. The last step is not
            # a name of another nested container, but actually a name in the last container.
            container = constraints
            path_splitted = path.split('.')

            while len(path_splitted) > 1:
                step = path_splitted.pop(0)

                if step not in container:
                    container[step] = {}

                container = container[step]

            container[path_splitted.pop()] = value

        log_dict(self.logger.debug, 'hw-constraints', constraints)

        return constraints

    @gluetool.utils.cached_property
    def user_data(self) -> Dict[str, str]:

        context = self.shared('eval_context')
        user_data = {}

        # Parse and template user-data-vars from YAML
        user_data_tpl_filepath = self.option('user-data-vars-template-file')

        if user_data_tpl_filepath is not None:
            user_data.update({
                key: gluetool.utils.render_template(str(value), logger=self.logger, **context)
                for key, value in gluetool.utils.load_yaml(user_data_tpl_filepath, logger=self.logger).items()
            })

        log_dict(self.logger.debug, 'user-data', user_data)

        return user_data

    def sanity(self) -> None:
        # test whether parsing of HW requirements yields anything valid - the value is just ignored, we just want
        # to be sure it doesn't raise any exception
        self.hw_constraints

        if self.api_version not in SUPPORTED_API_VERSIONS:
            raise GlueError("Unsupported API version '{}', only {} are supported".format(
                self.api_version,
                ', '.join(SUPPORTED_API_VERSIONS)
            ))

        if self.option('wait') and not self.option('provision'):
            raise GlueError('Option --provision required with --wait.')

        if self.option('provision') and (not self.option('arch') or not self.option('compose')):
            raise GlueError('Options --arch and --compose required with --provision')

        if self.option('guest-logs-enable') and not self.option('guest-logs-config'):
            raise GlueError('Option --guest-logs-config is required to enable guest logs.')

        if self.option('provision') and self.option('analyze-event-log'):
            raise GlueError('Option --provision and --analyze-event-log are mutually exclusive.')

        if self.option('analyze-event-log') and not self.option('error-template-file'):
            raise GlueError('Option --error-template-file is required with --analyze-event-log.')

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(ArtemisProvisioner, self).__init__(*args, **kwargs)

        self.guests: List[ArtemisGuest] = []
        self.api: Optional[ArtemisAPI] = None
        self.guest_logs_template: Optional[ArtemisGuestLogs] = None

    def provisioner_capabilities(self) -> ProvisionerCapabilities:
        '''
        Return description of Artemis provisioner capabilities.

        Follows :doc:`Provisioner Capabilities Protocol </protocols/provisioner-capabilities>`.
        '''

        return ProvisionerCapabilities(
            available_arches=gluetool_modules_framework.libs.ANY
        )

    def provision_guest_start(self,
                              environment: TestingEnvironment,
                              pool: Optional[str] = None,
                              key: Optional[str] = None,
                              priority: Optional[str] = None,
                              ssh_key: Optional[str] = None,
                              options: Optional[List[str]] = None,
                              post_install_script: Optional[str] = None,
                              user_data: Optional[Dict[str, str]] = None,
                              watchdog_dispatch_delay: Optional[int] = None,
                              watchdog_period_delay: Optional[int] = None,
                              workdir: Optional[str] = None,
                              guest_logs: Optional[ArtemisGuestLogs] = None,
                              security_group_rules_ingress: Optional[SecurityGroupRules] = None,
                              security_group_rules_egress: Optional[SecurityGroupRules] = None
                              ) -> ArtemisGuest:
        '''
        Start provisioning of an Artemis guest by submitting a request to Artemis API.

        :param tuple environment: description of the environment caller wants to provision.
            Follows :doc:`Testing Environment Protocol </protocols/testing-environment>`.

        :param str pool: name of the pool

        :param str key: name of key stored in Artemis configuration.

        :param str priority: Priority group of the guest request.
            See Artemis API docs for more.

        :param str ssh_key: the path to public key, that should be used to securely connect to a provisioned machine.
            See Artemis API docs for more.

        :param list option: SSH options that would be used when securely connecting to a provisioned guest via SSH.

        :param int watchdog_dispatch_delay: How long (seconds) before the guest "is-alive" watchdog is dispatched.

        :param int watchdog_period_delay: How often (seconds) check that the guest "is-alive".

        :param str workdir: working directory where all runtime data should be stored.
            For example the workding directory of a schedule entry.

        :rtype: ArtemisGuest
        :returns: ArtemisGuest instance.
        '''
        assert self.api
        response = self.api.create_guest(environment,
                                         pool=pool,
                                         keyname=key,
                                         priority=priority,
                                         user_data=user_data,
                                         post_install_script=post_install_script,
                                         watchdog_dispatch_delay=watchdog_dispatch_delay,
                                         watchdog_period_delay=watchdog_period_delay,
                                         security_group_rules_ingress=security_group_rules_ingress,
                                         security_group_rules_egress=security_group_rules_egress)

        guestname = response.get('guestname')
        hostname = six.ensure_str(response['address']) if response['address'] is not None else None

        guest = ArtemisGuest(self, guestname, hostname, environment,
                             port=response['ssh']['port'],
                             username=six.ensure_str(response['ssh']['username']),
                             key=ssh_key,
                             options=options,
                             workdir=workdir,
                             guest_logs=guest_logs)
        guest.info('Guest is being provisioned')
        log_dict(guest.debug, 'Created guest request', response)
        log_dict(guest.info, 'Created guest request with environment', response['environment'])

        return guest

    def provision_guest_wait(self, guest: ArtemisGuest) -> None:
        '''
        Wait for provisioning of an Artemis guest.

        :param ArtemisGuest guest: Provisioned guest.
        '''
        assert self.api
        try:
            timeout = self._adj_timeout()
            guest._wait_ready(timeout=timeout, tick=self.option('ready-tick'))
            response = self.api.inspect_guest(guest.artemis_id)
            guest.hostname = six.ensure_str(response['address']) if response['address'] is not None else None
            guest.info("Guest is ready: {}".format(guest))

            guest._wait_alive(self.option('connect-timeout'),
                              self.option('activation-timeout'), self.option('activation-tick'),
                              self.option('echo-timeout'), self.option('echo-tick'),
                              self.option('boot-timeout'), self.option('boot-tick'))
            guest.info('Guest has become alive')

            self.api.dump_events(guest)

        except (GlueError, KeyboardInterrupt) as exc:
            message = 'KeyboardInterrupt' if isinstance(exc, KeyboardInterrupt) else str(exc)
            self.warn("Exception while provisioning guest: {}".format(message))
            six.reraise(*sys.exc_info())

    def provision(
        self,
        environment: TestingEnvironment,
        workdir: Optional[str] = None,
        **kwargs: Any
    ) -> List[ArtemisGuest]:
        '''
        Provision Artemis guest(s).

        :param tuple environment: description of the environment caller wants to provision.
            Follows :doc:`Testing Environment Protocol </protocols/testing-environment>`.
        :param str workdir: working directory where all runtime data should be stored.
            For example the workding directory of a schedule entry.
        :param ArtemisGuestLogs logs: List of guest logs to process.

        :rtype: list
        :returns: List of ArtemisGuest instances or ``None`` if it wasn't possible to grab the guests.
        '''

        pool = self.option('pool')
        key = self.option('key')
        ssh_key = self.option('ssh-key')
        priority = self.option('priority-group')
        options = normalize_multistring_option(self.option('ssh-options'))
        # NOTE(ivasilev) Use artemis module requested post-install-script or the one from the environment
        post_install_script = self.option('post-install-script')
        provisioning = (environment.settings or {}).get('provisioning') or {}
        if not post_install_script:
            post_install_script = provisioning.get('post_install_script')
        security_group_rules_ingress = provisioning.get('security_group_rules_ingress') or None
        security_group_rules_egress = provisioning.get('security_group_rules_egress') or None

        if self.option('snapshots'):
            environment.snapshots = True

        user_data = self.user_data

        # Add tags from environment settings if exists
        tags = provisioning.get('tags', {})
        if tags:
            user_data.update(tags)

        watchdog_dispatch_delay = self.option('watchdog-dispatch-delay')
        # Get watchdog-dispatch-delay from environment settings if exists
        if watchdog_dispatch_delay is None:
            watchdog_dispatch_delay = provisioning.get('watchdog_dispatch_delay')

        watchdog_period_delay = self.option('watchdog-period-delay')
        # Get watchdog-period-delay from environment settings if exists
        if watchdog_period_delay is None:
            watchdog_period_delay = provisioning.get('watchdog_period_delay')

        # Prevent pipeline cancellation before provisioned guest id is stored (TFT-3300).
        with self.shared('pipeline_cancellation_lock') or nullcontext():
            guest = self.provision_guest_start(
                environment,
                pool=pool,
                key=key,
                priority=priority,
                ssh_key=ssh_key,
                options=options,
                post_install_script=post_install_script,
                user_data=user_data,
                watchdog_dispatch_delay=watchdog_dispatch_delay,
                watchdog_period_delay=watchdog_period_delay,
                workdir=workdir,
                # NOTE: create a copy of the logs template, we need a separate instance for each guest
                guest_logs=[
                    attrs.evolve(log) for log in self.guest_logs_template
                ] if self.guest_logs_template else None,
                security_group_rules_ingress=security_group_rules_ingress,
                security_group_rules_egress=security_group_rules_egress
            )

            self.guests.append(guest)

        event_log_uri = self.shared('artifacts_location', guest.event_log_path, self.logger)
        guest.info("guest event log: {}".format(event_log_uri))

        if self.option('guest-logs-enable'):
            guest.start_guest_logging()

        self.provision_guest_wait(guest)

        guest.info('Guest provisioned')

        return [guest]

    def load_guest_logs_template(self) -> None:
        """
        Load guest logging configuration.
        """
        logs_config: str = normalize_path(self.option('guest-logs-config'))

        try:
            self.guest_logs_template = load_yaml(
                logs_config,
                unserializer=gluetool.utils.create_cattrs_unserializer(ArtemisGuestLogs)
            )
            log_dict(
                self.debug,
                "loaded guest logs configuration from '{}'".format(logs_config),
                self.guest_logs_template
            )

        except cattrs.errors.BaseValidationError as error:
            log_dict(self.error, 'validation errors', cattrs.transform_error(error))
            raise GlueError('Failed to validate {} file'.format(logs_config))

        except TypeError:
            raise GlueError('Failed to validate {} file: {}'.format(logs_config,
                "The file has invalid content, it needs to contain a list of dicts with `type`, `filename` and `datetime_filename` keys"))  # noqa

        except GlueError as error:
            raise GlueError('Could not load {} file: {}'.format(logs_config, error))

    def event_log_error(self, events: Any) -> Optional[str]:
        """
        Analyze guest event log and return a reasonable error rendered using the error template.
        """
        if not events:
            self.warn("Skipping event log analysis, no events available.", sentry=True)
            return None

        if not self.error_template_file:
            self.warn("Skipping event log analysis, no error template file available.")
            return None

        if not self.error_events:
            self.warn("Skipping event log analysis, no error events defined to analyze.")
            return None

        last_events = {}

        # Search for last events in the event log. The event log is ordered newest-first,
        # so the first occurrence of each event type is the most recent one.
        for eventname in self.error_events:
            for event in events:
                if event['eventname'] == eventname:
                    last_events[eventname] = event
                    break

        with open(self.error_template_file, "r") as template_file:
            error_template = template_file.read()

        try:
            return render_template(error_template, event=last_events)
        except GlueError as error:
            self.warn("Could not render Artemis error template: {}".format(str(error)), sentry=True)
            return None

    def execute(self) -> None:

        self.api = ArtemisAPI(self,
                              self.api_url,
                              self.api_version,
                              self.option('api-call-timeout'),
                              self.option('api-call-tick'))

        # TODO: print Artemis API version when version endpoint is implemented
        self.info('Using Artemis API {}'.format(self.api.url))

        if self.option('guest-logs-enable'):
            if self.api.version < API_FEATURE_VERSIONS['log-types']:
                raise GlueError('Artemis API version {} does not support guest logs.'.format(self.api.version))
            self.load_guest_logs_template()

        # Analyze guest event log
        if self.guest_event_log:
            log_blob(
                self.info,
                "Guest event log error",
                self.event_log_error(self.guest_event_log) or "Not available"
            )
            return

        if not self.option('provision'):
            return

        provision_count = self.option('provision')
        arch = self.option('arch')
        compose = self.option('compose')

        kickstart = self.kickstart

        environment = TestingEnvironment(arch=arch,
                                         compose=compose,
                                         kickstart=kickstart)

        for num in range(provision_count):
            self.info("Trying to provision guest #{}".format(num+1))
            guest = self.provision(environment,
                                   provision_count=provision_count)[0]
            guest.info("Provisioned guest #{} {}".format(num+1, guest))

        if self.option('setup-provisioned'):
            for guest in self.guests:
                guest.setup()

        wait = self.option('wait')
        if wait:
            self.warn('Waiting for {} seconds as requested...'.format(wait))
            time.sleep(wait)

    def remove_from_list(self, guest: ArtemisGuest) -> None:
        if guest not in self.guests:
            self.warn('{} is not found in guests list'.format(guest.name))
            return

        self.guests.remove(guest)

    def destroy(self, failure: Optional[Any] = None) -> None:
        self.destroying = True

        if not self.guests:
            self.info('no guests to remove during module destroy')
            return

        self.info('removing {} guest(s) during module destroy'.format(len(self.guests)))

        assert self.api

        for guest in self.guests[:]:
            guest.destroy()

    def _adj_timeout(self) -> int:
        timeout = int(self.option('ready-timeout'))
        if self.option('ready-timeout-from-pipeline'):
            if self.has_shared('testing_farm_request'):
                offset = self.option('ready-timeout-from-pipeline-offset') or 0
                request = self.shared('testing_farm_request')
                if request and request.pipeline_timeout > 0:
                    user_timeout = (request.pipeline_timeout * 60) - offset
                    if user_timeout > 0:
                        timeout = user_timeout
        return timeout
