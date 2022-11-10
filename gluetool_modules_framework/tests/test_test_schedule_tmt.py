# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
from mock import MagicMock

import pytest

import gluetool
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
import gluetool_modules_framework.testing.test_schedule_tmt
from gluetool_modules_framework.infrastructure.distgit import DistGit, DistGitRepository
from gluetool_modules_framework.libs.test_schedule import TestScheduleResult
from gluetool_modules_framework.testing.test_schedule_tmt import gather_plan_results, TestScheduleEntry

from . import create_module, check_loadable

ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets', 'test_schedule_tmt')


def _load_assets(name):
    return (
        name,
        gluetool.utils.load_yaml(os.path.join(ASSETS_DIR, '{}.yaml'.format(name))),
    )


def _set_run_outputs(monkeypatch, *outputs):
    '''Monkey-patch gluetools.utils.Command.run to return given output'''
    returns = map(lambda o: MagicMock(exit_code=0, stdout=o, stderr=''), outputs)
    monkeypatch.setattr(gluetool.utils.Command, 'run', MagicMock(side_effect=returns))


@pytest.fixture(name='module')
def fixture_module():
    module = create_module(gluetool_modules_framework.testing.test_schedule_tmt.TestScheduleTMT)[1]

    return module


@pytest.fixture(name='guest')
def fixture_guest():
    guest = MagicMock()
    guest.name = 'guest0'
    guest.hostname = 'guest0'
    guest.key = 'mockkey0'
    guest.execute = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    return guest


def test_sanity(module):
    assert isinstance(module, gluetool_modules_framework.testing.test_schedule_tmt.TestScheduleTMT)


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/testing/test_schedule_tmt.py', 'TestScheduleTMT')


def test_shared(module):
    module.add_shared()

    for functions in ['create_test_schedule', 'run_test_schedule_entry', 'serialize_test_schedule_entry_results']:
        assert module.glue.has_shared(functions)


def _assert_results(results, expected_results):
    for result, expected in zip(results, expected_results):
        assert result.name == expected['name']
        assert result.result == expected['result']
        assert result.log == os.path.join(ASSETS_DIR, expected['log'])
        assert result.artifacts_dir == os.path.join(ASSETS_DIR, expected['artifacts_dir'])


@pytest.mark.parametrize('asset', [
        _load_assets('passed'),
        _load_assets('failed'),
        _load_assets('error'),
    ]
)
def test_gather_results(module, asset, monkeypatch):
    name, expected_results = asset

    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        TestingEnvironment('x86_64', 'rhel-9'),
        # a plan always starts with slash
        '/{}'.format(name),
        'some-repo-dir'
    )

    outcome, results = gather_plan_results(schedule_entry, ASSETS_DIR)

    assert outcome == getattr(TestScheduleResult, expected_results['outcome'])
    _assert_results(results, expected_results['results'])


def test_serialize_test_schedule_entry_results(module, guest, monkeypatch):
    # this doesn't appear anywhere in results.xml, but _run_plan() needs it
    module_dist_git = create_module(DistGit)[1]
    module_dist_git._repository = DistGitRepository(
        module_dist_git, 'some-package',
        clone_url='http://example.com/git/myproject', ref='myfix'
    )
    module.glue.add_shared('dist_git_repository', module_dist_git)

    test_env = TestingEnvironment('x86_64', 'rhel-9')
    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        test_env,
        '/passed',
        'some-repo-dir'
    )
    schedule_entry.guest = guest
    schedule_entry.testing_environment = test_env

    # gather_plan_results() is called in _run_plan() right after calling tmt; we need to inject
    # writing results.yaml in between, which we can't do with a mock
    orig_gather_plan_results = gluetool_modules_framework.testing.test_schedule_tmt.gather_plan_results

    def inject_gather_plan_results(schedule_entry, work_dir):
        shutil.copytree(os.path.join(ASSETS_DIR, 'passed'), os.path.join(work_dir, 'passed'))
        return orig_gather_plan_results(schedule_entry, work_dir)

    # run tmt with the mock plan
    with monkeypatch.context() as m:
        _set_run_outputs(m, 'dummy test done')
        try:
            gluetool_modules_framework.testing.test_schedule_tmt.gather_plan_results = inject_gather_plan_results
            module.run_test_schedule_entry(schedule_entry)
        finally:
            gluetool_modules_framework.testing.test_schedule_tmt.gather_plan_results = orig_gather_plan_results

    # generate results.xml
    test_suite = gluetool.utils.new_xml_element('testsuite')
    module.shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)

    assert test_suite['tests'] == 2
    testcase_docs, testcase_dry = test_suite.contents
    assert testcase_docs.name == 'testcase'
    assert testcase_docs['name'] == '/tests/core/docs'
    assert testcase_docs['result'] == 'passed'
    # expecting log_dir and testout.log, in exactly that order; current code ignores journal.txt
    assert len(testcase_docs.logs) == 2
    assert testcase_docs.logs.contents[0].name == 'log'
    assert testcase_docs.logs.contents[0]['name'] == 'log_dir'
    assert testcase_docs.logs.contents[0]['href'].endswith('/passed/execute/logs/tests/core/docs')
    assert testcase_docs.logs.contents[1]['name'] == 'testout.log'
    assert testcase_docs.logs.contents[1]['href'].endswith('/passed/execute/logs/tests/core/docs/out.log')

    assert testcase_dry['name'] == '/tests/core/dry'
    assert testcase_dry['result'] == 'passed'
    assert len(testcase_dry.logs) == 2
    assert testcase_dry.logs.contents[0]['name'] == 'log_dir'
    assert testcase_dry.logs.contents[0]['href'].endswith('/passed/execute/logs/tests/core/dry')
    assert testcase_dry.logs.contents[1]['name'] == 'testout.log'
    assert testcase_dry.logs.contents[1]['href'].endswith('/passed/execute/logs/tests/core/dry/out.log')

    shutil.rmtree(schedule_entry.work_dirpath)
