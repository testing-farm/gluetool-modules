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

from . import create_module, check_loadable, patch_shared

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
def fixture_module(monkeypatch):
    module = create_module(gluetool_modules_framework.testing.test_schedule_tmt.TestScheduleTMT)[1]
    module._config['command'] = 'dummytmt'
    # XXX: why does this not apply the default?
    module._config['reproducer-comment'] = '# tmt reproducer'
    patch_shared(monkeypatch,
                 module,
                 {
                     'compose': ['dummy-compose'],
                 },
                 callables={
                     'testing_farm_request': lambda: MagicMock(environments_requested=[{}])
                 })
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
        'some-repo-dir',
        ['exclude1', 'exclude2']
    )

    outcome, results = gather_plan_results(schedule_entry, ASSETS_DIR)

    assert outcome == getattr(TestScheduleResult, expected_results['outcome'])
    _assert_results(results, expected_results['results'])


def test_tmt_output_dir(module, guest, monkeypatch):
    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        TestingEnvironment('x86_64', 'rhel-9'),
        'plan1',
        'some-repo-dir',
        []
    )
    schedule_entry.guest = guest

    with monkeypatch.context() as m:
        # tmt run
        _set_run_outputs(m, 'dummy test done')
        module.run_test_schedule_entry(schedule_entry)

    with open(os.path.join(schedule_entry.work_dirpath, 'tmt-run.log')) as f:
        assert 'dummy test done\n' in f.read()

    with open(os.path.join(schedule_entry.work_dirpath, 'tmt-reproducer.sh')) as f:
        assert f.read() == '''# tmt reproducer
dummytmt run --all --verbose provision --how virtual --image dummy-compose plan --name ^plan1$'''

    shutil.rmtree(schedule_entry.work_dirpath)


def test_tmt_output_distgit(module, guest, monkeypatch):
    module_dist_git = create_module(DistGit)[1]
    module_dist_git._repository = DistGitRepository(
        module_dist_git, 'some-package',
        clone_url='http://example.com/git/myproject', ref='myfix'
    )
    module.glue.add_shared('dist_git_repository', module_dist_git)

    with monkeypatch.context() as m:
        _set_run_outputs(m,
                         '',       # git clone
                         'myfix',  # git show-ref
                         '',       # git checkout
                         'plan1',  # tmt plan ls
                         '...',    # tmt plan show
                         '{}')     # tmt plan export
        schedule_entry = module.create_test_schedule([guest.environment])[0]

    schedule_entry.guest = guest

    with monkeypatch.context() as m:
        # tmt run
        _set_run_outputs(m, 'dummy test done')
        module.run_test_schedule_entry(schedule_entry)

    with open(os.path.join(schedule_entry.work_dirpath, 'tmt-run.log')) as f:
        assert 'dummy test done\n' in f.read()

    with open(os.path.join(schedule_entry.work_dirpath, 'tmt-reproducer.sh')) as f:
        assert f.read() == '''# tmt reproducer
git clone http://example.com/git/myproject testcode
git -C testcode checkout -b testbranch myfix
cd testcode
dummytmt run --all --verbose provision --how virtual --image dummy-compose plan --name ^plan1$'''

    shutil.rmtree(schedule_entry.work_dirpath)
