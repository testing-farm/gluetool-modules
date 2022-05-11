# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import six

from functools import partial
from posixpath import join as urljoin

import gluetool
from gluetool.log import LoggerMixin
from gluetool.result import Result
from gluetool.utils import log_dict, requests

from requests.exceptions import ConnectionError, HTTPError, Timeout

# Type annotations
# pylint: disable=unused-import,wrong-import-order
from typing import Any, Dict, List, Optional, Union, cast  # noqa
from typing_extensions import TypedDict

RequestTestFMFType = TypedDict(
    'RequestTestFMFType',
    {
        'url': str,
        'ref': str,
        'name': str,
    }
)

RequestTestSTIType = TypedDict(
    'RequestTestSTIType',
    {
        'url': str,
        'ref': str,
        'playbooks': Any,
    }
)

RequestTestType = TypedDict(
    'RequestTestType',
    {
        'fmf': RequestTestFMFType,
        'sti': RequestTestSTIType,
    }
)

RequestEnvironmentTMTType = TypedDict(
    'RequestEnvironmentTMTType',
    {
        'context': Dict[str, str],
    }
)

RequestEnvironmentType = TypedDict(
    'RequestEnvironmentType',
    {
        'arch': Dict[str, str],
        'variables': Dict[str, str],
        'secrets': Dict[str, str],
        'tmt': RequestEnvironmentTMTType,
        'settings': Dict[Any, Any],
    },
    total=False
)

RequestWebhookType = TypedDict(
    'RequestWebhookType',
    {
        'url': str,
        'token': str,
    }
)

RequestNotificationType = TypedDict(
    'RequestNotificationType',
    {
        'webhook': RequestWebhookType,
    }
)

RequestType = TypedDict(
    'RequestType',
    {
        'test': RequestTestType,
        'environments_requested': List[RequestEnvironmentType],
        'notification': RequestNotificationType,
    }
)


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
        # is specified, as defined in the API standard
        test = request['test']
        type = self.type = [key for key in test.keys() if test[key]][0]  # type: ignore

        test_type = cast(Union[RequestTestFMFType, RequestTestSTIType], test[type])  # type: ignore

        self.url = test_type['url']
        self.ref = test_type['ref']

        self.playbooks = cast(RequestTestSTIType, test_type)['playbooks'] if type == 'sti' else None

        self.plans = cast(RequestTestFMFType, test_type)['name'] if type == 'fmf' else None

        self.environments_requested = request['environments_requested']

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
    # TODO(mvadkert): user_* and tmt_context functions should move to environments later
    shared_functions = ['testing_farm_request', 'user_variables', 'user_secrets', 'tmt_context', 'user_settings']

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

    def user_settings(self):
        # type: () -> Dict[Any, Any]
        request = self.testing_farm_request()
        settings = {}  # type: Dict[Any, Any]

        if request and request.environments_requested \
                and 'settings' in request.environments_requested[0] \
                and request.environments_requested[0]['settings']:

            settings = request.environments_requested[0]['settings']

        return settings

    def user_variables(self, hide_secrets=False, **kwargs):
        # type: (bool, **Any) -> Dict[str, str]
        request = self.testing_farm_request()
        variables = {}

        if request and request.environments_requested \
                and 'variables' in request.environments_requested[0] \
                and request.environments_requested[0]['variables']:

            variables.update({
                key: value or ''
                for key, value in six.iteritems(request.environments_requested[0]['variables'])
            })

        if request and request.environments_requested \
                and 'secrets' in request.environments_requested[0] \
                and request.environments_requested[0]['secrets']:

            variables.update({
                key: '*'*len(value) if hide_secrets else value or ''
                for key, value in six.iteritems(request.environments_requested[0]['secrets'])
            })

        return variables

    def user_secrets(self):
        # type: () -> Dict[str, str]
        request = self.testing_farm_request()
        secrets = {}  # type: Dict[str, str]

        if request and request.environments_requested \
                and 'secrets' in request.environments_requested[0] \
                and request.environments_requested[0]['secrets']:

            return request.environments_requested[0]['secrets']

        return secrets

    def tmt_context(self):
        # type: () -> Dict[str, str]
        request = self.testing_farm_request()

        if request and request.environments_requested \
                and 'tmt' in request.environments_requested[0] \
                and request.environments_requested[0]['tmt'] \
                and 'context' in request.environments_requested[0]['tmt']:

            return request.environments_requested[0]['tmt']['context']

        return {}

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
                environment['arch'] = self.option('arch')

        if request.type == 'fmf':
            plans = request.plans if request.plans else 'all'
        else:
            plans = '<not applicable>'

        def _hide_secrets(environments):
            # type: (List[RequestEnvironmentType]) -> List[RequestEnvironmentType]
            environments = [environment.copy() if environment else environment for environment in environments]
            for environment in environments:
                if 'secrets' in environment:
                    environment.pop('secrets', None)
            return environments

        log_dict(self.info, "Initialized with {}".format(request.id), {
            'type': request.type,
            'plans': plans,
            'url': request.url,
            'ref': request.ref,
            'variables': self.user_variables(hide_secrets=True) or '<no variables specified>',
            'environments_requested': _hide_secrets(request.environments_requested),
            'webhook_url': request.webhook_url or '<no webhook specified>',
            'settings': self.user_settings() or '<no settings specified>'
        })
