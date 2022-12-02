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
from gluetool_modules_framework.helpers.install_copr_build import InstallCoprBuild
from gluetool_modules_framework.libs.guest_setup import GuestSetupStage
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


@pytest.fixture(name='module_dist_git')
def fixture_module_dist_git():
    module_dist_git = create_module(DistGit)[1]
    module_dist_git._repository = DistGitRepository(
        module_dist_git, 'some-package',
        clone_url='http://example.com/git/myproject', ref='myfix'
    )
    return module_dist_git


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
        assert len(result.artifacts) == len(expected['artifacts'])
        for ta, ea in zip(result.artifacts, expected['artifacts']):
            assert ta.name == ea['name']
            assert ta.path == os.path.join(ASSETS_DIR, ea['path'])


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


def test_serialize_test_schedule_entry_results(module, module_dist_git, guest, monkeypatch):
    # this doesn't appear anywhere in results.xml, but _run_plan() needs it
    module.glue.add_shared('dist_git_repository', module_dist_git)

    test_env = TestingEnvironment('x86_64', 'rhel-9')
    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        test_env,
        '/passed',
        'some-repo-dir',
        ['exclude1', 'exclude2']
    )
    schedule_entry.guest = guest
    schedule_entry.testing_environment = test_env

    # gather_plan_results() is called in _run_plan() right after calling tmt; we need to inject
    # writing results.yaml in between, which we can't do with a mock
    orig_gather_plan_results = gluetool_modules_framework.testing.test_schedule_tmt.gather_plan_results

    def inject_gather_plan_results(schedule_entry, work_dir, recognize_errors=False):
        shutil.copytree(os.path.join(ASSETS_DIR, 'passed'), os.path.join(work_dir, 'passed'))
        return orig_gather_plan_results(schedule_entry, work_dir, recognize_errors=recognize_errors)

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
    # expecting log_dir, testout.log, and journal.txt, in exactly that order
    assert len(testcase_docs.logs) == 3
    assert testcase_docs.logs.contents[0].name == 'log'
    assert testcase_docs.logs.contents[0]['name'] == 'log_dir'
    assert testcase_docs.logs.contents[0]['href'].endswith('/passed/execute/logs/tests/core/docs')
    assert testcase_docs.logs.contents[1]['name'] == 'testout.log'
    assert testcase_docs.logs.contents[1]['href'].endswith('/passed/execute/logs/tests/core/docs/out.log')
    assert testcase_docs.logs.contents[2]['name'] == 'journal.txt'
    assert testcase_docs.logs.contents[2]['href'].endswith('/passed/execute/logs/tests/core/docs/journal.txt')

    assert testcase_dry['name'] == '/tests/core/dry'
    assert testcase_dry['result'] == 'passed'
    assert len(testcase_dry.logs) == 2
    assert testcase_dry.logs.contents[0]['name'] == 'log_dir'
    assert testcase_dry.logs.contents[0]['href'].endswith('/passed/execute/logs/tests/core/dry')
    assert testcase_dry.logs.contents[1]['name'] == 'testout.log'
    assert testcase_dry.logs.contents[1]['href'].endswith('/passed/execute/logs/tests/core/dry/out.log')

    shutil.rmtree(schedule_entry.work_dirpath)


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


def test_tmt_output_distgit(module, module_dist_git, guest, monkeypatch):
    # this doesn't appear anywhere in results.xml, but _run_plan() needs it
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


def test_tmt_output_copr(module, module_dist_git, guest, monkeypatch, tmpdir):
    # install-copr-build module
    module_copr = create_module(InstallCoprBuild)[1]
    module_copr._config['log-dir-name'] = 'artifact-installation'
    primary_task_mock = MagicMock()
    primary_task_mock.repo_url = 'http://copr/project.repo'
    primary_task_mock.rpm_urls = ['http://copr/project/one.rpm', 'http://copr/project/two.rpm']
    primary_task_mock.rpm_names = ['one', 'two']
    primary_task_mock.project = 'owner/project'

    patch_shared(monkeypatch, module_copr, {
        'primary_task': primary_task_mock,
        'tasks': [primary_task_mock],
    })

    # main test-schedule-tmt module
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

    # these are normally done by TestScheduleRunner, but running that is too involved for a unit test
    guest_setup_output = module_copr.setup_guest(
            guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION, log_dirpath=str(tmpdir))

    schedule_entry.guest = guest
    schedule_entry.guest_setup_outputs = {GuestSetupStage.ARTIFACT_INSTALLATION: guest_setup_output.unwrap()}

    with monkeypatch.context() as m:
        # tmt run
        _set_run_outputs(m, 'dummy test done')
        module.run_test_schedule_entry(schedule_entry)

    with open(os.path.join(schedule_entry.work_dirpath, 'tmt-run.log')) as f:
        assert 'dummy test done\n' in f.read()

    # COPR installation actually happened
    guest.execute.assert_any_call(
        'dnf --allowerasing -y install http://copr/project/one.rpm http://copr/project/two.rpm')

    # ... and is shown in the reproducer
    with open(os.path.join(schedule_entry.work_dirpath, 'tmt-reproducer.sh')) as f:
        assert f.read() == '''# tmt reproducer
git clone http://example.com/git/myproject testcode
git -C testcode checkout -b testbranch myfix
cd testcode
dummytmt run --all --verbose provision --how virtual --image dummy-compose prepare --how shell --script '
curl http://copr/project.repo --retry 5 --output /etc/yum.repos.d/copr_build-owner_project-1.repo
dnf --allowerasing -y reinstall http://copr/project/one.rpm || true
dnf --allowerasing -y reinstall http://copr/project/two.rpm || true
dnf --allowerasing -y install http://copr/project/one.rpm http://copr/project/two.rpm
rpm -q one
rpm -q two
' plan --name ^plan1$'''
