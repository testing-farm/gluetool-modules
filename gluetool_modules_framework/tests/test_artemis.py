# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import collections
import logging
import os
import re
from gluetool.glue import Module

import pytest
import requests
import urllib3.exceptions
import gluetool_modules_framework.testing_farm.testing_farm_request
from requests.exceptions import ConnectionError
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
ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets', 'artemis')


class MockRequests():
    requests = None
    module = None
    cancelled = False

    def __init__(self, mocked_requests, module):
        MockRequests.requests = mocked_requests
        MockRequests.module = module

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
                if 'pipeline_cancelled' in mock:
                    assert MockRequests.module
                    MockRequests.cancelled = True
                    MockRequests.module.glue.pipeline_cancelled = True

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

    module._config['api-url'] = "https://artemis.xyz/v0.0.28/"
    module._config['api-version'] = "0.0.28"

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
    module._config['ready-timeout-from-pipeline'] = False
    module._config['ready-timeout-from-pipeline-offset'] = 1
    module._config['snapshot-ready-tick'] = 1
    module._config['snapshot-ready-timeout'] = 2

    return module


@pytest.fixture(name='scenario')
def fixture_scenario(module, monkeypatch, request, tmpdir):
    asset = testing_asset('artemis', '{}.yaml'.format(request.param))
    scenario = load_yaml(asset)

    # console logs are tested only for 'successful' scenario
    if request.param == 'successful':
        module._config['guest-logs-enable'] = True
        module._config['guest-logs-config'] = os.path.abspath(testing_asset('artemis', 'artemis-log-config.yaml'))

    module._mocked_requests = MockRequests(scenario['requests'], module)
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

    # Save guest events yaml file to a tmp directory
    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        module.execute()

    if 'environment' in scenario:
        environment = TestingEnvironment(
            arch=scenario['environment']['arch'],
            compose=scenario['environment']['compose'],
            snapshots=scenario['environment']['snapshots'],
            settings=scenario['environment']['settings']
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

    monkeypatch.setattr(requests, 'get', MockRequests(scenario['requests'], module).get)
    module.execute()

    assert log.match(levelno=logging.INFO, message='Using Artemis API https://artemis.xyz/v0.0.28/')


def test_api_call(monkeypatch, module, log):
    scenario = load_yaml(testing_asset('artemis', 'successful.yaml'))

    monkeypatch.setattr(requests, 'get', MockRequests(scenario['requests'], module).get)
    module.execute()

    # test unexpected status code
    with pytest.raises(
        GlueError,
        match="Artemis API call failed: Condition 'api_call' failed to pass within given time"
    ):
        module.api.api_call('guests/', expected_status_codes=[400])

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
        message="check failed with 'requests.exceptions.ConnectionError: Connection aborted dude', assuming failure"
    )

    monkeypatch.setattr(
        requests,
        'get',
        MagicMock(side_effect=requests.exceptions.ConnectionError('some-other-error'))
    )

    with pytest.raises(
        GlueError,
        match="Artemis API call failed: Condition 'api_call' failed to pass within given time"
    ):
        module.api.api_call('some-url')


@pytest.mark.parametrize('scenario', ['successful'], indirect=True)
def test_pipeline_cancelled(module, scenario, log):
    environment, guest, snapshot, exception = scenario
    module.provision(environment)
    assert log.match(levelno=logging.INFO, message='Created guest request with environment:\n{}')
    for key in guest.keys():
        assert getattr(module.guests[0], key) == guest[key]

    module.glue.pipeline_cancelled = True

    with pytest.raises(PipelineCancelled):
        module.execute()

    module.destroy()
    assert log.match(levelno=logging.INFO, message='removing 1 guest(s) during module destroy')
    assert log.match(levelno=logging.INFO, message='destroying guest')
    assert log.match(levelno=logging.INFO, message='successfully released')


@pytest.mark.parametrize('scenario', ['pipeline_cancelled'], indirect=True)
def test_pipeline_cancelled_before_provision_finished(module, scenario, log):
    environment, guest, snapshot, exception = scenario

    with pytest.raises(GlueError, match="Guest couldn't be provisioned: Pipeline was cancelled, aborting"):
        module.provision(environment)

    assert log.match(levelno=logging.INFO, message='Created guest request with environment:\n{}')

    module.destroy()

    assert log.match(levelno=logging.INFO, message='removing 1 guest(s) during module destroy')
    assert log.match(levelno=logging.INFO, message='destroying guest')
    assert log.match(levelno=logging.INFO, message='successfully released')


@pytest.mark.parametrize('exception', [
    urllib3.exceptions.HTTPError(),
    urllib3.exceptions.ProtocolError(),
    urllib3.exceptions.NewConnectionError('', ''),
], ids=lambda exception: exception)
def test_api_call_exceptions(module, monkeypatch, log, exception):
    monkeypatch.setattr(requests, 'get', MagicMock(side_effect=exception))

    with pytest.raises(
        GlueError,
        match="Artemis API call failed: Condition 'api_call' failed to pass within given time"
    ):
        module.execute()

    assert log.match(
        levelno=logging.DEBUG,
        message="Retrying Artemis API call due to {}.{} exception".format(
            exception.__module__, exception.__class__.__qualname__
        )
    )


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


@pytest.mark.parametrize('options, expected', [
    (
        {
            'kickstart-pre-install': 'pre-install\nscript',
            'kickstart-script': 'main script',
            'kickstart-post-install': 'post-install',
            'kickstart-metadata': 'metadata',
            'kickstart-kernel-options': 'kernel options',
            'kickstart-kernel-options-post': 'kernel options post'
        },
        {
            'pre-install': 'pre-install\nscript',
            'script': 'main script',
            'post-install': 'post-install',
            'metadata': 'metadata',
            'kernel-options': 'kernel options',
            'kernel-options-post': 'kernel options post'
        }
    ),
    (
        {
            'kickstart-pre-install': 'pre-install script',
            'kickstart-script': 'main script'
        },
        {
            'pre-install': 'pre-install script',
            'script': 'main script'
        }
    ),
    (
        {},
        {}
    ),
    (
        {
            'kickstart-pre-install': '',
            'kickstart-script': None,
            'kickstart-post-install': '  ',
            'kickstart-metadata': '\n',
            'kickstart-kernel-options': '\t',
            'kickstart-kernel-options-post': ' \n '
        },
        {
            'post-install': '  ',
            'metadata': '\n',
            'kernel-options': '\t',
            'kernel-options-post': ' \n '
        }
    )
])
def test_kickstart(module, options, expected):
    for key, value in options.items():
        module._config[key] = value

    assert module.kickstart == expected


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

    with pytest.raises(
        GlueError,
        match="Unsupported API version '', only {} are supported".format(', '.join(SUPPORTED_API_VERSIONS))
    ):
        module.sanity()

    module._config.update({
        'provision': 1,
        'arch': 'some-arch',
        'api-version': 'unsupported'
    })

    with pytest.raises(
        GlueError,
        match="Unsupported API version 'unsupported', only {} are supported".format(', '.join(SUPPORTED_API_VERSIONS))
    ):
        module.sanity()

    for config in [{
        'provision': 1,
        'arch': 'some-arch',
        'api-version': list(SUPPORTED_API_VERSIONS)[0]
    }, {
        'compose': 'some-compose',
        'arch': None,
    }]:
        module._config.update(config)
        with pytest.raises(
            GlueError,
            match="Options --arch and --compose required with --provision"
        ):
            module.sanity()

    with pytest.raises(
        GlueError,
        match="Options --arch and --compose required with --provision"
    ):
        module.sanity()

    for version in SUPPORTED_API_VERSIONS:
        module._config.update({
            'provision': 1,
            'arch': 'some-arch',
            'compose': 'some-compose',
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
def test_provision(monkeypatch, module, scenario, tmpdir, log):
    environment, guest, snapshot, exception = scenario

    # Save guest events yaml file to a tmp directory
    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        # exception cases, without snapshots
        if exception and not snapshot:
            with pytest.raises(GlueError, match=exception):
                module.provision(environment)
            return

        if environment:
            module.provision(environment)
            assert log.match(levelno=logging.INFO, message='Created guest request with environment:\n{}')

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

        if module.option('keep'):
            return

        # destroy directly guests as test scheduler modules would
        module.guests[0].destroy()
        assert log.match(levelno=logging.INFO, message='destroying guest')
        assert log.match(levelno=logging.INFO, message='successfully released')

        # there should be nothing else to cleanup
        module.destroy()
        assert log.match(levelno=logging.INFO, message='no guests to remove during module destroy')


def test_adj_timeout(monkeypatch, module, log):
    patch_shared(monkeypatch, module, {'testing_farm_request': {'settings': {'pipeline': {'timeout': 50}}}})
    module._config['ready-timeout-from-pipeline'] = False  # disable overriding ready-timeout from request
    module._config['ready-timeout-offset'] = 3
    module._config['ready-timeout'] = 7
    timeout = module._adj_timeout()
    assert type(timeout) == int
    assert timeout == 7


def test_adj_timeout_enabled(monkeypatch, module, log):
    # without a testing-farm-request module, read-timeout should be used
    module._config['ready-timeout-from-pipeline'] = True
    module._config['ready-timeout-from-pipeline-offset'] = 3
    module._config['ready-timeout'] = 7
    timeout = module._adj_timeout()
    assert type(timeout) == int
    assert timeout == 7


def test_adj_timeout_tfrequest(monkeypatch, module, log):
    patch_shared(monkeypatch, module, {'testing_farm_request': {'settings': {'pipeline': {'timeout': 50}}}})
    module._config['ready-timeout-from-pipeline'] = True
    module._config['ready-timeout-from-pipeline-offset'] = 3
    module._config['ready-timeout'] = 7
    timeout = module._adj_timeout()
    assert type(timeout) == int
    assert timeout == 2997


def test_adj_timeout_zero(monkeypatch, module, log):
    # if the result of pipeline timeout and offset is less than one, use ready-timeout
    patch_shared(monkeypatch, module, {'testing_farm_request': {'settings': {'pipeline': {'timeout': 7}}}})
    module._config['ready-timeout-from-pipeline'] = True
    module._config['ready-timeout-from-pipeline-offset'] = 420
    module._config['ready-timeout'] = 20
    timeout = module._adj_timeout()
    assert type(timeout) == int
    assert timeout == 20


def test_adj_timeout_no_offset(monkeypatch, module, log):
    patch_shared(monkeypatch, module, {'testing_farm_request': {'settings': {'pipeline': {'timeout': 50}}}})
    module._config['ready-timeout-from-pipeline'] = True
    module._config['ready-timeout-from-pipeline-offset'] = None
    module._config['ready-timeout'] = 21
    timeout = module._adj_timeout()
    assert type(timeout) == int
    assert timeout == 3000


def test_api_url_option(module, monkeypatch):
    module._config['api-url'] = '{{ some_api_url_template }}'
    patch_shared(monkeypatch, module, {'eval_context': {'some_api_url_template': 'foo'}})
    assert module.api_url == 'foo'
    assert module.option('api-url') == '{{ some_api_url_template }}'


@pytest.mark.parametrize('scenario', [
    'successful',
], indirect=True)
def test_console_log_and_workdir(monkeypatch, module, scenario, tmpdir):
    environment, guest, _, _ = scenario

    # Change working directory to a tmp directory
    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        os.mkdir('workdir')

        module.provision(environment, workdir='workdir')
        module.destroy()

        assert os.path.exists('workdir/console-{}.log'.format(guest['name']))
        with open('workdir/console-{}.log'.format(guest['name'])) as log:
            assert log.read() == 'This is a serial console log'

        assert os.path.exists('workdir/flasher-debug-{}.log'.format(guest['name']))
        with open('workdir/flasher-debug-{}.log'.format(guest['name'])) as log:
            assert log.read() == 'This is a flasher debug log'

        # This log is simulated as empty, in config there is `save_empty: false`
        # thus the file should not exist
        assert not os.path.exists('workdir/flasher-event-{}.log'.format(guest['name']))


@pytest.mark.parametrize('input_script, expected_script', [
    ('#!/bin/sh\necho hello world', '#!/bin/sh\necho hello world', ),
    (os.path.join(ASSETS_DIR, 'post-install-script.sh'), '#!/bin/sh\necho hello world from file\n')
])
def test_expand_post_install_script(module, input_script, expected_script):
    assert module.expand_post_install_script(input_script) == expected_script
