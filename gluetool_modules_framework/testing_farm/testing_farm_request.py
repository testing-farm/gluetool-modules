# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from functools import partial
from posixpath import join as urljoin
from dataclasses import dataclass, fields
import re

import gluetool
from gluetool.log import LoggerMixin
from gluetool.result import Result
from gluetool.utils import dict_update, log_dict, requests, render_template
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.git import GIT_URL_REGEX

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
    user_id: str


class UserType(TypedDict):
    id: str
    name: str


class TestingFarmAPI(LoggerMixin, object):
    def __init__(self, module: gluetool.Module, api_url: str) -> None:
        super(TestingFarmAPI, self).__init__(module.logger)

        self._module = module
        self._api_url = api_url
        self._post_request = partial(self._request, type='post')
        self._put_request = partial(self._request, type='put')
        self._get_request = partial(self._request, type='get')
        self._delete_request = partial(self._request, type='delete')

    def _request(self, endpoint: str, payload: Optional[Dict[str, Any]] = None, type: Optional[str] = None) -> Any:
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

        def _response() -> Any:
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

    def get_request(self, request_id: str, api_key: str) -> RequestType:
        request = self._get_request('v0.1/requests/{}?api_key={}'.format(request_id, api_key))

        if not request:
            raise gluetool.GlueError("Request '{}' was not found".format(request_id))

        return cast(RequestType, request.json())

    def get_user(self, user_id: str, api_key: str) -> UserType:
        request = self._get_request('v0.1/users/{}?api_key={}'.format(user_id, api_key))

        if not request:
            raise gluetool.GlueError("Request '{}' was not found".format(user_id))

        return cast(UserType, request.json())

    def put_request(self, request_id: str, payload: Optional[Dict[str, Any]]) -> Any:
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
    plan_filter: Optional[str] = None
    test_filter: Optional[str] = None

    @property
    def plan(self) -> Optional[str]:
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

    def __init__(self, module: 'TestingFarmRequestModule') -> None:
        super(TestingFarmRequest, self).__init__(module.logger)

        assert module._tf_api is not None

        self._module = module
        self._api_key = module.api_key
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
        self.tmt = TestingFarmRequestTMT(**{field.name: request_test[field.name]
                                            for field in fields(TestingFarmRequestTMT)
                                            if field.name in request_test}) if type == 'fmf' else None
        self.sti = TestingFarmRequestSTI(**{field.name: request_test[field.name]
                                            for field in fields(TestingFarmRequestSTI)
                                            if field.name in request_test}) if type == 'sti' else None

        # Create a shortcut for the common TMT/STI properties
        test = (self.tmt or self.sti)
        assert test
        self.url = test.url

        # Check whether the git url contains any secrets, if so, store them in hide-secrets module
        match = re.match(GIT_URL_REGEX, self.url)
        if match and self._module.has_shared('add_additional_secrets'):
            self._module.shared('add_additional_secrets', match.group(2))

        # In the context of this class, `self.ref` is a git reference which will be checked out by the
        # RemoteGitRepository library class, `self.merge` is a git reference to be merged into `self.ref`.
        #
        # The TF API contains two fields with similar names but require different handling:
        # `ref` - the git reference to be tested,
        # `merge_sha` - the target git referene to which `ref` should be merged into.
        #
        # If just `ref` is specified in the TF API, it can just be checked out and nothing more has to be done.
        # If both `ref` and `merge_sha` are specified, `merge_sha` has to be checked out and `ref` will then be merged
        # into `merge_sha`.
        if test.merge_sha is None:
            self.ref = test.ref
            self.merge = test.merge_sha
        else:
            self.ref = test.merge_sha
            self.merge = test.ref

        # Additional environment variables are set at the provisioned system
        testing_farm_env_vars = {
            "TESTING_FARM_REQUEST_ID": self.id,
            "TESTING_FARM_TEST_TYPE": self.type,
            "TESTING_FARM_GIT_URL": test.url,
            "TESTING_FARM_GIT_REF": test.ref
        }

        environments_requested: List[TestingEnvironment] = []
        for environment_raw in request['environments_requested']:
            environments_requested.append(TestingEnvironment(
                arch=environment_raw['arch'],
                compose=(environment_raw.get('os') or {}).get('compose'),
                pool=environment_raw.get('pool'),
                variables=dict_update(environment_raw.get('variables') or {}, testing_farm_env_vars),
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

        user = self._api.get_user(request['user_id'], self._api_key)
        self.request_username = user['name']

    def webhook(self) -> Any:
        """
        Post to webhook, as defined in the API.
        """

        if not self.webhook_url:
            self.debug('No webhook, skipping')
            return

        payload = {'request_id': self.id}

        if self.webhook_token:
            payload.update({'token': self.webhook_token})

        def _response() -> Any:
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

    def update(self,
               state: Optional[str] = None,
               overall_result: Optional[str] = None,
               xunit: Optional[str] = None,
               summary: Optional[str] = None,
               artifacts_url: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {}
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
                'help': '''
                        API key required for authentication. Accepts also Jinja templates which will be rendered using
                        `eval_context` shared method.
                        ''',
            },
            'api-url': {
                'help': '''
                        Root of Nucleus internal API endpoint. Accepts also Jinja templates which will be rendered using
                        `eval_context` shared method.
                        ''',
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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(TestingFarmRequestModule, self).__init__(*args, **kwargs)
        self._tf_request: Optional[TestingFarmRequest] = None
        self._tf_api: Optional[TestingFarmAPI] = None

    @property
    def api_url(self) -> str:
        option = self.option('api-url')

        return render_template(option, **self.shared('eval_context'))

    @property
    def api_key(self) -> str:
        option = self.option('api-key')

        return render_template(option, **self.shared('eval_context'))

    @property
    def eval_context(self) -> Dict[str, Optional[str]]:
        if not self._tf_request:
            return {}
        return {
            # common for all artifact providers
            'TESTING_FARM_REQUEST_ID': self._tf_request.id,
            'TESTING_FARM_REQUEST_TEST_TYPE': self._tf_request.type,
            'TESTING_FARM_REQUEST_TEST_URL': self._tf_request.url,
            'TESTING_FARM_REQUEST_TEST_REF': self._tf_request.ref,
            'TESTING_FARM_REQUEST_USERNAME': self._tf_request.request_username,
            'TESTING_FARM_REQUEST_MERGE': self._tf_request.merge
        }

    def testing_farm_request(self) -> Optional[TestingFarmRequest]:
        return self._tf_request

    def execute(self) -> None:
        self._tf_api = TestingFarmAPI(self, self.api_url)

        self.info(
            "Connected to Testing Farm Service '{}'".format(
                self.api_url,
            )
        )

        self._tf_request = request = TestingFarmRequest(self)

        if self.option('arch'):
            for environment in request.environments_requested:
                environment.arch = self.option('arch')

        log_dict(self.info, "Initialized with {}".format(request.id), {
            'type': request.type,
            'plan': request.tmt.plan if request.tmt and request.tmt.plan else '<not applicable>',
            'plan_filter': request.tmt.plan_filter if request.tmt and request.tmt.plan_filter else '<not applicable>',
            'test_filter': request.tmt.test_filter if request.tmt and request.tmt.test_filter else '<not applicable>',
            'url': request.url,
            'ref': request.ref,
            'environments_requested': [env.serialize_to_json() for env in request.environments_requested],
            'webhook_url': request.webhook_url or '<no webhook specified>',
        })
