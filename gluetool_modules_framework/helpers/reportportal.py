# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import datetime
from requests.models import Response

import gluetool
from gluetool.glue import GlueError
from gluetool.result import Result
from gluetool.utils import requests, wait
from gluetool_modules_framework.testing.test_schedule_tmt import TestScheduleEntry

# Type annotations
# pylint: disable=unused-import,wrong-import-order
from typing import Any, Dict, List, Optional, cast  # noqa
from typing_extensions import TypedDict

DEFAULT_RETRY_TIMEOUT = 30
DEFAULT_RETRY_TICK = 10


class ReportPortalAPI:
    """
    Report Portal API client handling authentication and API calls.

    :param str api_url: Base URL of the Report Portal API
    :param str token: Authentication token for Report Portal
    :param str project: Report Portal project name
    """

    def __init__(self, module: 'ReportPortalModule', api_url: str, token: str, project: str):
        self.module = module
        self.api_url = api_url.rstrip('/')
        self.token = token
        self.project = project
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, ReportPortalAPI)
            and self.api_url == other.api_url
            and self.token == other.token
            and self.project == other.project
        )

    def create_launch(self, name: str, description: Optional[str] = None) -> str:
        """
        Create a new launch in Report Portal.

        :param str name: Name of the launch
        :param str description: Optional description of the launch
        :returns: ID of the created launch
        :rtype: str
        """
        response = self._request('POST', f'/{self.project}/launch', {
            'name': name,
            'description': description,
            'startTime': datetime.datetime.utcnow().isoformat()
        })
        return cast(str, response['id'])

    def finish_launch(self, launch_id: str, status: str = 'PASSED') -> None:
        """
        Finish an existing launch.

        :param str launch_id: ID of the launch to finish
        :param str status: Final status of the launch
        """
        self._request('PUT', f'/{self.project}/launch/{launch_id}/finish', {
            'endTime': datetime.datetime.utcnow().isoformat(),
            'status': status
        })

    def _request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Any:
        """
        Make an HTTP request to Report Portal API.

        :param str method: HTTP method
        :param str endpoint: API endpoint
        :param dict data: Request payload
        :returns: Response data
        :rtype: dict
        :raises: requests.exceptions.RequestException
        """
        url = f'{self.api_url}/api/v1{endpoint}'

        def _do_request() -> Result[Response, bool]:
            with requests(logger=self.module.logger) as R:
                try:
                    response = R.request(method, url, json=data, headers=self.headers)
                except Exception as exc:
                    self.module.warn(f'Report Portal request failed: {exc}')
                    return Result.Error(False)
                return Result.Ok(response)

        try:
            response = wait(
                f'Report Portal request to {url}',
                _do_request,
                timeout=self.module.option('retry-timeout'),
                tick=self.module.option('retry-tick')
            )
        except Exception:
            raise GlueError(f'Report Portal request failed: {url}')

        try:
            return response.json()
        except Exception:
            raise GlueError(f'Report Portal response is not valid JSON: {str(response.content)}')


RpApiLaunchMap = TypedDict(
    'RpApiLaunchMap',
    {
        'launch_id': str,
        'api': ReportPortalAPI
    }
)


class ReportPortalModule(gluetool.Module):
    """
    Provides creation and finish launches in Report Portal.
    """

    name = 'reportportal'
    description = 'Allows to create and finish launches in Report Portal.'

    options = {
        'retry-timeout': {
            'help': 'Timeout for retry. (default: %(default)s)',
            'type': int,
            'default': DEFAULT_RETRY_TIMEOUT,
            },
        'retry-tick': {
            'help': 'Tick for retry. (default: %(default)s)',
            'type': int,
            'default': DEFAULT_RETRY_TICK,
        }
    }

    shared_functions = ['check_create_rp_launch']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(ReportPortalModule, self).__init__(*args, **kwargs)

        self.rp_api_launch_map: List[RpApiLaunchMap] = []

        self.required_vars = (
            'TMT_PLUGIN_REPORT_REPORTPORTAL_URL',
            'TMT_PLUGIN_REPORT_REPORTPORTAL_TOKEN',
            'TMT_PLUGIN_REPORT_REPORTPORTAL_PROJECT',
        )

    def _should_have_rp_launch(self, schedule_entry: TestScheduleEntry) -> bool:
        """
        Check if Report Portal launch should be created based on environment configuration.

        Returns True if all required Report Portal settings are present and suite-per-plan=1.
        """

        assert schedule_entry.testing_environment is not None
        assert schedule_entry.testing_environment.tmt is not None
        assert schedule_entry.testing_environment.tmt['environment'] is not None

        tmt_environment = schedule_entry.testing_environment.tmt['environment']

        # If no variables are defined, user don't want to use Report Portal.
        if not any(var in tmt_environment for var in self.required_vars):
            return False

        if tmt_environment.get('TMT_PLUGIN_REPORT_REPORTPORTAL_UPLOAD_TO_LAUNCH'):
            self.info('Report Portal launch is defined, a launch will not be created.')
            return False

        # Check if all required variables are defined.
        if not all(var in tmt_environment for var in self.required_vars):
            self.warn(
                f'Not all required Report Portal variables are defined. '
                f'Required variables: {", ".join(self.required_vars)}.'
            )
            return False

        suite_per_plan = tmt_environment.get('TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN')

        # Check if suite per plan is enabled.
        if suite_per_plan is None:
            self.info('TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN is not defined. Suite per plan is disabled.')
            return False

        if suite_per_plan != '1':
            self.warn(
                f'TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN={suite_per_plan} is not supported. Only value "1" enables the feature.'  # noqa: E501
            )
            return False

        self.info('Report Portal suite per plan is enabled')
        return True

    def create_rp_launch(self, schedule_entry: TestScheduleEntry, default_name: str) -> str:
        """
        Create a new launch in Report Portal.
        """

        assert schedule_entry.testing_environment is not None
        assert schedule_entry.testing_environment.tmt is not None
        assert schedule_entry.testing_environment.tmt['environment'] is not None

        tmt_environment = schedule_entry.testing_environment.tmt['environment']

        rp_api = ReportPortalAPI(
            module=self,
            api_url=tmt_environment['TMT_PLUGIN_REPORT_REPORTPORTAL_URL'],
            token=tmt_environment['TMT_PLUGIN_REPORT_REPORTPORTAL_TOKEN'],
            project=tmt_environment['TMT_PLUGIN_REPORT_REPORTPORTAL_PROJECT']
        )

        # Check if launch for the same api was already created, if so, just return existing launch id
        rp_api_launch_entry = next((entry for entry in self.rp_api_launch_map if entry["api"] == rp_api), None)

        if rp_api_launch_entry:
            self.info(f'Reusing existing Report Portal launch {rp_api_launch_entry["launch_id"]}')
            return rp_api_launch_entry['launch_id']

        launch_name = tmt_environment.get('TMT_PLUGIN_REPORT_REPORTPORTAL_LAUNCH')
        if not launch_name:
            launch_name = default_name

        launch_id = rp_api.create_launch(
            name=launch_name,
            description=f'Testing Farm launch for {launch_name}'
        )

        self.rp_api_launch_map.append({
            'launch_id': launch_id,
            'api': rp_api
        })

        self.info(f'Created Report Portal launch {launch_id}')

        return launch_id

    def check_create_rp_launch(self, schedule_entry: TestScheduleEntry) -> Optional[str]:
        """
        Check if Report Portal launch should be created based on environment configuration
        and create them if needed.
        """

        assert schedule_entry.testing_environment is not None
        assert schedule_entry.testing_environment.tmt is not None

        if not self._should_have_rp_launch(schedule_entry):
            return None

        launch_id = self.create_rp_launch(
            schedule_entry, default_name=f'{schedule_entry.plan}@{schedule_entry.testing_environment.compose}')

        return launch_id

    def destroy(self, failure: Optional[gluetool.Failure] = None) -> None:
        """
        Finish all Report Portal launches.
        """
        for rp_entry in self.rp_api_launch_map:
            self.info(f'Finalizing Report Portal launch {rp_entry["launch_id"]}')
            rp_entry['api'].finish_launch(rp_entry['launch_id'])
