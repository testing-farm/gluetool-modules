# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import gluetool_modules_framework.testing_farm.testing_farm_request
import os
import gluetool
import contextlib

from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from . import create_module
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


class ResponseMock():
    status_code = 200
    text = 'hello'

    def json(self):
        return REQUESTS['fakekey']['1']


class Response404(ResponseMock):
    status_code = 404


class ResponseInvalidJSON(ResponseMock):
    def json(self):
        raise ValueError


class ResponseInvalidBool(ResponseMock):
    def __nonzero__(self):
        return False


class RequestsMock():
    def get(self, url, json):
        return ResponseMock()

    def put(self, url, json):
        return ResponseMock()

    def post(self, url, json):
        return ResponseMock()

    def request_404(self, url, json):
        return Response404()

    def request_invalid_json(self, url, json):
        return ResponseInvalidJSON()

    def request_invalid_bool(self, url, json):
        return ResponseInvalidBool()

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
def fixture_request1(module):
    module._config.update({'request-id': '1'})
    module._tf_api = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmAPI(
        module, module.option('api-url')
    )
    module._tf_request = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmRequest(module)


@pytest.fixture(name='request2')
def fixture_request2(module):
    module._config.update({'request-id': '2'})
    module._tf_api = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmAPI(
        module, module.option('api-url')
    )
    module._tf_request = gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmRequest(module)


# TestingFarmAPI class tests
def test_request_type_error(module_api):
    try:
        module_api._request('', None, None)
    except gluetool.GlueError as e:
        assert str(e) == 'No request type specified'

    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    try:
        module_api._request('', type='sometype')
    except gluetool.GlueError as e:
        assert str(e) == "Invalid request type 'sometype'"


def test_get_request(module_api):
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    module_api.get_request('1', 'fakekey')


def test_get_request_404(module_api):
    RequestsMock.get = RequestsMock.request_404
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    try:
        module_api.get_request('1', 'fakekey')
    except gluetool.GlueError as e:
        assert str(e) == "Request '1' was not found"


def test_get_request_no_response(module_api):
    RequestsMock.get = RequestsMock.request_invalid_bool
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    try:
        module_api.get_request('1', 'fakekey')
    except gluetool.GlueError as e:
        assert str(e) == ("Condition 'getting get response from dummy-module/v0.1/requests/1?api_key=fakekey' "
                          "failed to pass within given time")


def test_get_request_invalid_json(module_api):
    RequestsMock.get = RequestsMock.request_invalid_json
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    try:
        module_api.get_request('1', 'fakekey')
    except ValueError:
        pass


def test_get_request_http_error(module_api):
    RequestsMock.get = RequestsMock.request_http_error
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    try:
        module_api.get_request('1', 'fakekey')
    except gluetool.GlueError as e:
        assert str(e) == ("Condition 'getting get response from dummy-module/v0.1/requests/1?api_key=fakekey' "
                          "failed to pass within given time")


def test_put_request_error(module_api):
    try:
        module_api.put_request('', None)
    except gluetool.GlueError as e:
        assert str(e) == "payload is required for 'post' and 'put' requests"


def test_put_request(module_api):
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    module_api.put_request('1', {'hello': 'world'})


def test_put_request_404(module_api):
    RequestsMock.put = RequestsMock.request_404
    module_api._module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    try:
        module_api.put_request('1', {'hello': 'world'})
    except gluetool.GlueError as e:
        assert str(e) == 'Request failed: None'


# TestingFarmRequest class tests
def test_update_empty(module, request2):
    request = module._tf_request
    request.update()
    assert PUT_REQUESTS['2'] == {'api_key': 'fakekey'}


def test_update(module, request2):
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
        },
        'run': {
            'artifacts': 'someurl',
        }
    }


def test_webhook(module, requests_mock, request2):
    module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    request = module._tf_request
    request.webhook_url = 'someurl'
    request.webhook_token = 'sometoken'
    request.webhook()


def test_webhook_invalid_bool(module, requests_mock, request2):
    RequestsMock.post = RequestsMock.request_invalid_bool
    module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    request = module._tf_request
    request.webhook_url = 'someurl'
    request.webhook_token = 'sometoken'
    try:
        request.webhook()
    except gluetool.GlueError as e:
        assert str(e) == "Condition 'posting update to webhook someurl' failed to pass within given time"


def test_webhook_http_error(module, requests_mock, request2):
    RequestsMock.post = RequestsMock.request_http_error
    module._config.update({'retry-timeout': 1, 'retry-tick': 1})
    request = module._tf_request
    request.webhook_url = 'someurl'
    request.webhook_token = 'sometoken'
    try:
        request.webhook()
    except gluetool.GlueError as e:
        assert str(e) == "Condition 'posting update to webhook someurl' failed to pass within given time"


# TestingFarmRequestModule class tests
def test_eval_context(module, request1):
    assert module.eval_context == {
        'TESTING_FARM_REQUEST_ID': '1',
        'TESTING_FARM_REQUEST_TEST_TYPE': 'fmf',
        'TESTING_FARM_REQUEST_TEST_URL': 'testurl',
        'TESTING_FARM_REQUEST_TEST_REF': 'testref',
    }


def test_eval_context_empty(module):
    try:
        module.eval_context
    except AssertionError:
        pass


def test_testing_farm_request(module, request1):
    request = module.testing_farm_request()
    assert isinstance(request, gluetool_modules_framework.testing_farm.testing_farm_request.TestingFarmRequest)
    assert request.type == 'fmf'
    assert request.url == 'testurl'
    assert request.ref == 'testref'


def test_testing_farm_request_empty(module):
    request = module.testing_farm_request()
    assert request is None


def test_execute_request1(module):
    module._config.update({'request-id': '1'})
    module.execute()
    request = module.testing_farm_request()

    assert request.type == 'fmf'
    assert request.tmt.url == 'testurl'
    assert request.tmt.ref == 'testref'
    assert request.webhook_url == 'webhookurl'
    assert request.webhook_token == None
    assert len(request.environments_requested) == 2
    assert request.environments_requested[0].arch == 'x86_64'
    assert request.environments_requested[1].arch == 's390'
    assert request.environments_requested[1].compose == 'Fedora-37'
    assert request.environments_requested[1].secrets == {'secret_key': 'secret-value'}
    assert len(request.environments_requested[1].artifacts) == 2


def test_execute_request2(module):
    module._config.update({'request-id': '2'})
    module.execute()
    request = module.testing_farm_request()

    assert request.type == 'fmf'
    assert request.tmt.url == 'faketesturl'
    assert request.tmt.ref == 'faketestref'
    assert request.webhook_url == None
    assert request.webhook_token == None
    assert request.environments_requested == []


def test_execute_request3(module):
    module._config.update({'request-id': '3', 'arch': 'forced-arch'})
    module.execute()
    request = module.testing_farm_request()

    assert request.type == 'sti'
    assert request.sti.url == 'testurl'
    assert request.sti.playbooks == ['playbook1', 'playbook2']
    assert request.webhook_token == None
    assert len(request.environments_requested) == 1
    assert request.environments_requested[0] == TestingEnvironment(
        arch='forced-arch',
        tmt={'context': {'some': 'context'}},
        secrets={'some': 'secrets'},
        variables={'something': 'variables'},
        compose=None,
        artifacts=None,
        hardware=None,
        pool=None,
        settings=None
    )
