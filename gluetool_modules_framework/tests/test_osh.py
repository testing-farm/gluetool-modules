# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import pytest

from mock import MagicMock
import gluetool
import gluetool.utils
from gluetool.glue import DryRunLevels
import gluetool_modules_framework.static_analysis.osh.osh
from gluetool_modules_framework.static_analysis.osh.osh import CIOpenScanHub, OSHResultDiff, \
    OSHResultMock, OSHFailedError, NoOSHBaselineFoundError
from gluetool_modules_framework.tests import create_module, patch_shared, assert_shared, check_loadable
from . import testing_asset

ADDED_PASS = """
{
    "defects": "",
    "scan": {
        "time-created": "2017-07-14 10:56:19",
        "mock-config": "rhel-7-x86_64"
    }
}
"""

ADDED_FAIL = """
{
    "defects": "some, defects",
    "scan": {
        "time-created": "2017-07-14 10:56:19",
        "mock-config": "rhel-7-x86_64"
    }
}
"""

FIXED = """
{
    "defects": "",
    "scan": {
        "time-created": "2017-07-27 14:08:11",
        "mock-config": "rhel-7-x86_64-basescan"
    }
}
"""

TASK_INFO_PASS = """
exclusive = False
resubmitted_by = None
weight = 1
state_label = CLOSED
awaited = False
result =
owner = jenkins/baseos-jenkins.rhev-ci-vms.eng.rdu2.redhat.com
id = 55188
state = 3
label = netpbm-10.79.00-1.el7.src.rpm
priority = 10
waiting = False
method = VersionDiffBuild
channel = 1
parent = None
"""

TASK_INFO_FAIL = """
exclusive = False
resubmitted_by = None
weight = 1
state_label = FAILED
awaited = False
result =
owner = jenkins/baseos-jenkins.rhev-ci-vms.eng.rdu2.redhat.com
id = 55122
state = 5
label = qt5-qtimageformats-5.9.1-1.el7.src.rpm
priority = 10
waiting = False
method = VersionDiffBuild
channel = 1
parent = None
"""


@pytest.fixture(name='module')
def fixture_module():
    return create_module(CIOpenScanHub)


def test_loadable(module):
    glue, _ = module

    check_loadable(glue, 'gluetool_modules_framework/static_analysis/osh/osh.py', 'CIOpenScanHub')


def test_no_brew(module):
    _, module = module

    assert_shared('primary_task', module.execute)


def test_not_enabled_target(log, module, monkeypatch):
    enabled_target = '(rhel|RHEL)-[67].[0-9]+(-z)?-candidate,rhel-7.1-ppc64le(-z)?-candidate'
    component_name = 'ssh'
    target = 'not_allowed'

    _, module = module
    module._config['target_pattern'] = enabled_target

    patch_shared(monkeypatch, module, {}, callables={
        'primary_task': MagicMock(return_value=MagicMock(target=target, component=component_name))
        })

    module.execute()

    assert log.records[-1].message == 'Target {} is not enabled, skipping job'.format(target)


def run(result, log, module, monkeypatch, tmpdir, mock_build=False):
    enabled_target = '(rhel|RHEL)-[67].[0-9]+(-z)?-candidate,rhel-7.1-ppc64le(-z)?-candidate'
    component_name = 'ssh'
    target = 'rhel-7.4-candidate'

    baseline_config = testing_asset('osh', 'example-config-map.yml')

    if mock_build:
        module._config['mock-build'] = True
        mocked_baseline_task = None
    else:
        mocked_baseline_task = MagicMock(target=target, component=component_name, srpm_urls=['dummy_baseline.src.rpm'])

    mocked_task = MagicMock(target=target,
                            component=component_name,
                            srpm_urls=['dummy_target.src.rpm'],
                            baseline_task=mocked_baseline_task)

    module._config.update({
        'target_pattern': enabled_target,
        'config-map': str(baseline_config),
        'osh-task-url-template': 'https://cov01.lab.eng.brq.redhat.com/oshhub/task/{{ OSH_TASK_ID }}/'
    })

    def mocked_urlopen(url):
        if 'added' in url or 'fixed' in url or 'scan' in url:
            file_name = 'dummy_file.html'
            outfile = tmpdir.join(file_name)

            if result == 'PASSED':
                outfile.write(ADDED_PASS)
            elif result == 'FAILED':
                outfile.write(ADDED_FAIL)
            return outfile
        else:
            return ''

    class MockedCommand(object):

        def __init__(self, command, *args, **kwargs):
            self.cmd = command

        def run(self):
            if self.cmd[1] == 'version-diff-build':
                with open(self.cmd[-1], 'w') as outfile:
                    outfile.write('1234')
            elif self.cmd[1] == 'task-info':
                if result in ['PASSED', 'FAILED']:
                    return MagicMock(stdout=TASK_INFO_PASS)
                if result in ['FAIL']:
                    return MagicMock(stdout=TASK_INFO_FAIL)
            elif self.cmd[1] == 'mock-build':
                # NOTE: task-id-file param is the second item from the end
                with open(self.cmd[-2], 'w') as outfile:
                    outfile.write('1234')

    def mocked_urlretrieve(url, filename=None):
        if 'rpm' in url:
            with open(filename or url, 'w') as outfile:
                outfile.write('')
            return (os.path.abspath(filename or url), {'mock_headers': 'mock_headers'})
        else:
            pass

    patch_shared(monkeypatch, module, {
        'primary_task': mocked_task
    })

    monkeypatch.setattr(gluetool_modules_framework.static_analysis.osh.osh, 'Command', MockedCommand)
    monkeypatch.setattr(gluetool_modules_framework.static_analysis.osh.osh, 'urlretrieve', mocked_urlretrieve)
    monkeypatch.setattr(gluetool_modules_framework.static_analysis.osh.osh, 'urlopen', mocked_urlopen)

    module.execute()


def test_pass_run(log, module, monkeypatch, tmpdir):
    _, module = module
    result = 'PASSED'
    run(result, log, module, monkeypatch, tmpdir)
    assert log.match(message='Result of testing: {}'.format(result))


def test_fail_run(log, module, monkeypatch, tmpdir):
    _, module = module
    result = 'FAILED'
    run(result, log, module, monkeypatch, tmpdir)
    assert log.match(message='Result of testing: {}'.format(result))


def test_pass_run_mock_build(log, module, monkeypatch, tmpdir, mock_build=True):
    _, module = module
    result = 'PASSED'
    run(result, log, module, monkeypatch, tmpdir, mock_build)
    assert log.match(message='Result of testing: {}'.format(result))


def test_fail_run_mock_build(log, module, monkeypatch, tmpdir, mock_build=True):
    _, module = module
    result = 'FAILED'
    run(result, log, module, monkeypatch, tmpdir, mock_build)
    assert log.match(message='Result of testing: {}'.format(result))


def test_run_command_error(module, monkeypatch):
    _, module = module

    output = MagicMock(exit_code=1)

    def mocked_run_command(cmd):
        raise gluetool.GlueCommandError(cmd, output)

    monkeypatch.setattr(gluetool_modules_framework.static_analysis.osh.osh.Command, 'run', mocked_run_command)

    with pytest.raises(gluetool.GlueError, match=r"^Failure during 'osh' execution"):
        module.version_diff_build('srpm', 'baseline', 'config', 'baseconfig')


def test_invalid_json(monkeypatch, tmpdir):
    def mocked_urlopen(url):
        file_name = 'dummy_file.html'
        outfile = tmpdir.join(file_name)
        outfile.write('{{{ some invalid json')
        return outfile

    monkeypatch.setattr(gluetool_modules_framework.static_analysis.osh.osh, 'urlopen', mocked_urlopen)

    module = MagicMock(task=111)
    module.option = MagicMock(
        return_value='https://cov01.lab.eng.brq.redhat.com/oshhub/task/{{ OSH_TASK_ID }}/')
    result = OSHResultDiff(module, 000, 'nvr')

    with pytest.raises(OSHFailedError):
        result.added


def test_no_baseline(monkeypatch, tmpdir):
    def mocked_urlopen(url):
        file_name = 'dummy_file.html'
        outfile = tmpdir.join(file_name)

        outfile.write(ADDED_PASS)
        return outfile

    monkeypatch.setattr(gluetool_modules_framework.static_analysis.osh.osh, 'urlopen', mocked_urlopen)

    module = MagicMock(task=111)
    module.option = MagicMock(
        return_value='https://cov01.lab.eng.brq.redhat.com/oshhub/task/{{ OSH_TASK_ID }}/')
    result = OSHResultDiff(module, 000, 'nvr')

    module.task = MagicMock(latest_released=MagicMock(return_value=None))

    assert result


def test_fetch_added(monkeypatch, tmpdir):
    def mocked_urlopen(url):
        file_name = 'dummy_file.html'
        outfile = tmpdir.join(file_name)

        outfile.write(ADDED_PASS)
        return outfile

    monkeypatch.setattr(gluetool_modules_framework.static_analysis.osh.osh, 'urlopen', mocked_urlopen)

    module = MagicMock(task=111)
    module.option = MagicMock(
        return_value='https://cov01.lab.eng.brq.redhat.com/oshhub/task/{{ OSH_TASK_ID }}/')
    result = OSHResultDiff(module, 000, 'nvr')

    assert result.added == ''


def test_fetch_fixed(monkeypatch, tmpdir):
    def mocked_urlopen(url):
        file_name = 'dummy_file.html'
        outfile = tmpdir.join(file_name)
        outfile.write(FIXED)
        return outfile

    monkeypatch.setattr(gluetool_modules_framework.static_analysis.osh.osh, 'urlopen', mocked_urlopen)

    module = MagicMock(task=111)
    module.option = MagicMock(
        return_value='https://cov01.lab.eng.brq.redhat.com/oshhub/task/{{ OSH_TASK_ID }}/')
    result = OSHResultDiff(module, 000, 'nvr')

    assert result.fixed == ''


def test_osh_fail(log, module, monkeypatch, tmpdir):
    _, module = module

    with pytest.raises(OSHFailedError):
        run('FAIL', log, module, monkeypatch, tmpdir)


def test_only_task_id(log, module, monkeypatch, tmpdir):
    _, module = module
    module._config['task-id'] = '1234'

    run('PASSED', log, module, monkeypatch, tmpdir)
    assert log.match(message='Skipping osh testing, using existing OSH task id 1234')


def test_dry_run_with_task_id(log, module, monkeypatch, tmpdir):
    ci, module = module
    ci._dryrun_level = DryRunLevels.DRY
    module._config['task-id'] = '1234'

    run('PASSED', log, module, monkeypatch, tmpdir)
    assert log.match(message='Skipping osh testing, using existing OSH task id 1234')


def test_dry_run_without_taskid(module):
    ci, module = module
    ci._dryrun_level = DryRunLevels.DRY

    with pytest.raises(gluetool.GlueError, match=r"^Can not run osh dryrun without task-id parameter"):
        module.scan()
