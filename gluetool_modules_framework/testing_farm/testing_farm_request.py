# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from functools import partial
from posixpath import join as urljoin
from dataclasses import dataclass

import gluetool
from gluetool.log import LoggerMixin
from gluetool.result import Result
from gluetool.utils import log_dict, requests
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from requests.exceptions import ConnectionError, HTTPError, Timeout

# Type annotations
# pylint: disable=unused-import,wrong-import-order
from typing import Any, Dict, List, Optional, Union, cast  # noqa
from typing_extensions import TypedDict, NotRequired, Literal


# Following classes reflect the structure of what comes out of GET `/request/{request_id}` endpoint in the TF API.
class RequestTestType(TypedDict):
    fmf: Optional[Dict[str, Any]]
    sti: Optional[Dict[str, Any]]


class RequestEnvironmentArtifactType(TypedDict):
    id: str
    type: str
    packages: NotRequired[List[str]]


class RequestEnvironmentTMTType(TypedDict):
    context: NotRequired[Dict[str, str]]


class RequestEnvironmentType(TypedDict):
    arch: str
    os: NotRequired[Dict[str, str]]
    pool: NotRequired[str]
    variables: NotRequired[Dict[str, str]]
    secrets: NotRequired[Dict[str, str]]
    artifacts: NotRequired[List[RequestEnvironmentArtifactType]]
    hardware: NotRequired[Dict[str, Any]]
    settings: NotRequired[Dict[str, Any]]
    tmt: NotRequired[RequestEnvironmentTMTType]


class RequestWebhookType(TypedDict):
    url: str
    token: NotRequired[str]


class RequestNotificationType(TypedDict):
    webhook: NotRequired[RequestWebhookType]


class RequestType(TypedDict):
    test: RequestTestType
    environments_requested: NotRequired[List[RequestEnvironmentType]]
    notification: NotRequired[RequestNotificationType]
    settings: NotRequired[Dict[str, Any]]


class TestingFarmAPI(LoggerMixin, object):
    def __init__(self, module, api_url):
        # type: (gluetool.Module, str) -> None
        super(TestingFarmAPI, self).__init__(module.logger)

        self._module = module
        self._api_url = api_url
        self._post_request = partial(self._request, type='post')
        self._put_request = partial(self._request, type='put')
        self._get_request = partial(self._request, type='get')
        self._delete_request = partial(self._request, type='delete')

    def _request(self, endpoint, payload=None, type=None):
        # type: (str, Optional[Dict[str, Any]], Optional[str]) -> Any
        """
        Post payload to the given API endpoint. Retry if failed to mitigate connection/service
        instabilities.
        """

        if not type:
            raise gluetool.GlueError('No request type specified')

        if type in ['post', 'put'] and not payload:
            raise gluetool.GlueError("payload is required for 'post' and 'put' requests")

        # construct post URL
        url = urljoin(self._api_url, endpoint)  # type: ignore
        log_dict(self.debug, "posting following payload to url '{}'".format(url), payload)

        def _response():
            # type: () -> Any
            assert type is not None
            try:
                with requests() as req:
                    response = getattr(req, type)(url, json=payload)

                try:
                    response_data = response.json()
                except ValueError:
                    response_data = response.text

                if response.status_code == 404:
                    return Result.Ok(None)

                if not response:
                    error_msg = 'Got unexpected response status code {}'.format(response.status_code)
                    log_dict(
                        self.error,
                        error_msg,
                        {
                            'post-url': url,
                            'payload': payload or '<not available>',
                            'response': response_data
                        }
                    )
                    return Result.Error(error_msg)

                return Result.Ok(response)

            except (ConnectionError, HTTPError, Timeout) as error:
                self.debug('retrying because of exception: {}'.format(error))
                return Result.Error(error)

            except AttributeError:
                raise gluetool.GlueError("Invalid request type '{}'".format(type))

        # wait until we get a valid response
        return gluetool.utils.wait('getting {} response from {}'.format(type, url),
                                   _response,
                                   timeout=self._module.option('retry-timeout'),
                                   tick=self._module.option('retry-tick'))

    def get_request(self, request_id, api_key):
        # type: (str, str) -> RequestType
        request = self._get_request('v0.1/requests/{}?api_key={}'.format(request_id, api_key))

        if not request:
            raise gluetool.GlueError("Request '{}' was not found".format(request_id))

        return cast(RequestType, request.json())

    def put_request(self, request_id, payload):
        # type: (str, Optional[Dict[str, Any]]) -> Any
        request = self._put_request('v0.1/requests/{}'.format(request_id), payload=payload)
        if not request:
            raise gluetool.GlueError("Request failed: {}".format(request))

        return request.json()


@dataclass
class TestingFarmRequestTMT():
    url: str
    ref: str
    merge_sha: Optional[str] = None
    path: Optional[str] = None
    name: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None

    @property
    def plan(self):
        # type: () -> Optional[str]
        # In the TF API v0.1, the field `name` represents which TMT plans shall be run. This field will be renamed to
        # `plan` in API v0.2. This provides a property to allow working with the future name.
        return self.name


@dataclass
class TestingFarmRequestSTI():
    url: str
    ref: str
    merge_sha: Optional[str] = None
    playbooks: Optional[List[str]] = None
    extra_variables: Optional[Dict[str, str]] = None


class TestingFarmRequest(LoggerMixin, object):
    ARTIFACT_NAMESPACE = 'testing-farm-request'

    def __init__(self, module):
        # type: (TestingFarmRequestModule) -> None
        super(TestingFarmRequest, self).__init__(module.logger)

        assert module._tf_api is not None

        self._module = module
        self._api_key = module.option('api-key')
        self._api = module._tf_api

        self.id = cast(str, self._module.option('request-id'))

        request = self._api.get_request(self.id, self._api_key)

        # Select correct test, trust Testing Farm validation that only one test
        # is specified, as defined in the API standard.
        for type in cast(List[Literal['fmf', 'sti']], list(request['test'].keys())):
            if request['test'][type]:
                self.type = type
                break
        else:
            raise gluetool.GlueError('Received malformed request from the Testing Farm API. It does not contain any '
                                     'test type under `test` key.')

        if type not in ['fmf', 'sti']:
            raise gluetool.GlueError('Received malformed request from the Testing Farm API. Its type is `{}`, '
                                     'it should be either `fmf` or `sti`.'.format(type))
        request_test = request['test'][type]
        assert request_test is not None

        # In the TF API v0.1, one of the types is called `test.fmf`, the name is expected to change to `test.tmt`
        # in v0.2, therefore this class and variable are named as `TMT`.
        self.tmt = TestingFarmRequestTMT(**request_test) if type == 'fmf' else None
        self.sti = TestingFarmRequestSTI(**request_test) if type == 'sti' else None

        # Create a shortcut for the common TMT/STI properties
        test = (self.tmt or self.sti)
        assert test
        self.url = test.url
        self.ref = test.ref

        environments_requested = []  # type: List[TestingEnvironment]
        for environment_raw in request['environments_requested']:
            environments_requested.append(TestingEnvironment(
                arch=environment_raw['arch'],
                compose=(environment_raw.get('os') or {}).get('compose'),
                pool=environment_raw.get('pool'),
                variables=environment_raw.get('variables'),
                secrets=environment_raw.get('secrets'),
                artifacts=cast(Optional[List[Dict[str, Any]]], environment_raw.get('artifacts')),
                hardware=environment_raw.get('hardware'),
                settings=environment_raw.get('settings'),
                tmt=cast(Dict[str, Any], environment_raw.get('tmt'))
            ))

        self.environments_requested = environments_requested

        self.webhook_url = None
        self.webhook_token = None

        try:
            self.webhook_url = request['notification']['webhook']['url'] or None
        except (KeyError, TypeError):
            pass

        try:
            self.webhook_token = request['notification']['webhook']['token'] or None
        except (KeyError, TypeError):
            pass

    def webhook(self):
        # type: () -> Any
        """
        Post to webhook, as defined in the API.
        """

        if not self.webhook_url:
            self.debug('No webhook, skipping')
            return

        payload = {'request_id': self.id}

        if self.webhook_token:
            payload.update({'token': self.webhook_token})

        def _response():
            # type: () -> Any
            try:
                with requests() as req:
                    response = req.post(self.webhook_url, json=payload)

                if not response:
                    return Result.Error('retrying because of status code {}'.format(response.status_code))

                return Result.Ok(None)

            except (ConnectionError, HTTPError, Timeout) as error:
                self.debug('retrying because of exception: {}'.format(error))
                return Result.Error(error)

        # wait until we get a valid response
        return gluetool.utils.wait('posting update to webhook {}'.format(self.webhook_url),
                                   _response,
                                   timeout=self._module.option('retry-timeout'),
                                   tick=self._module.option('retry-tick'))

    def update(self, state=None, overall_result=None, xunit=None, summary=None, artifacts_url=None):
        # type: (Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]) -> Any
        payload = {}
        result = {}
        run = {}

        if self._api_key:
            payload.update({
                'api_key': self._api_key
            })

        if state:
            payload.update({
                'state': state
            })

        if overall_result:
            result.update({
                'overall': overall_result
            })

        if xunit:
            result.update({
                'xunit': xunit
            })

        if summary:
            result.update({
                'summary': summary
            })

        if artifacts_url:
            run.update({
                'artifacts': artifacts_url
            })

        if result:
            payload.update({
                'result': result
            })

        if run:
            payload.update({
                'run': run
            })

        self._api.put_request(self.id, payload)

        self.webhook()


class TestingFarmRequestModule(gluetool.Module):
    """
    Provides testing farm request.
    """

    name = 'testing-farm-request'
    description = "Module providing Testing Farm Request."
    supported_dryrun_level = gluetool.glue.DryRunLevels.DRY

    options = [
        ('API options', {
            'api-key': {
                'help': 'API key required for authentication',
            },
            'api-url': {
                'help': 'Root of Nucleus internal API endpoint',
            },
            'retry-timeout': {
                'help': 'Wait timeout in seconds. (default: %(default)s)',
                'type': int,
                'default': 30
            },
            'retry-tick': {
                'help': 'Number of times to retry a query. (default: %(default)s)',
                'type': int,
                'default': 10
            },
        }),
        ('Testing Farm Request', {
            'request-id': {
                'help': 'Testing Farm request ID to report against.'
            },
            'arch': {
                'help': 'Force given architecture in all environments.'
            }
        }),
    ]

    required_options = ('api-url', 'api-key', 'request-id')
    shared_functions = ['testing_farm_request']

    def __init__(self, *args, **kwargs):
        # type: (*Any, **Any) -> None
        super(TestingFarmRequestModule, self).__init__(*args, **kwargs)
        self._tf_request = None  # type: Optional[TestingFarmRequest]
        self._tf_api = None  # type: Optional[TestingFarmAPI]

    @property
    def eval_context(self):
        # type: () -> Dict[str, str]
        assert self._tf_request is not None
        return {
            # common for all artifact providers
            'TESTING_FARM_REQUEST_ID': self._tf_request.id,
            'TESTING_FARM_REQUEST_TEST_TYPE': self._tf_request.type,
            'TESTING_FARM_REQUEST_TEST_URL': self._tf_request.url,
            'TESTING_FARM_REQUEST_TEST_REF': self._tf_request.ref,
        }

    def testing_farm_request(self):
        # type: () -> Optional[TestingFarmRequest]
        return self._tf_request

    def execute(self):
        # type: () -> None
        self._tf_api = TestingFarmAPI(self, self.option('api-url'))

        self.info(
            "Connected to Testing Farm Service '{}'".format(
                self.option('api-url'),
            )
        )

        self._tf_request = request = TestingFarmRequest(self)

        if self.option('arch'):
            for environment in request.environments_requested:
                environment.arch = self.option('arch')

        log_dict(self.info, "Initialized with {}".format(request.id), {
            'type': request.type,
            'plan': request.tmt.plan if request.tmt and request.tmt.plan else '<not applicable>',
            'url': request.url,
            'ref': request.ref,
            'environments_requested': request.environments_requested,
            'webhook_url': request.webhook_url or '<no webhook specified>',
        })
