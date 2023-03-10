# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import collections
import logging
import re

import pytest
import requests
from requests.exceptions import ConnectionError
from urllib3.exceptions import NewConnectionError
from mock import MagicMock

from gluetool import GlueError
from gluetool.utils import load_yaml
from gluetool_modules_framework.tests import testing_asset
from gluetool_modules_framework.libs import ANY
from gluetool_modules_framework.libs.guest import NetworkedGuest
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.provision.artemis import (
    ArtemisProvisioner, PipelineCancelled, ProvisionerCapabilities,
    SUPPORTED_API_VERSIONS
)

from . import create_module, check_loadable, patch_shared


Response = collections.namedtuple('Response', ['status_code', 'headers', 'text', 'json'])


class MockRequests():
    requests = None

    def __init__(self, mocked_requests):
        MockRequests.requests = mocked_requests

        # initialize generators
        for method in self.requests.keys():
            for mock in self.requests[method]:
                if 'generator' in mock and mock['generator']:
                    mock['generator'] = (
                        item for item in mock['generator']
                    )

    @staticmethod
    def handle_mocks(url, mocks, method):
        print("handling method '{}' for url '{}'".format(url, method))
        for mock in mocks:
            if re.search(mock['url'], url):
                if 'generator' in mock:
                    return Response(mock['status_code'], '', '', lambda: next(mock['generator']))
                return Response(mock['status_code'], '', '', lambda: mock.get('response'))
        raise Exception("No mock matched url '{}' for method '{}'".format(url, method))

    @staticmethod
    def delete(url, json=None):
        return MockRequests.handle_mocks(url, MockRequests.requests['delete'], 'delete')

    @staticmethod
    def get(url, json=None):
        return MockRequests.handle_mocks(url, MockRequests.requests['get'], 'get')

    @staticmethod
    def post(url, json=None):
        return MockRequests.handle_mocks(url, MockRequests.requests['post'], 'post')


@pytest.fixture(name='module')
def fixture_module():
    module = create_module(ArtemisProvisioner)[1]

    module._config['api-url'] = "https://artemis.xyz"
    module._config['api-version'] = "v0.0.28"

    module._config['connect-timeout'] = 1
    module._config['ready-tick'] = 1
    module._config['ready-timeout'] = 2
    module._config['activation-tick'] = 1
    module._config['activation-timeout'] = 2
    module._config['api-call-tick'] = 1
    module._config['api-call-timeout'] = 2
    module._config['echo-tick'] = 1
    module._config['echo-timeout'] = 2
    module._config['boot-tick'] = 1
    module._config['boot-timeout'] = 2
    module._config['ready-tick'] = 1
    module._config['ready-timeout'] = 2
    module._config['snapshot-ready-tick'] = 1
    module._config['snapshot-ready-timeout'] = 2

    return module


@pytest.fixture(name='scenario')
def fixture_scenario(module, monkeypatch, request):
    asset = testing_asset('artemis', '{}.yaml'.format(request.param))
    scenario = load_yaml(asset)

    module._mocked_requests = MockRequests(scenario['requests'])
    module._mocked_wait_alive = MagicMock()

    monkeypatch.setattr(requests, 'get', module._mocked_requests.get)
    monkeypatch.setattr(requests, 'post', module._mocked_requests.post)
    monkeypatch.setattr(requests, 'delete', module._mocked_requests.delete)

    monkeypatch.setattr(NetworkedGuest, 'wait_alive', module._mocked_wait_alive)

    if 'config' in scenario:
        module._config.update(scenario['config'])

    patch_shared(monkeypatch, module, {
        'setup_guest': None
    })

    module.execute()

    if 'environment' in scenario:
        environment = TestingEnvironment(
            arch=scenario['environment']['arch'],
            compose=scenario['environment']['compose'],
            snapshots=scenario['environment']['snapshots']
        )

    else:
        environment = None

    guest = scenario['asserts'].get('guest')
    snapshot = scenario['asserts'].get('snapshot')
    exception = scenario['asserts'].get('exception')

    return environment, guest, snapshot, exception


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/provision/artemis.py', 'ArtemisProvisioner')


def test_sanity_shared(module):
    for shared in ['provision', 'provisioner_capabilities']:
        assert module.glue.has_shared(shared) is True


def test_execute(monkeypatch, module, log):
    assert module.pipeline_cancelled == False

    scenario = load_yaml(testing_asset('artemis', 'successful.yaml'))

    monkeypatch.setattr(requests, 'get', MockRequests(scenario['requests']).get)
    module.execute()

    assert log.match(levelno=logging.INFO, message='Using Artemis API https://artemis.xyz/')


def test_api_call(monkeypatch, module, log):
    scenario = load_yaml(testing_asset('artemis', 'successful.yaml'))

    monkeypatch.setattr(requests, 'get', MockRequests(scenario['requests']).get)
    module.execute()

    # test unexpected status code
    with pytest.raises(
        GlueError,
        match="Artemis API call failed: Condition 'api_call' failed to pass within given time"
    ):
        module.api.api_call('guests/', expected_status_code=400)

    assert log.match(
        levelno=logging.DEBUG,
        message="check failed with 'Artemis API error: Call to Artemis API failed, HTTP 200: ', assuming failure"
    )

    # test invalid HTTP method
    with pytest.raises(
        GlueError,
        match="Artemis API call failed: Condition 'api_call' failed to pass within given time"
    ):
        module.api.api_call('some-url', method='some-method')

    assert log.match(
        levelno=logging.DEBUG,
        message="check failed with 'Unknown HTTP method some-method', assuming failure"
    )

    monkeypatch.setattr(
        requests,
        'get',
        MagicMock(side_effect=requests.exceptions.ConnectionError('Connection aborted dude'))
    )

    with pytest.raises(
        GlueError,
        match="Artemis API call failed: Condition 'api_call' failed to pass within given time"
    ):
        module.api.api_call('some-url')

    assert log.match(
        levelno=logging.DEBUG,
        message="check failed with 'Connection aborted dude', assuming failure"
    )

    monkeypatch.setattr(requests, 'get', MagicMock(side_effect=requests.exceptions.ConnectionError('some-error')))

    with pytest.raises(
        ConnectionError,
        match="some-error"
    ):
        module.api.api_call('some-url')


def test_pipeline_cancelled(module):
    module.glue.pipeline_cancelled = True

    with pytest.raises(PipelineCancelled):
        module.execute()


def test_new_connection_error(module, monkeypatch, log):
    monkeypatch.setattr(requests, 'get', MagicMock(side_effect=NewConnectionError('', '')))

    with pytest.raises(
        GlueError,
        match="Artemis API call failed: Condition 'api_call' failed to pass within given time"
    ):
        module.execute()

    assert log.match(levelno=logging.DEBUG, message="Retrying due to NewConnectionError")


@pytest.mark.parametrize('constraint, expected', [
    (
        'memory=>4GB,cpu.cores=<8,cpu.model=some-model',
        {
            'memory': '>4GB',
            'cpu': {
                'cores': '<8',
                'model': 'some-model'
            }
        }
    ),
    (
        None,
        None
    ),
    (
        'memory=',
        (GlueError, 'Cannot parse HW constraint: memory=')
    )
])
def test_hw_constraints(module, constraint, expected):
    module._config = {
        'hw-constraint': constraint
    }

    if not isinstance(expected, tuple):
        assert module.hw_constraints == expected
        return

    exc, msg = expected
    with pytest.raises(exc, match=msg):
        module.hw_constraints


@pytest.mark.parametrize('filename, context, expected', [
    (
        'user_data.yaml',
        {'__TEST_VAR__': 'test string'},
        {
            'TEST_VARIABLE': 'test string',
            'another-user-data-field': 'a string value'
        }
    ),
    (
        None,
        {},
        {}
    ),
    (
        'invalid_filename',
        {},
        (
            GlueError,
            'File \'gluetool_modules_framework/tests/assets/artemis/invalid_filename\' does not exist'
        )
    )
])
def test_user_data(module, monkeypatch, filename, context, expected):
    module._config = {
        'user-data-vars-template-file': testing_asset('artemis', filename) if filename else None
    }

    patch_shared(monkeypatch, module, {'eval_context': context})

    if not isinstance(expected, tuple):
        assert module.user_data == expected
        return

    exc, msg = expected
    with pytest.raises(exc, match=msg):
        module.user_data


def test_sanity(module):
    assert module.sanity() is None

    module._config = {
        'provision': 1
    }

    with pytest.raises(GlueError, match='Missing required option: --arch'):
        module.sanity()

    module._config = {
        'provision': 1,
        'arch': 'some-arch'
    }

    with pytest.raises(
        GlueError,
        match='Unsupported API version, only {} are supported'.format(', '.join(SUPPORTED_API_VERSIONS))
    ):
        module.sanity()

    module._config.update({
        'provision': 1,
        'arch': 'some-arch',
        'api-version': 'unsupported'
    })

    with pytest.raises(
        GlueError,
        match='Unsupported API version, only {} are supported'.format(', '.join(SUPPORTED_API_VERSIONS))
    ):
        module.sanity()

    for version in SUPPORTED_API_VERSIONS:
        module._config.update({
            'provision': 1,
            'arch': 'some-arch',
            'api-version': version
        })
        assert module.sanity() is None


def test_provisioner_capabilities(module):
    assert module.provisioner_capabilities().available_arches == ANY


@pytest.mark.parametrize('scenario', [
    'successful',
    'error',
    'provision_and_setup_from_execute',
    'snapshot',
    'ip_not_ready',
    'snapshot_error'
], indirect=True)
def test_provision(module, scenario):
    environment, guest, snapshot, exception = scenario

    # exception cases, without snapshots
    if exception and not snapshot:
        with pytest.raises(GlueError, match=exception):
            module.provision(environment)
        return

    if environment:
        module.provision(environment)

    for key in guest.keys():
        assert getattr(module.guests[0], key) == guest[key]

    # exception cases, with snapshots
    if exception:
        with pytest.raises(GlueError, match=exception):
            module.guests[0].create_snapshot()
        return

    if snapshot:
        module.guests[0].create_snapshot()
        module.guests[0].restore_snapshot(module.guests[0]._snapshots[0])

    module.destroy()


def test_api_url_option(module, monkeypatch):
    module._config['api-url'] = '{{ some_api_url_template }}'
    patch_shared(monkeypatch, module, {'eval_context': {'some_api_url_template': 'foo'}})
    assert module.api_url == 'foo'
    assert module.option('api-url') == '{{ some_api_url_template }}'
