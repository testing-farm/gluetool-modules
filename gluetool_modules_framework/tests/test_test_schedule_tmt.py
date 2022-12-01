# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import re
from mock import MagicMock

import pytest
import logging

import gluetool
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
import gluetool_modules_framework.testing.test_schedule_tmt
from gluetool_modules_framework.infrastructure.distgit import DistGit, DistGitRepository
from gluetool_modules_framework.infrastructure.static_guest import StaticLocalhostGuest
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
    returns = map(lambda o: MagicMock(exit_code=0, stdout=o, stderr='', return_value=o), outputs)
    monkeypatch.setattr(gluetool.utils.Command, 'run', MagicMock(side_effect=returns))


@pytest.fixture(name='module')
def fixture_module(monkeypatch):
    module = create_module(gluetool_modules_framework.testing.test_schedule_tmt.TestScheduleTMT)[1]
    module._config['command'] = 'dummytmt'
    module._config['reproducer-comment'] = '# tmt reproducer'
    patch_shared(monkeypatch,
                 module,
                 {'testing_farm_request': MagicMock(environments_requested=[{}], tmt=MagicMock(plan=None), plans=None)})
    return module


@pytest.fixture(name='guest')
def fixture_guest():
    guest = MagicMock()
    guest.name = 'guest0'
    guest.hostname = 'guest0'
    guest.key = 'mockkey0'
    guest.execute = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest.environment = TestingEnvironment(compose='guest-compose')
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


@pytest.mark.parametrize('additional_options, additional_shared, testing_environment, expected_reproducer', [
        (  # virtual provision
            {},
            {},
            TestingEnvironment('x86_64', 'rhel-9'),
            '''# tmt reproducer
dummytmt run --all --verbose provision --how virtual --image guest-compose plan --name ^plan1$'''
        ),
        (  # local - provision done by tmt
            {'how': 'local'},  # NOTE: option does not exist, used only to signal usage of StaticLocalhostGuest
            {},
            TestingEnvironment('x86_64', 'rhel-9'),
            '''# tmt reproducer
dummytmt run --all --verbose provision plan --name ^plan1$ plan --name ^plan1$'''
        ),
        (  # with SUT install commands
            {},
            {'sut_install_commands': ['install_command1', 'install_command2']},
            TestingEnvironment('x86_64', 'rhel-9'),
            """# tmt reproducer
dummytmt run --all --verbose provision --how virtual --image guest-compose prepare --how shell --script '
install_command1
install_command2
' plan --name ^plan1$"""
        ),
        (  # with environment variables
            {},
            {},
            TestingEnvironment(
                'x86_64', 'rhel-9',
                variables={'user_variable1': 'user_value1', 'user_variable2': 'user_value2'}
            ),
            """# tmt reproducer
curl -LO {tmpdir}/tmt-environment-lan1.yaml
dummytmt run --all --verbose -e @tmt-environment-lan1.yaml provision --how virtual --image guest-compose plan --name ^plan1$"""  # noqa
        ),
        (  # with tmt context
            {},
            {},
            TestingEnvironment('x86_64', 'rhel-9', tmt={'context': {'distro': 'rhel', 'trigger': 'push'}}),
            """# tmt reproducer
dummytmt run --all --verbose -c distro=rhel -c trigger=push provision --how virtual --image guest-compose plan --name ^plan1$"""  # noqa
        ),
    ],
    ids=['virtual', 'local', 'sut_install_commands', 'variables', 'tmt_context']
)
def test_tmt_output_dir(
    module, guest, monkeypatch, tmpdir,
    additional_options, additional_shared, expected_reproducer,
    testing_environment
):
    module._config = {**module._config, **additional_options}
    patch_shared(monkeypatch, module, additional_shared)

    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        testing_environment,
        'plan1',
        tmpdir,
        []
    )

    schedule_entry.guest = guest

    if module._config.get('how') == 'local':
        schedule_entry.guest = StaticLocalhostGuest(module, 'localhost')

    with monkeypatch.context() as m:
        # tmt run
        _set_run_outputs(m, 'dummy test done')
        module.run_test_schedule_entry(schedule_entry)

    with open(os.path.join(schedule_entry.work_dirpath, 'tmt-run.log')) as f:
        assert 'dummy test done\n' in f.read()

    with open(os.path.join(schedule_entry.work_dirpath, 'tmt-reproducer.sh')) as f:
        c = f.read()
        print(c)
        print(expected_reproducer.format(tmpdir=tmpdir))
        assert c == expected_reproducer.format(tmpdir=tmpdir)

    shutil.rmtree(schedule_entry.work_dirpath)


@pytest.mark.parametrize('additional_options, additional_shared, expected_tmt_reproducer_regex', [
        (  # Test case no. 1
            {},
            {},
            r'''\# tmt reproducer
cd testcode
dummytmt run --all --verbose provision --how virtual --image guest-compose plan --name \^myfix\$'''  # noqa
        ),
        (  # Test case no. 2
            {'context-template-file': [os.path.abspath(os.path.join(ASSETS_DIR, 'context-template.yaml'))]},
            {},
            r'''\# tmt reproducer
cd testcode
dummytmt --context=@[a-zA-Z0-9\/\._-]+ run --all --verbose provision --how virtual --image guest-compose plan --name \^myfix\$'''  # noqa
        ),
    ]
)
def test_tmt_output_distgit(module, guest, monkeypatch, additional_options, additional_shared,
                            expected_tmt_reproducer_regex, tmpdir):
    module._config = {**module._config, **additional_options}
    patch_shared(monkeypatch, module, additional_shared)

    # this doesn't appear anywhere in results.xml, but _run_plan() needs it
    module_dist_git = create_module(DistGit)[1]
    module_dist_git._repository = DistGitRepository(
        module_dist_git, 'some-package',
        clone_url='http://example.com/git/myproject', branch='myfix'
    )
    module.glue.add_shared('dist_git_repository', module_dist_git)

    #  The module generates some files in CWD, so change it to one that will be cleaned up
    original_cwd = os.getcwd()
    os.chdir(tmpdir)
    try:
        with monkeypatch.context() as m:
            _set_run_outputs(m,
                            '',       # git clone
                            'myfix',  # git show-ref
                            #'',       # git checkout  # TODO: somehow one of the `git` calls is skipped
                            'plan1',  # tmt plan ls
                            '[{"name": "plan_name", "prepare": [{"how": "foo"}, {"how": "install", "exclude": ["exclude1", "exclude2"]}]}]')     # tmt plan export  # noqa
            schedule_entry = module.create_test_schedule([guest.environment])[0]

        schedule_entry.guest = guest

        with monkeypatch.context() as m:
            # tmt run
            _set_run_outputs(m, 'dummy test done')

            module.run_test_schedule_entry(schedule_entry)
    finally:
        os.chdir(original_cwd)

        print(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-reproducer.sh'))
        with open(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-run.log')) as f:
            assert 'dummy test done\n' in f.read()
        with open(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-reproducer.sh')) as f:
            assert re.match(expected_tmt_reproducer_regex, f.read())

    shutil.rmtree(os.path.join(tmpdir, schedule_entry.work_dirpath))


@pytest.mark.parametrize('tec, expected_schedule, expected_logs', [
        (
            None,
            [],
            [(logging.WARN, 'TMT scheduler does not support open constraints')]
        ),
        (
            [TestingEnvironment(TestingEnvironment.ANY)],
            [],
            [(logging.WARN, 'TMT scheduler does not support open constraints')]
        ),
        (
            [TestingEnvironment('x86_64')],
            [],
            [(logging.INFO, 'cloning repo http://example.com/git/myproject (branch not specified, ref myfix)'),
             (logging.INFO, 'looking for plans')]
        )
    ]
)
def test_create_schedule(module, monkeypatch, log, tec, expected_schedule, expected_logs):
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
                         '[]')     # tmt plan export
        schedule = module.create_test_schedule(tec)

    for entry, expected_entry in zip(schedule, expected_schedule):
        assert entry == expected_entry

    for log_level, log_message in expected_logs:
        assert log.match(levelno=log_level, message=log_message)


TEST_PLANS_FROM_GIT_LOG_MESSAGES = [
    '''tmt plans:
[
    "plan1"
]''', '''tmt emitted following warnings:
[
    "warning: foo"
]''', '''tmt plans:
[]'''
]


@pytest.mark.parametrize('tmt_plan_ls, expected_logs, expected_exception', [
        (
            'plan1',
            [(logging.DEBUG, TEST_PLANS_FROM_GIT_LOG_MESSAGES[0])],
            None
        ),
        (
            'plan1\nwarning: foo',
            [(logging.DEBUG, TEST_PLANS_FROM_GIT_LOG_MESSAGES[0]),
             (logging.WARN, TEST_PLANS_FROM_GIT_LOG_MESSAGES[1])],
            None
        ),
        (
            '',
            [],
            (gluetool.GlueError, "Did not find any plans. Command used 'dummytmt plan ls --filter enabled:true'")
        ),
        (
            'warning: foo',
            [(logging.DEBUG, TEST_PLANS_FROM_GIT_LOG_MESSAGES[2]),
             (logging.WARN, TEST_PLANS_FROM_GIT_LOG_MESSAGES[1])],
            (gluetool.GlueError, 'No plans found, cowardly refusing to continue.')
        ),
    ]
)
def test_plans_from_git(module, monkeypatch, log, tmt_plan_ls, expected_logs, expected_exception):
    repodir = 'foo'
    context_files = []
    testing_environment = TestingEnvironment('x86_64')
    filter = None
    with monkeypatch.context() as m:
        _set_run_outputs(m, tmt_plan_ls)

        if expected_exception:
            with pytest.raises(expected_exception[0], match=expected_exception[1]):
                module._plans_from_git(repodir, context_files, testing_environment, filter)
        else:
            module._plans_from_git(repodir, context_files, testing_environment, filter)

    for log_level, log_message in expected_logs:
        assert log.match(levelno=log_level, message=log_message)


@pytest.mark.parametrize('plan, expected', [
        (   # no excludes
            {
                'name': 'plan',
                'prepare': {
                    'how': 'install',
                    'package': ['a', 'b']
                }
            },
            []
        ),
        (   # excludes, prepare is not a list
            {
                'name': 'plan',
                'prepare': {
                    'how': 'install',
                    'exclude': ['package1', 'package2']
                }
            },
            ['package1', 'package2']
        ),
        (   # excludes, prepare is a list
            {
                'name': 'plan',
                'prepare': [{
                    'name': 'Install packages',
                    'how': 'install',
                    'exclude': ['package3', 'package4']
                }]
            },
            ['package3', 'package4']
        ),
        (   # excludes, multiple prepare steps, multiple install excludes
            {
                'name': 'plan',
                'prepare': [
                    {
                        'name': 'Install packages',
                        'how': 'install',
                        'exclude': ['package1', 'package2']
                    },
                    {
                        'name': 'Shell prepare step',
                        'how': 'shell',
                        'script': 'do-something',
                    },
                    {
                        'name': 'Install packages',
                        'how': 'install',
                        'exclude': ['package3', 'package4']
                    }
                ]
            },
            ['package1', 'package2', 'package3', 'package4']
        ),

    ],
    ids=['no_excludes', 'excludes', 'prepare_list', 'multiple_steps']
)
def test_excludes_from_tmt(module, plan, expected):
    assert module.excludes_from_tmt(plan) == expected
