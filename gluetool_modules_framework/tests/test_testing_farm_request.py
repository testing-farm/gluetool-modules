# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import logging
import psutil
import pytest
import gluetool_modules_framework.testing_farm.testing_farm_request
import os
import gluetool
import contextlib
import time
from mock import MagicMock

from gluetool_modules_framework.testing_farm.testing_farm_request import RequestConflictError
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from . import create_module, patch_shared
from requests.exceptions import HTTPError

ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets', 'testing_farm')


def _load_assets(name):
    return gluetool.utils.load_json(os.path.join(ASSETS_DIR, '{}.json'.format(name)))


REQUESTS = {
    'fakekey': {
        '1': _load_assets('request1'),
        '2': _load_assets('request2'),
        '3': _load_assets('request3'),
    }
}

PUT_REQUESTS = {}

REQUESTS_USER = {
    'fakekey': {
        'user': _load_assets('user'),
    }
}


class ResponseMock():
    status_code = 200
    text = 'hello'

    def json(self):
        return REQUESTS['fakekey']['1']


class Response404(ResponseMock):
    status_code = 404


class Response409(ResponseMock):
    status_code = 409


class ResponseInvalidJSON(ResponseMock):
    def json(self):
        raise ValueError


class RequestsMock():
    def get(self, url, json):
        return ResponseMock()

    def put(self, url, json):
        return ResponseMock()

    def post(self, url, json):
        return ResponseMock()

    def request_404(self, url, json):
        return Response404()

    def request_409(self, url, json):
        return Response409()

    def request_invalid_json(self, url, json):
        return ResponseInvalidJSON()

    def request_http_error(self, url, json):
        raise HTTPError


@contextlib.contextmanager
def requests_mock():
    try:
        yield RequestsMock()
    finally:
        pass


@pytest.fixture(name='module')
def fixture_module():
    api = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmAPI
    api.get_request = lambda _, id, key: REQUESTS[key][id]
    api.get_user = lambda _, id, key: REQUESTS_USER[key][id]
    api.put_request = lambda _, id, payload: PUT_REQUESTS.update({id: payload})
    module = create_module(gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmRequestModule)[1]
    module._config.update({
        'api-url': 'fakeurl',
        'api-key': 'fakekey',
        'retry-tick': 10,
    })
    return module


@pytest.fixture(name='module_request')
def fixture_module_request():
    module_request = create_module(gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmRequest)[1]
    return module_request


@pytest.fixture(name='requests_mock')
def fixture_requests_mock():
    gluetool_modules_framework.testing_farm.testing_farm_request.requests = requests_mock


@pytest.fixture(name='module_api')
def fixture_module_api(requests_mock):
    module_api = create_module(
        gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmAPI, add_shared=False
    )[1]
    return module_api


@pytest.fixture(name='request1')
def fixture_request1(module, monkeypatch):
    module._config.update({'request-id': '1'})
    module._tf_api = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmAPI(
        module, module.option('api-url')
    )
    patch_shared(monkeypatch, module, {}, callables={'add_secrets': MagicMock(return_value=None)})
    module._tf_request = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmRequest(module)


@pytest.fixture(name='request2')
def fixture_request2(module):
    module._config.update({'request-id': '2'})
    module._tf_api = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmAPI(
        module, module.option('api-url')
    )
    module._tf_request = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmRequest(module)


@pytest.fixture(name='request3')
def fixture_request3(module, monkeypatch):
    module._config.update({'request-id': '3'})
    module._tf_api = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmAPI(
        module, module.option('api-url')
    )
    patch_shared(monkeypatch, module, {}, callables={'add_secrets': MagicMock(return_value=None)})
    module._tf_request = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmRequest(module)


# TestingFarmAPI class tests
def test_request_type_error(module_api):
    with pytest.raises(gluetool.GlueError, match='No request type specified'):
        module_api._request('', None, None)

    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    with pytest.raises(gluetool.GlueError, match="Invalid request type 'sometype'"):
        module_api._request('', type='sometype')


def test_get_request(module_api):
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    module_api.get_request('1', 'fakekey')


def test_get_request_404(module_api):
    RequestsMock.get = RequestsMock.request_404
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    with pytest.raises(gluetool.GlueError, match="Request '1' was not found"):
        module_api.get_request('1', 'fakekey')


def test_put_request(module_api):
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    module_api.put_request('1', {'hello': 'world'})


def test_put_request_409(module_api):
    RequestsMock.put = RequestsMock.request_409
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    with pytest.raises(RequestConflictError, match=""):
        module_api.put_request('1', 'fakekey')


def test_get_request_invalid_json(module_api):
    RequestsMock.get = RequestsMock.request_invalid_json
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    with pytest.raises(ValueError):
        module_api.get_request('1', 'fakekey')


def test_put_request_error(module_api):
    with pytest.raises(gluetool.GlueError, match="payload is required for 'post' and 'put' requests"):
        module_api.put_request('', None)


def test_put_request_404(module_api):
    RequestsMock.put = RequestsMock.request_404
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    with pytest.raises(gluetool.GlueError, match="Request '1' not found."):
        module_api.put_request('1', {'hello': 'world'})


# TestingFarmRequest class tests
def test_update_empty(module, request2):
    request = module._tf_request
    request.update()
    assert PUT_REQUESTS['2'] == {'api_key': 'fakekey'}


def test_update(module, request2, monkeypatch):
    patch_shared(monkeypatch, module, {'xunit_testing_farm_file': 'xunitfile'})
    request = module._tf_request
    request.update(
        state='somestate',
        overall_result='someresult',
        xunit='somexunit',
        summary='somesummary',
        artifacts_url='someurl'
    )
    assert PUT_REQUESTS['2'] == {
        'api_key': 'fakekey',
        'state': 'somestate',
        'result': {
            'overall': 'someresult',
            'xunit': 'somexunit',
            'summary': 'somesummary',
            'xunit_url': 'someurl/xunitfile'
        },
        'run': {
            'artifacts': 'someurl',
        }
    }


def test_update_conflict(module, request2, monkeypatch, log):
    patch_shared(monkeypatch, module, {'xunit_testing_farm_file': 'xunitfile'})
    request = module._tf_request
    module._tf_api.put_request = MagicMock(side_effect=RequestConflictError('some-message', MagicMock()))

    # state is not canceled but request failed to update, that is unexpected and the update should blow up
    module._tf_api.get_request = MagicMock(return_value={'state': 'running'})
    with pytest.raises(RequestConflictError, match='some-message'):
        request.update(
            state='error',
        )
    assert log.match(
        levelno=logging.ERROR,
        message="Request is 'running', but failed to update it, this is unexpected.",
    )

    # state is canceled, that should be handled gracefully
    module._tf_api.get_request = MagicMock(return_value={'state': 'canceled'})
    request.update(
        state='error',
    )
    assert log.match(
        levelno=logging.INFO,
        message="Request is 'canceled', refusing to update it."
    )

    # state is cancel-requested, that should be handled gracefully with a pipeline cancellation
    module.cancel_pipeline = MagicMock()
    module._tf_api.get_request = MagicMock(return_value={'state': 'cancel-requested'})
    request.update(
        state='error',
        destroying=True
    )
    module.cancel_pipeline.assert_called_once_with(destroying=True)


def test_webhook(module, requests_mock, request2):
    module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    request = module._tf_request
    request.webhook_url = 'someurl'
    request.webhook_token = 'sometoken'
    request.webhook()


def test_webhook_http_error(module, requests_mock, request2, log):
    RequestsMock.post = RequestsMock.request_http_error
    module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    request = module._tf_request
    request.webhook_url = 'someurl'
    request.webhook_token = 'sometoken'
    request.webhook()
    assert log.match(
        levelno=logging.WARNING,
        message="failed to post to webhook: Condition 'posting update to webhook someurl' failed to pass within given time"
    )


# TestingFarmRequestModule class tests
def test_eval_context_request1(module, monkeypatch, request1):
    assert module.eval_context == {
        'TESTING_FARM_REQUEST_ID': '1',
        'TESTING_FARM_REQUEST_TEST_TYPE': 'fmf',
        'TESTING_FARM_REQUEST_TEST_URL': 'testurl',
        'TESTING_FARM_REQUEST_TEST_REF': 'testref',
        # TODO: revert once TFT-2433 done
        # 'TESTING_FARM_REQUEST_USERNAME': 'testuser',
        'TESTING_FARM_REQUEST_MERGE': None,
        'TESTING_FARM_FAILED_IF_PROVISION_ERROR': True,
        'TESTING_FARM_PARALLEL_LIMIT': 123,
    }


def test_eval_context_request3(module, monkeypatch, request3):
    assert module.eval_context == {
        'TESTING_FARM_REQUEST_ID': '3',
        'TESTING_FARM_REQUEST_TEST_TYPE': 'sti',
        'TESTING_FARM_REQUEST_TEST_URL': 'https://username:secret@gitlab.com/namespace/repo',
        'TESTING_FARM_REQUEST_TEST_REF': 'sha',
        # TODO: revert once TFT-2433 done
        # 'TESTING_FARM_REQUEST_USERNAME': 'testuser',
        'TESTING_FARM_REQUEST_MERGE': 'testref',
        'TESTING_FARM_FAILED_IF_PROVISION_ERROR': False,
        'TESTING_FARM_PARALLEL_LIMIT': None,
    }


def test_eval_context_empty(module):
    assert module.eval_context == {}


def test_testing_farm_request(module, monkeypatch, request1):
    request = module.testing_farm_request()
    assert isinstance(request, gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmRequest)
    assert request.type == 'fmf'
    with (request.url == 'testurl').dangerous_reveal() as test:
        assert test
    assert request.ref == 'testref'


def test_testing_farm_request_empty(module):
    request = module.testing_farm_request()
    assert request is None


def test_execute_request1(module, monkeypatch):
    module._config.update({'request-id': '1'})

    patch_shared(monkeypatch, module, {}, callables={'add_secrets': MagicMock(return_value=None)})

    module.execute()
    request = module.testing_farm_request()

    assert request.type == 'fmf'
    with (request.tmt.url == 'testurl').dangerous_reveal() as test:
        assert test
    assert request.tmt.ref == 'testref'
    assert request.webhook_url == 'webhookurl'
    assert request.webhook_token == None
    assert len(request.environments_requested) == 2

    assert request.environments_requested[0].arch == 'x86_64'
    assert request.environments_requested[0].tmt == {'context': {'some': 'context'}}
    assert request.environments_requested[0].secrets == {'some': 'secrets'}
    assert request.environments_requested[0].variables['something'] == 'variables'

    assert request.environments_requested[1].arch == 's390'
    assert request.environments_requested[1].compose == 'Fedora-37'
    assert request.environments_requested[1].pool == 'some-pool'
    assert request.environments_requested[1].variables['foo'] == 'bar'
    assert request.environments_requested[1].secrets == {'secret_key': 'secret-value'}
    assert len(request.environments_requested[1].artifacts) == 2
    assert request.environments_requested[1].hardware == {'cpu': {'model_name': 'AMD'}}
    assert request.environments_requested[1].kickstart == {
        'kernel-options': 'some-kernel-options',
        'kernel-options-post': 'some-kernel-options-post',
        'metadata': 'some-metadata',
        'post-install': 'some-post-install',
        'pre-install': 'some-pre-install',
        'script': 'some-script'
    }
    assert request.environments_requested[1].settings == {
        'pipeline': {'skip_guest_setup': True},
        'provisioning': {'post_install_skip': 'foo'}
    }
    assert request.environments_requested[1].tmt == {
        'context': {'some': 'context'},
        'environment': {'foo': 'foo-value', 'bar': 'bar-value'}
    }


def test_execute_log_request1(module, monkeypatch, log):
    module._config.update({'request-id': '1'})

    patch_shared(monkeypatch, module, {}, callables={'add_secrets': MagicMock(return_value=None)})

    module.execute()

    with open(os.path.join(ASSETS_DIR, 'request1-log.log'), 'r') as request1_log_file:
        request1_log = ''.join(request1_log_file.readlines())

    assert log.records[-1].message == request1_log


def test_execute_request2(module):
    module._config.update({'request-id': '2'})
    module.execute()
    request = module.testing_farm_request()

    assert request.type == 'fmf'
    with (request.tmt.url == 'faketesturl').dangerous_reveal() as test:
        assert test
    assert request.tmt.ref == 'faketestref'
    assert request.webhook_url == None
    assert request.webhook_token == None
    with request.environments_requested[0].variables['TESTING_FARM_GIT_URL'].dangerous_reveal() as url:
        request.environments_requested[0].variables['TESTING_FARM_GIT_URL'] = url
    assert request.environments_requested == [
        TestingEnvironment(
            arch='x86_64',
            artifacts=[],
            snapshots=False,
            variables={
                'TESTING_FARM_REQUEST_ID': '2',
                'TESTING_FARM_TEST_TYPE': 'fmf',
                'TESTING_FARM_GIT_URL': 'faketesturl',
                'TESTING_FARM_GIT_REF': 'faketestref'
            }
        )
    ]


def test_execute_request3(module, monkeypatch):
    module._config.update({'request-id': '3', 'arch': 'forced-arch'})

    add_secrets = MagicMock(return_value=None)

    patch_shared(monkeypatch, module, {}, callables={'add_secrets': add_secrets})
    module.execute()
    request = module.testing_farm_request()

    assert request.type == 'sti'
    with (request.sti.url == 'https://username:secret@gitlab.com/namespace/repo').dangerous_reveal() as test:
        assert test
    assert request.sti.playbooks == ['playbook1', 'playbook2']
    assert request.webhook_token == None
    assert len(request.environments_requested) == 1
    with request.environments_requested[0].variables['TESTING_FARM_GIT_URL'].dangerous_reveal() as url:
        request.environments_requested[0].variables['TESTING_FARM_GIT_URL'] = url
    assert request.environments_requested[0] == TestingEnvironment(
        arch='forced-arch',
        tmt={'context': {'some': 'context'}},
        secrets={'some': 'secrets'},
        variables={
            "something": "variables",
            "TESTING_FARM_REQUEST_ID": "3",
            "TESTING_FARM_TEST_TYPE": "sti",
            "TESTING_FARM_GIT_URL": "https://username:secret@gitlab.com/namespace/repo",
            "TESTING_FARM_GIT_REF": "testref"
        },
        compose=None,
        artifacts=[],
        hardware=None,
        pool=None,
        settings=None
    )
    add_secrets.assert_any_call('username:secret')
    add_secrets.assert_any_call(['secrets'])
    assert add_secrets.call_count == 2


def test_api_url_option(module, monkeypatch):
    module._config['api-url'] = '{{ some_api_url_template }}'
    patch_shared(monkeypatch, module, {'eval_context': {'some_api_url_template': 'foo'}})
    assert module.api_url == 'foo'


def test_api_key_option(module, monkeypatch):
    module._config['api-key'] = '{{ some_api_key_template }}'
    patch_shared(monkeypatch, module, {'eval_context': {'some_api_key_template': 'foo'}})
    assert module.api_key == 'foo'


def test_pipeline_cancellation(module, request2, monkeypatch, log):
    module._config['enable-pipeline-cancellation'] = True
    module._config['pipeline-cancellation-tick'] = 0.1

    process_mock = MagicMock()
    monkeypatch.setattr(psutil, 'Process', process_mock)

    # pipeline cancellation is started in execute
    module.execute()
    assert log.records[-1].message == 'Starting pipeline cancellation, check every 0.1 seconds'

    # make sure the timer runs
    time.sleep(0.5)

    assert process_mock.called_once()
    assert PUT_REQUESTS['2']['state'] == 'canceled'
    assert log.records[-3].message == 'Cancelling pipeline as requested'
    assert log.records[-2].message == 'Stopping pipeline cancellation check'
    assert log.records[-1].message == 'No webhook, skipping'


def test_pipeline_cancellation_destroy(module, request1, monkeypatch, log):
    module._config['enable-pipeline-cancellation'] = True
    module._config['pipeline-cancellation-tick'] = 0.1

    process_mock = MagicMock()
    monkeypatch.setattr(psutil, 'Process', process_mock)

    # pipeline cancellation is started in execute
    module.execute()
    assert log.records[-1].message == 'Starting pipeline cancellation, check every 0.1 seconds'
    module.destroy()

    assert process_mock.called_once()
    assert log.records[-1].message == 'Stopping pipeline cancellation check'
