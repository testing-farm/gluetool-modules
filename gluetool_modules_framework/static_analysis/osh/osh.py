# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import re
import tempfile
import json
from six.moves.urllib.request import urlopen, urlretrieve

import gluetool
import gluetool_modules_framework.infrastructure.koji_fedora

from gluetool import GlueError, SoftGlueError
from gluetool.glue import DryRunLevels
from gluetool.log import log_blob, log_dict, format_dict
from gluetool.utils import cached_property, Command, check_for_commands, GlueCommandError, \
    dict_update, Bunch, PatternMap, format_command_line
from gluetool_modules_framework.libs.results.test_result import TestResult, publish_result
from gluetool_modules_framework.libs.results import TestSuite, Log, TestCase

from typing import Any, Dict, cast, Optional, Union

from gluetool_modules_framework.infrastructure.koji_fedora import KojiTask, BrewTask

REQUIRED_CMDS = ['osh-cli']


def _unlink(filepath: str) -> None:
    try:
        os.unlink(filepath)

    except Exception as exc:
        raise GlueError('Unable to remove {}: {}'.format(filepath, exc))


class OSHFailedError(SoftGlueError):
    def __init__(self, url: str) -> None:
        super(OSHFailedError, self).__init__('OSH testing failed, task did not pass')

        self.osh_result_url = url

    # do not send this entry to Sentry
    @property
    def submit_to_sentry(self) -> bool:

        return False


class NoOSHBaselineFoundError(SoftGlueError):
    STATUS = 'SKIP'

    def __init__(self) -> None:
        super(NoOSHBaselineFoundError, self).__init__('Could not find baseline for this build')

    # do not send this error to Sentry, this must be handled by the user
    @property
    def submit_to_sentry(self) -> bool:

        return False


class OSHTestResult(TestResult):
    def __init__(self, glue: gluetool.glue.Glue, overall_result: str, osh_result: Any,
                 task: Any, baseline: Any, **kwargs: Any) -> None:
        urls = kwargs.pop('urls', {})
        urls.update({
            'osh_url': osh_result.url,
            'brew_url': task.url
        })

        super(OSHTestResult, self).__init__(glue, 'osh', overall_result, urls=urls, **kwargs)

        self.baseline = baseline

    def convert_to_results(self) -> TestSuite:
        test_suite = super(OSHTestResult, self).convert_to_results()

        if 'osh_url' in self.urls:
            test_suite.properties.update({'baseosci.url.osh-run': self.urls['osh_url']})

        return test_suite


class OSHTestResultDiff(OSHTestResult):
    def __init__(self, glue: gluetool.glue.Glue, overall_result: str, osh_result: Any,
                 task: Any, baseline: Any, **kwargs: Any) -> None:
        super(OSHTestResultDiff, self).__init__(glue, overall_result, osh_result, task, baseline, **kwargs)

        self.fixed = len(osh_result.fixed)
        self.added = len(osh_result.added)

    @classmethod
    def _unserialize_from_json(cls, glue: gluetool.glue.Glue, input_data: Dict[str, Any]) -> 'OSHTestResultDiff':
        osh_result = Bunch(
            url=input_data['urls']['osh_url'],
            fixed=range(0, input_data['fixed']),
            added=range(0, input_data['added'])
        )

        task = Bunch(url=input_data['urls']['brew_url'])

        return OSHTestResultDiff(
            glue, input_data['overall_result'],
            osh_result, task,
            baseline=input_data['baseline'], ids=input_data['ids'],
            urls=input_data['urls'], payload=input_data['payload']
        )

    def _serialize_to_json(self) -> Dict[str, Any]:
        serialized = super(OSHTestResultDiff, self)._serialize_to_json()

        return dict_update(serialized, {
            'baseline': self.baseline,
            'fixed': self.fixed,
            'added': self.added
        })

    def convert_to_results(self) -> TestSuite:
        test_suite = super(OSHTestResultDiff, self).convert_to_results()

        self.glue.shared('osh_xunit_serialize_diff', test_suite, self)

        return test_suite


class OSHTestResultMock(OSHTestResult):
    def __init__(self, glue: gluetool.Glue, overall_result: str, osh_result: Any, task: Any, **kwargs: Any) -> None:
        super(OSHTestResultMock, self).__init__(glue, overall_result, osh_result, task, None, **kwargs)

        self.defects = len(osh_result.defects)

    @classmethod
    def _unserialize_from_json(cls, glue: gluetool.glue.Glue, input_data: Dict[str, Any]) -> 'OSHTestResultMock':
        osh_result = Bunch(
            url=input_data['urls']['osh_url'],
            results=range(0, input_data['defects'])
        )

        task = Bunch(url=input_data['urls']['brew_url'])

        return OSHTestResultMock(
            glue, input_data['overall_result'],
            osh_result, task,
            ids=input_data['ids'],
            urls=input_data['urls'], payload=input_data['payload']
        )

    def _serialize_to_json(self) -> Dict[str, Any]:
        serialized = super(OSHTestResultMock, self)._serialize_to_json()

        return dict_update(serialized, {
            'defects': self.defects,
        })

    def convert_to_results(self) -> TestSuite:
        test_suite = super(OSHTestResultMock, self).convert_to_results()

        self.glue.shared('osh_xunit_serialize_mock', test_suite, self)

        return test_suite


class OSHResult(object):
    def __init__(self, module: gluetool.Module, task_id: int, srpm: str) -> None:
        self.module = module
        self.task_id = task_id
        self.nvr = srpm.replace('.src.rpm', '')
        self.url = gluetool.utils.render_template(
            self.module.option('osh-task-url-template'),
            logger=self.module.logger,
            **{
                'OSH_TASK_ID': task_id
            }
        )

    def _fetch_diff(self, url: str) -> str:
        diff_json = urlopen(url).read()
        try:
            diff = json.loads(diff_json)
        except ValueError:
            raise OSHFailedError(url)
        log_dict(self.module.debug, 'This is what we got from osh', diff)
        defects = diff['defects']
        self.module.debug('Defects:\n{}\nfetched from {}'.format(format_dict(defects), url))
        return cast(str, defects)

    def status_failed(self) -> bool:
        command = ['osh-cli', 'task-info', str(self.task_id)]
        process_output = Command(command, logger=self.module.logger).run()
        assert process_output.stdout is not None
        match = re.search('state_label = (.*)\n', process_output.stdout)

        if match is None:
            return True

        return match.group(1) == 'FAILED'


class OSHResultDiff(OSHResult):
    @cached_property
    def added(self) -> str:
        added_json_url = self.url + 'log/added.js?format=raw'
        added_defects = self._fetch_diff(added_json_url)
        return added_defects

    @cached_property
    def fixed(self) -> str:
        fixed_json_url = self.url + 'log/fixed.js?format=raw'
        fixed_defects = self._fetch_diff(fixed_json_url)
        return fixed_defects

    # download added.html and fixed.html to keep them as build artifacts
    def download_artifacts(self) -> None:
        urlretrieve(self.url + 'log/added.html?format=raw', filename='added.html')
        urlretrieve(self.url + 'log/fixed.html?format=raw', filename='fixed.html')


class OSHResultMock(OSHResult):
    @cached_property
    def defects(self) -> str:
        json_url = self.url + 'log/{}/scan-results-imp.js?format=raw'.format(self.nvr)
        results = self._fetch_diff(json_url)
        return results

    # download scan-results-imp.html to keep them as build artifacts
    def download_artifacts(self) -> None:
        urlretrieve(self.url + 'log/{}/scan-results-imp.html?format=raw'.format(self.nvr),
                    filename='scan-results-imp.html')


class CIOpenScanHub(gluetool.Module):
    """
    CI OSH module

    This module schedules a OSH task, waits until it is finished and reports
    results in results shared function.

    config-map
    ==========

    .. code-block:: yaml

        ---

        - '(?:rhel|RHEL)-([67]).[0-9]+(?:-z)?-candidate|rhel-(7).1-ppc64le(?:-z)?-candidate':
            - 'rhel-\1-x86_64'
            - 'rhel-\1-x86_64-basescan'
    """

    name = 'osh'
    description = 'Run osh'
    supported_dryrun_level = DryRunLevels.DRY
    task = None

    options = {
        'task-id': {
            'help': 'Do not schedule OSH task, just report from given task id',
        },
        'target_pattern': {
            'help': 'A comma separated list of regexes, which define enabled targets'
        },
        'config-map': {
            'help': 'Path to a file with ``target`` => ``target_config``, ``baseline_config`` patterns.',
            'metavar': 'FILE'
        },
        'osh-task-url-template': {
            'help': 'Url to a coverity scan scheduler'
        },
        'mock-build': {
            'help': 'Issue a mock-build, i.e. a scan without a baseline',
            'action': 'store_true'
        }
    }

    required_options = ('osh-task-url-template',)

    shared_functions = ['osh_xunit_serialize_diff', 'osh_xunit_serialize_mock']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(CIOpenScanHub, self).__init__(*args, **kwargs)

        self._baseline: Optional[Union[KojiTask, BrewTask]] = None

    def sanity(self) -> None:
        check_for_commands(REQUIRED_CMDS)

    def version_diff_build(self, target: str, baseline: str, config: str, base_config: str) -> OSHResultDiff:
        handle, task_id_filename = tempfile.mkstemp()
        try:
            os.close(handle)

            command = ['osh-cli', 'version-diff-build',
                       '--config', config,
                       '--base-config', base_config,
                       '--srpm', target,
                       '--base-srpm', baseline,
                       '--task-id-file', task_id_filename]

            log_blob(self.info, 'OSH command (copy & paste)', format_command_line([command]))

            try:
                Command(command, logger=self.logger).run()
            except GlueCommandError as exc:
                raise GlueError("Failure during 'osh' execution: {}".format(exc.output.stderr))

            with open(task_id_filename, 'r') as task_id_file:
                osh_task_id = int(task_id_file.readline())
        finally:
            _unlink(task_id_filename)

        return OSHResultDiff(self, osh_task_id, target)

    def mock_build(self, target: str, config: str) -> OSHResultMock:
        handle, task_id_filename = tempfile.mkstemp()
        try:
            os.close(handle)

            command = [
                'osh-cli', 'mock-build',
                '--config', config,
                '--task-id-file', task_id_filename,
                target
            ]

            log_blob(self.info, 'OSH command (copy & paste)', format_command_line([command]))

            try:
                Command(command, logger=self.logger).run()

            except GlueCommandError as exc:
                raise GlueError("Failure during 'osh' execution: {}".format(exc.output.stderr))

            with open(task_id_filename, 'r') as task_id_file:
                osh_task_id = int(task_id_file.readline())

        finally:
            _unlink(task_id_filename)

        return OSHResultMock(self, osh_task_id, target)

    def osh_xunit_serialize_diff(self, test_suite: TestSuite, result: OSHTestResultDiff) -> TestSuite:
        test_case = TestCase(
            name=self.shared('primary_task').nvr,
            added=str(result.added),
            fixed=str(result.fixed),
            baseline=result.baseline.nvr,
            result=result.overall_result,
            result_class=result.result_class,
            test_type=result.test_type
        )

        # note we have only one result for osh
        if result.overall_result == 'FAILED':
            test_case.failure = "Test failed - see added.html for details"

        def _log_url(log_name: str) -> str:
            return '{}/log/{}'.format(result.urls.get('osh_url'), log_name)

        logs_url = result.urls.get('osh_url')
        assert logs_url is not None
        logs_url += 'log/{}'

        for log_type in ['added', 'fixed']:
            for log_ext in ['err', 'html', 'js']:
                log_name = "{}.{}".format(log_type, log_ext)
                test_case.logs.append(Log(name=log_name, href=_log_url(log_name)))

        test_case.logs.append(Log(name='src.rpm', href=logs_url.format(result.baseline.nvr + '.src.rpm')))
        test_case.logs.append(Log(name='tar.gz', href=logs_url.format(result.baseline.nvr + '.tar.gz')))

        test_suite.test_cases.append(test_case)

        return test_suite

    def osh_xunit_serialize_mock(self, test_suite: TestSuite, result: OSHTestResultMock) -> TestSuite:
        nvr = self.shared('primary_task').nvr
        test_case = TestCase(
            name=nvr,
            defects=str(result.defects),
            result=result.overall_result,
            result_class=result.result_class,
            test_type=result.test_type
        )

        # note we have only one result for osh
        if result.overall_result == 'FAILED':
            test_case.failure = "Test failed - see scan-results-imp.html for details"

        def _log_url(log_name: str) -> str:
            return '{}/log/{}/{}'.format(result.urls.get('osh_url'), nvr, log_name)

        log_type = 'scan-results-imp'

        for log_ext in ['err', 'html', 'js']:
            log_name = "{}.{}".format(log_type, log_ext)
            test_case.logs.append(Log(name=log_name, href=_log_url(log_name)))

        test_suite.test_cases.append(test_case)

        return test_suite

    def scan(self) -> None:
        osh_result = None
        baseline = self._baseline

        task_id = self.option('task-id')
        if task_id:
            self.info('Skipping osh testing, using existing OSH task id {}'.format(task_id))
            assert self.task is not None
            if self.option('mock-build'):
                osh_result = OSHResultMock(self, task_id, self.task.nvr)
            else:
                osh_result = OSHResultDiff(self, task_id, self.task.nvr)

        if not osh_result and not self.dryrun_allows('Run osh testing'):
            raise GlueError('Can not run osh dryrun without task-id parameter')

        if not osh_result:
            target = self.task

            if not baseline:
                # When no baseline build is found the scan for version-diff-build can not be run.
                # Instead mock-build will run to ensure some sanity test.
                self.info("No basebuild found. Running mock-build instead.")

            assert target is not None
            self.info("Using '{}' (build task id: {}) as target".format(target.nvr, target.id))
            if baseline:
                self.info("Using '{}' (build task id: {}) as baseline".format(baseline.nvr, baseline.id))

            target_srpm_url = target.srpm_urls[0] if target.srpm_urls else None
            if baseline:
                baseline_srpm_url = baseline.srpm_urls[0] if baseline.srpm_urls else None

            if not target_srpm_url:
                raise GlueError('Target srpm is missing')

            if baseline and not baseline_srpm_url:
                raise GlueError('Baseline srpm is missing')

            self.info('Obtaining source RPM(s)')
            self.info('target: {}'.format(target_srpm_url))
            target_srpm, _ = urlretrieve(target_srpm_url, filename=target_srpm_url.split('/')[-1])

            if baseline:
                baseline_srpm_name = 'baseline-{}'.format(os.path.basename(baseline_srpm_url))
                self.info('baseline: "{} -> {}"'.format(baseline_srpm_url, baseline_srpm_name))
                baseline_srpm, _ = urlretrieve(baseline_srpm_url, filename=baseline_srpm_name)

            self.info('Looking for osh configuration in {}'.format(self.option('config-map')))
            configs = PatternMap(self.option('config-map'), logger=self.logger).match(self.task.target, multiple=True)

            if baseline and len(configs) != 2:
                raise GlueError('Mapping file does not provide exactly two configurations for this target')

            target_config = configs[0]
            if baseline:
                baseline_config = configs[1]

            try:
                if baseline and not self.option('mock-build'):
                    osh_result = self.version_diff_build(target_srpm, baseline_srpm, target_config, baseline_config)
                else:
                    osh_result = self.mock_build(target_srpm, target_config)
            finally:
                self.debug('Removing the downloaded source RPM')

                _unlink(target_srpm)
                if baseline:
                    _unlink(baseline_srpm)

        self.info('OSH task url: {0}'.format(osh_result.url))

        if osh_result.status_failed():
            raise OSHFailedError(osh_result.url)

        osh_result.download_artifacts()

        # diff
        if baseline and not self.option('mock-build'):
            if osh_result.added:
                self.info('FAILED: New defects found in package.')
                overall_result = 'FAILED'

            else:
                self.info('PASSED: No new defects found in package.')
                overall_result = 'PASSED'

        # mock build
        else:
            if osh_result.defects:
                self.info('FAILED: Defects found in package.')
                overall_result = 'FAILED'

            else:
                self.info('PASSED: No defects found in package.')
                overall_result = 'PASSED'

        # Log in format expected by postbuild scripting
        self.info('Result of testing: {}'.format(overall_result))

        if baseline and not self.option('mock-build'):
            publish_result(self, OSHTestResultDiff, overall_result, osh_result, self.task, baseline)
            return

        publish_result(self, OSHTestResultMock, overall_result, osh_result, self.task)

    def execute(self) -> None:
        self.require_shared('primary_task')

        self.task = cast(gluetool_modules_framework.infrastructure.koji_fedora.KojiTask, self.shared('primary_task'))

        target = self.task.target
        enabled_targets = self.option('target_pattern')
        self.verbose('enabled targets: {}'.format(enabled_targets))

        self._baseline = self.task.baseline_task

        if enabled_targets and any((re.compile(regex.strip()).match(target) for regex in enabled_targets.split(','))):
            self.info('Running osh for {} on {}'.format(self.task.component, target))
            self.scan()
        else:
            self.info('Target {} is not enabled, skipping job'.format(target))
