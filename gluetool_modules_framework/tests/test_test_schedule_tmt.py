# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import re
from mock import MagicMock
from typing import List

import pytest
import logging

import gluetool
from gluetool.utils import normalize_multistring_option
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
import gluetool_modules_framework.testing.test_schedule_tmt
from gluetool_modules_framework.infrastructure.distgit import DistGit, DistGitRepository
from gluetool_modules_framework.infrastructure.static_guest import StaticLocalhostGuest
from gluetool_modules_framework.helpers.install_copr_build import InstallCoprBuild
from gluetool_modules_framework.helpers.install_koji_build_execute import InstallKojiBuildExecute
from gluetool_modules_framework.libs.guest_setup import GuestSetupStage
from gluetool_modules_framework.libs.sut_installation import INSTALL_COMMANDS_FILE
from gluetool_modules_framework.libs.test_schedule import TestScheduleResult
from gluetool_modules_framework.libs.results import TestSuite
from gluetool_modules_framework.libs.git import SecretGitUrl
from gluetool_modules_framework.provision.artemis import ArtemisGuest, ArtemisGuestLog
from gluetool_modules_framework.testing.test_schedule_tmt import (gather_plan_results, TestScheduleEntry, TMTPlan,
                                                                  TMTPlanProvision, TMTPlanPrepare)
from gluetool_modules_framework.testing_farm.testing_farm_request import Artifact

from . import create_module, check_loadable, patch_shared

ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets', 'test_schedule_tmt')


def _load_assets(name):
    return (
        name,
        gluetool.utils.load_yaml(os.path.join(ASSETS_DIR, '{}.yaml'.format(name))),
    )


def _set_run_outputs(monkeypatch, *outputs):
    '''Monkey-patch gluetools.utils.Command.run to return given output'''
    returns = map(lambda o: MagicMock(exit_code=0, stdout=o, stderr=o, return_value=o), outputs)
    run_mock = MagicMock(side_effect=returns)
    monkeypatch.setattr(gluetool.utils.Command, 'run', run_mock)
    return run_mock


@pytest.fixture(name='module')
def fixture_module(monkeypatch):
    module = create_module(gluetool_modules_framework.testing.test_schedule_tmt.TestScheduleTMT)[1]
    module._config['command'] = 'dummytmt'
    module._config['reproducer-comment'] = '# tmt reproducer'
    patch_shared(
        monkeypatch,
        module,
        {
            'testing_farm_request': MagicMock(
                environments_requested=[{}],
                tmt=MagicMock(plan=None, plan_filter=None, path="some-tmt-root", test_filter=None, test_name=None),
                plans=None)
        }
    )
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
def fixture_guest(module):
    guest = ArtemisGuest(
        MagicMock(),
        'guest0',
        hostname='guest0',
        environment=TestingEnvironment(compose='guest-compose'),
        key='mockkey0'
    )
    guest.execute = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest.guest_logs = [ArtemisGuestLog(
        name='console.log',
        type='some-type',
        filename='console-{guestname}.log',
        datetime_filename='console-{guestname}-{datetime}.log'
    )]
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
        _load_assets('weird.name+passed'),
        _load_assets('skipped'),
    ]
)
def test_gather_results(module, asset, monkeypatch):
    name, expected_results = asset

    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        TestingEnvironment('x86_64', 'rhel-9', excluded_packages=['exclude1', 'exclude2']),
        # a plan always starts with slash
        '/{}'.format(name),
        'some-repo-dir'
    )

    outcome, results = gather_plan_results(module, schedule_entry, ASSETS_DIR)

    assert outcome == getattr(TestScheduleResult, expected_results['outcome'])
    _assert_results(results, expected_results['results'])


def test_serialize_test_schedule_entry_results(module, module_dist_git, guest, monkeypatch, tmpdir):
    # this doesn't appear anywhere in results.xml, but _run_plan() needs it
    module.glue.add_shared('dist_git_repository', module_dist_git)

    test_env = TestingEnvironment('x86_64', 'rhel-9', excluded_packages=['exclude1', 'exclude2'])
    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        test_env,
        '/passed',
        'some-repo-dir'
    )
    schedule_entry.guest = guest
    schedule_entry.testing_environment = test_env
    schedule_entry.work_dirpath = os.path.join(tmpdir, 'some-workdir')
    os.mkdir(schedule_entry.work_dirpath)

    # gather_plan_results() is called in _run_plan() right after calling tmt; we need to inject
    # writing results.yaml in between, which we can't do with a mock
    orig_gather_plan_results = gluetool_modules_framework.testing.test_schedule_tmt.gather_plan_results

    def inject_gather_plan_results(module, schedule_entry, work_dir, recognize_errors=False):
        shutil.copytree(os.path.join(ASSETS_DIR, 'passed'), os.path.join(work_dir, 'passed'))
        return orig_gather_plan_results(module, schedule_entry, work_dir, recognize_errors=recognize_errors)

    # run tmt with the mock plan
    with monkeypatch.context() as m:
        _set_run_outputs(m, 'dummy test done')
        try:
            gluetool_modules_framework.testing.test_schedule_tmt.gather_plan_results = inject_gather_plan_results
            module.run_test_schedule_entry(schedule_entry)
        finally:
            gluetool_modules_framework.testing.test_schedule_tmt.gather_plan_results = orig_gather_plan_results

    # generate results.xml
    test_suite = TestSuite(name='some-suite', result='some-result')
    module.shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)

    assert test_suite.test_count == 2
    testcase_docs, testcase_dry = test_suite.test_cases[0], test_suite.test_cases[1]
    assert testcase_docs.name == '/tests/core/docs'
    assert testcase_docs.result == 'passed'
    # expecting log_dir, testout.log, and journal.txt, in exactly that order
    assert len(testcase_docs.logs) == 4
    assert testcase_docs.logs[0].name == 'log_dir'
    assert testcase_docs.logs[0].href.endswith('/passed/execute/logs/tests/core/docs')
    assert testcase_docs.logs[1].name == 'data'
    assert testcase_docs.logs[1].href.endswith('/passed/execute/logs/tests/core/docs/data')
    assert testcase_docs.logs[2].name == 'testout.log'
    assert testcase_docs.logs[2].href.endswith('/passed/execute/logs/tests/core/docs/out.log')
    assert testcase_docs.logs[3].name == 'journal.txt'
    assert testcase_docs.logs[3].href.endswith('/passed/execute/logs/tests/core/docs/journal.txt')

    assert testcase_dry.name == '/tests/core/dry'
    assert testcase_dry.result == 'passed'
    assert testcase_dry.note == ['original result: fail']
    assert len(testcase_dry.logs) == 3
    assert testcase_dry.logs[0].name == 'log_dir'
    assert testcase_dry.logs[0].href.endswith('/passed/execute/logs/tests/core/dry')
    assert testcase_dry.logs[1].name == 'data'
    assert testcase_dry.logs[1].href.endswith('/passed/execute/logs/tests/core/dry/data')
    assert testcase_dry.logs[2].name == 'testout.log'
    assert testcase_dry.logs[2].href.endswith('/passed/execute/logs/tests/core/dry/out.log')

    shutil.rmtree(schedule_entry.work_dirpath)


def test_serialize_test_schedule_entry_no_results(module, module_dist_git, guest, monkeypatch, tmpdir):
    # this doesn't appear anywhere in results.xml, but _run_plan() needs it
    module.glue.add_shared('dist_git_repository', module_dist_git)

    test_env = TestingEnvironment('x86_64', 'rhel-9', excluded_packages=['exclude1', 'exclude2'])
    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        test_env,
        '/passed',
        'some-repo-dir'
    )
    schedule_entry.guest = guest
    schedule_entry.testing_environment = test_env
    schedule_entry.work_dirpath = os.path.join(tmpdir, 'some-workdir')
    os.mkdir(schedule_entry.work_dirpath)

    # gather_plan_results() is called in _run_plan() right after calling tmt; we need to inject
    # writing results.yaml in between, which we can't do with a mock
    orig_gather_plan_results = gluetool_modules_framework.testing.test_schedule_tmt.gather_plan_results

    def inject_gather_plan_results(module, schedule_entry, work_dir, recognize_errors=False):
        shutil.copytree(os.path.join(ASSETS_DIR, 'passed'), os.path.join(work_dir, 'passed'))
        return orig_gather_plan_results(module, schedule_entry, work_dir, recognize_errors=recognize_errors)

    # run tmt with the mock plan
    with monkeypatch.context() as m:
        _set_run_outputs(m, 'dummy test done')
        try:
            gluetool_modules_framework.testing.test_schedule_tmt.gather_plan_results = inject_gather_plan_results
            module.run_test_schedule_entry(schedule_entry)
        finally:
            gluetool_modules_framework.testing.test_schedule_tmt.gather_plan_results = orig_gather_plan_results

    schedule_entry.results = None
    # generate results.xml
    test_suite = TestSuite(name='some-suite', result='some-result')
    module.shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)

    assert len(test_suite.logs) == 4

    assert test_suite.logs[0].name == 'workdir'
    assert test_suite.logs[0].href.endswith('some-workdir')

    assert test_suite.logs[1].name == 'tmt-log'
    assert test_suite.logs[1].href.endswith('some-workdir/tmt-run.log')

    assert test_suite.logs[2].name == 'console.log'
    assert test_suite.logs[2].href.endswith('some-workdir/console-guest0.log')

    assert test_suite.logs[3].name == 'tmt-reproducer'
    assert test_suite.logs[3].href.endswith('some-workdir/tmt-reproducer.sh')

    shutil.rmtree(schedule_entry.work_dirpath)


@pytest.mark.parametrize(
        'additional_options, additional_shared, testing_environment, expected_reproducer, expected_environment, exception', [
        (  # virtual provision
            {},
            {
                'testing_farm_request': MagicMock(
                    environments_requested=[{}],
                    tmt=MagicMock(plan=None, plan_filter=None, path="some-tmt-root", test_filter=None, test_name='some-name'),  # noqa
                    plans=None)
            },
            TestingEnvironment('x86_64', 'rhel-9'),
            '''# tmt reproducer
dummytmt --root some-tmt-root run --all --verbose provision --how virtual --image guest-compose plan --name ^plan1$ tests --name some-name''',
            None,
            None
        ),
        (  # local - provision done by tmt
            {'how': 'local'},  # NOTE: option does not exist, used only to signal usage of StaticLocalhostGuest
            {},
            TestingEnvironment('x86_64', 'rhel-9'),
            '''# tmt reproducer
dummytmt --root some-tmt-root run --all --verbose provision --how container plan --name ^plan1$''',
            None,
            None
        ),
        (  # with environment variables and secrets
            {},
            {
                'user_variables': {
                    'user_variable3': 'user_value3',
                }
            },
            TestingEnvironment(
                'x86_64', 'rhel-9',
                variables={'user_variable1': 'user_value1', 'user_variable2': 'user_value2'},
                secrets={'secret_variable1': 'secret_value1', 'secret_variable2': 'secret_value2'}
            ),
            """# tmt reproducer
curl -LO tmt-environment-lan1.yaml
dummytmt --root some-tmt-root run --all --verbose -e @tmt-environment-lan1.yaml provision --how virtual --image guest-compose plan --name ^plan1$""",  # noqa
            """user_variable1: user_value1
user_variable2: user_value2
user_variable3: user_value3
secret_variable1: secret_value1
secret_variable2: secret_value2
""",
            None
        ),
        (  # with tmt context
            {},
            {},
            TestingEnvironment('x86_64', 'rhel-9', tmt={'context': {'distro': 'rhel', 'trigger': 'push'}}),
            """# tmt reproducer
dummytmt --root some-tmt-root -c distro=rhel -c trigger=push run --all --verbose provision --how virtual --image guest-compose plan --name ^plan1$""",  # noqa
            None,
            None
        ),
        (  # with tmt process environment variables from options only
            {
                'accepted-environment-variables': 'VARIABLE1',
                'environment-variables': [
                    'VARIABLE1=VAL1'
                ]
            },
            {},
            TestingEnvironment('x86_64', 'rhel-9'),
            """# tmt reproducer
dummytmt --root some-tmt-root run --all --verbose provision --how virtual --image guest-compose plan --name ^plan1$""",  # noqa
            None,
            None
        ),
        (  # with tmt process environment variables from both sources
            {
                'accepted-environment-variables': 'VARIABLE1,VARIABLE2,VARIABLE3,VARIABLE4,VARIABLE5',
                'environment-variables': [
                    'VARIABLE3=VAL3,VARIABLE4=VAL4',
                    'VARIABLE5=VAL5'
                ]
            },
            {},
            TestingEnvironment('x86_64', 'rhel-9', tmt={'environment': {'VARIABLE1': 'VALUE1', 'VARIABLE2': 'VALUE2'}}),
            """# tmt reproducer
export VARIABLE1=hidden VARIABLE2=hidden
dummytmt --root some-tmt-root run --all --verbose provision --how virtual --image guest-compose plan --name ^plan1$""",  # noqa
            None,
            None
        ),
        (  # with tmt process environment variables, variables not accepted
            {
                'accepted-environment-variables': 'VARIABLE1'
            },
            {},
            TestingEnvironment('x86_64', 'rhel-9', tmt={'environment': {'VARIABLE1': 'VALUE1', 'VARIABLE2': 'VALUE2'}}),
            """# tmt reproducer
export VARIABLE1=hidden VARIABLE2=hidden
dummytmt --root some-tmt-root run --all --verbose provision --how virtual --image guest-compose plan --name ^plan1$""",  # noqa
            None,
            (gluetool.glue.GlueError, "Environment variable 'VARIABLE2' is not allowed to be exposed to the tmt process")
        ),
    ],
    ids=[
        'virtual', 'local', 'variables', 'tmt_context',
        'tmt_process_environment_options_only', 'tmt_process_environment', 'tmt_process_environment_not_accepted'
    ]
)
def test_tmt_output_dir(
    module, guest, monkeypatch, tmpdir,
    additional_options, additional_shared,
    testing_environment,
    expected_reproducer, expected_environment,
    exception
):
    module._config = {**module._config, **additional_options}
    patch_shared(monkeypatch, module, additional_shared)

    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        testing_environment,
        'plan1',
        tmpdir
    )

    schedule_entry.guest = guest

    schedule_entry.tmt_env_file = module._prepare_tmt_env_file(testing_environment, 'plan1', tmpdir)
    schedule_entry.work_dirpath = os.path.join(tmpdir, 'some-workdir')
    os.mkdir(schedule_entry.work_dirpath)

    if module._config.get('how') == 'local':
        schedule_entry.guest = StaticLocalhostGuest(module, 'localhost')

    # make a copy of variables
    variables = testing_environment.variables.copy() if testing_environment.variables else None

    with monkeypatch.context() as m:
        # tmt run
        run_mock = _set_run_outputs(m, 'dummy test done')

        with monkeypatch.context() as m:
            m.chdir(tmpdir)
            if exception:
                with pytest.raises(exception[0], match=exception[1]):
                    module.run_test_schedule_entry(schedule_entry)
            else:
                module.run_test_schedule_entry(schedule_entry)

    # make sure testing environment variables do not change
    assert schedule_entry.testing_environment.variables == variables

    # do not continue if test schedule entry fails in an exception
    if exception:
        return

    with open(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-run.log')) as f:
        assert 'dummy test done\n' in f.read()

    with open(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-reproducer.sh')) as f:
        c = f.read()
        print(c)
        print(expected_reproducer)
        assert c == expected_reproducer

    with open(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-reproducer.sh')) as f:
        c = f.read()
        print(c)
        print(expected_reproducer)
        assert c == expected_reproducer

    tmt_environment_file = os.path.join(tmpdir, schedule_entry.repodir, 'tmt-environment-lan1.yaml')
    if expected_environment:
        with open(tmt_environment_file) as f:
            c = f.read()
            print(c)
            print(expected_environment)
            assert c == expected_environment
    else:
        assert not os.path.exists(tmt_environment_file)

    expected_tmt_environment = {}

    if 'environment-variables' in additional_options:
        expected_tmt_environment.update(module.environment_variables)

    if testing_environment.tmt and 'environment' in testing_environment.tmt:
        expected_tmt_environment.update(testing_environment.tmt['environment'])

    if expected_tmt_environment:
        run_mock.call_args.kwargs['env'] = expected_tmt_environment


@pytest.mark.parametrize('additional_options, additional_shared, clone_url, expected_tmt_reproducer_regex', [
        (  # Test case no. 1
            {},
            {
                'testing_farm_request': MagicMock(
                    environments_requested=[{}],
                    tmt=MagicMock(plan=None, plan_filter=None, path="some-tmt-root", test_filter=None, test_name='some-name'),  # noqa
                    plans=None)
            },
            SecretGitUrl('http://example.com/git/myproject'),
            r'''\# tmt reproducer
git clone --depth 1 -b myfix http://example.com/git/myproject testcode
cd testcode
dummytmt --root some-tmt-root run --all --verbose provision --how virtual --image guest-compose plan --name \^myfix\$ tests --name some-name'''  # noqa
        ),
        (  # Test case no. 2
            {'context-template-file': [os.path.abspath(os.path.join(ASSETS_DIR, 'context-template.yaml'))]},
            {},
            SecretGitUrl('http://example.com/git/myproject'),
            r'''\# tmt reproducer
git clone --depth 1 -b myfix http://example.com/git/myproject testcode
cd testcode
dummytmt --root some-tmt-root --context=@[a-zA-Z0-9\/\._-]+ run --all --verbose provision --how virtual --image guest-compose plan --name \^myfix\$'''  # noqa
        ),
        (  # Test case no. 3
            {},
            {},
            SecretGitUrl('http://username:secret@example.com/git/myproject'),
            r'''\# tmt reproducer
git clone --depth 1 -b myfix http://hidden@example.com/git/myproject testcode
cd testcode
dummytmt --root some-tmt-root run --all --verbose provision --how virtual --image guest-compose plan --name \^myfix\$'''  # noqa
        ),
    ]
)
def test_tmt_output_distgit(module, guest, monkeypatch, additional_options, additional_shared, clone_url,
                            expected_tmt_reproducer_regex, tmpdir):
    module._config = {**module._config, **additional_options}
    patch_shared(monkeypatch, module, additional_shared)

    # this doesn't appear anywhere in results.xml, but _run_plan() needs it
    module_dist_git = create_module(DistGit)[1]
    module_dist_git._repository = DistGitRepository(
        module_dist_git, 'some-package',
        clone_url=clone_url, branch='myfix'
    )
    module.glue.add_shared('dist_git_repository', module_dist_git)

    #  The module generates some files in CWD, so change it to one that will be cleaned up
    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        run_outputs = [
            '',       # git clone
            'myfix',  # git show-ref
            # '',     # git checkout  # TODO: somehow one of the `git` calls is skipped
            'plan1'   # tmt run discover plan --name plan1
        ]

        _set_run_outputs(
            m,
            *run_outputs,
            r'[{"name": "plan_name", "prepare": [{"how": "foo"}, {"how": "install", "exclude": ["exclude1", "exclude2"]}], "provision": {}}]')     # tmt plan export  # noqa
        schedule_entry = module.create_test_schedule([guest.environment])[0]

    schedule_entry.guest = guest

    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        # tmt run
        _set_run_outputs(m, 'dummy test done')

        module.run_test_schedule_entry(schedule_entry)

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
            [{
                'id': 'None:x86_64:plan1',
                'plan': 'plan1',
                'runner_capability': 'tmt',
                'testing_environment': TestingEnvironment('x86_64', snapshots=False),
                'work_dirpath': lambda workdir_path: workdir_path.startswith('work-plan'),
                'repodir': lambda repodir: re.match(r'.*test_create_schedule_tec2.*/git-myfix.*', repodir)
            }],
            [(logging.INFO, 'cloning repo http://example.com/git/myproject (branch not specified, ref myfix)'),
             (logging.INFO, 'looking for plans')]
        )
    ]
)
def test_create_schedule(module, monkeypatch, log, tec, expected_schedule, expected_logs, tmpdir):
    module_dist_git = create_module(DistGit)[1]
    module_dist_git._repository = DistGitRepository(
        module_dist_git, 'some-package',
        clone_url='http://example.com/git/myproject', ref='myfix'
    )
    module.glue.add_shared('dist_git_repository', module_dist_git)
    module._config['test-filter'] = 'filter1'

    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        _set_run_outputs(m,
                         '',       # git clone
                         '',       # git config #1
                         '',       # git config #2
                         '',       # git fetch
                         '',       # git checkout
                         'plan1',  # tmt plan ls
                         'plan1',   # tmt run discover plan --name plan1 test --filter filter1
                         '[]')     # tmt plan export

        schedule = module.create_test_schedule(tec)

    assert len(schedule) == len(expected_schedule)

    for entry, expected_entry in zip(schedule, expected_schedule):
        for key in expected_entry.keys():
            if callable(expected_entry[key]):
                assert expected_entry[key](entry.__dict__.get(key))
                continue
            assert entry.__dict__.get(key) == expected_entry[key]

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
            (
                gluetool.GlueError,
                "Did not find any plans. Command used 'dummytmt --root some-tmt-root plan ls --filter enabled:true'"
            )
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


def test_plans_from_git_filter(module, monkeypatch):
    repodir = 'foo'
    context_files = []
    testing_environment = TestingEnvironment('x86_64')
    filter = 'filter1'

    mock_output = MagicMock(exit_code=0, stdout='plan1', stderr='')
    mock_command_run = MagicMock(return_value=mock_output)
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt, 'Command', mock_command)

    module._plans_from_git(repodir, context_files, testing_environment, filter)

    mock_command.assert_called_once_with(['dummytmt', '--root', 'some-tmt-root', 'plan', 'ls', '--filter', 'filter1'])


@pytest.mark.parametrize('test_filter, test_name, expected_command', [
        (
            'filter1',
            None,
            [
                'dummytmt', '--root', 'some-tmt-root', 'run', '-e', '@tmt-environment-lan1.yaml', 'discover', 'plan',
                '--name', '^plan1$', 'test', '--filter', 'filter1'
            ]
        ),
        (
            None,
            'name1',
            [
                'dummytmt', '--root', 'some-tmt-root', 'run', '-e', '@tmt-environment-lan1.yaml', 'discover', 'plan',
                '--name', '^plan1$', 'test', '--name', 'name1'
            ]
        ),
        (
            'filter1',
            'name1',
            [
                'dummytmt', '--root', 'some-tmt-root', 'run', '-e', '@tmt-environment-lan1.yaml', 'discover', 'plan',
                '--name', '^plan1$', 'test', '--filter', 'filter1', '--name', 'name1'
            ]
        ),
    ]
)
def test_apply_test_filter(module, monkeypatch, tmpdir, test_filter, test_name, expected_command):
    repodir = 'foo'
    context_files = []
    testing_environment = TestingEnvironment('x86_64', variables={'variable1': 'value1'})

    mock_output = MagicMock(exit_code=0, stdout='', stderr='plan1')
    mock_command_run = MagicMock(return_value=mock_output)
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt, 'Command', mock_command)

    tmt_env_file = module._prepare_tmt_env_file(testing_environment, 'plan1', tmpdir)

    assert not module._is_plan_empty(
        plan='plan1',
        tmt_env_file=tmt_env_file,
        repodir=repodir,
        context_files=context_files,
        testing_environment=testing_environment,
        work_dirpath=tmpdir,
        test_filter=test_filter,
        test_name=test_name
    )

    mock_command.assert_called_once_with(expected_command)


def test_is_plan_empty(module, monkeypatch, log, tmpdir):
    repodir = 'foo'
    context_files = []
    testing_environment = TestingEnvironment('x86_64')

    # Plan with tests
    mock_output = MagicMock(exit_code=0, stdout='', stderr='plan1')
    mock_command_run = MagicMock(return_value=mock_output)
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt, 'Command', mock_command)

    module._is_plan_empty(
        plan='plan1',
        repodir=repodir,
        context_files=context_files,
        testing_environment=testing_environment,
        tmt_env_file='tmt-env-file',
        work_dirpath=tmpdir
    )

    mock_command.assert_called_once_with([
        'dummytmt',
        '--root',
        'some-tmt-root',
        'run',
        '-e',
        '@tmt-env-file',
        'discover',
        'plan',
        '--name',
        '^plan1$',
    ])

    # Empty plan
    mock_output = MagicMock(exit_code=0, stdout='', stderr='warning: No tests found, finishing plan.')
    mock_command_run = MagicMock(return_value=mock_output)
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt, 'Command', mock_command)

    assert module._is_plan_empty(
        plan='plan1',
        repodir=repodir,
        context_files=context_files,
        testing_environment=testing_environment,
        tmt_env_file='tmt-env-file',
        work_dirpath=tmpdir
    ) == True

    mock_command.assert_called_once_with([
        'dummytmt',
        '--root',
        'some-tmt-root',
        'run',
        '-e',
        '@tmt-env-file',
        'discover',
        'plan',
        '--name',
        '^plan1$',
    ])

    # No plans found
    mock_command_run = MagicMock(side_effect=gluetool.glue.GlueCommandError(
        cmd=['some-command'],
        output=MagicMock(stderr='No plans found')
    ))
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt, 'Command', mock_command)

    assert module._is_plan_empty(
        plan='plan1',
        repodir=repodir,
        context_files=context_files,
        testing_environment=testing_environment,
        tmt_env_file='tmt-env-file',
        work_dirpath=tmpdir
    ) == True

    mock_command.assert_called_once_with([
        'dummytmt',
        '--root',
        'some-tmt-root',
        'run',
        '-e',
        '@tmt-env-file',
        'discover',
        'plan',
        '--name',
        '^plan1$',
    ])


def test_plans_from_git_filter_from_request(module, monkeypatch):
    repodir = 'foo'
    context_files = []
    testing_environment = TestingEnvironment('x86_64')
    filter = None

    patch_shared(
        monkeypatch,
        module,
        {
            'testing_farm_request': MagicMock(
                environments_requested=[{}],
                tmt=MagicMock(plan=None, plan_filter='filter1', path="some-tmt-root"),
                plans=None
            )
        }
    )

    mock_output = MagicMock(exit_code=0, stdout='plan1', stderr='')
    mock_command_run = MagicMock(return_value=mock_output)
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt, 'Command', mock_command)

    module._plans_from_git(repodir, context_files, testing_environment, filter)

    mock_command.assert_called_once_with(['dummytmt', '--root', 'some-tmt-root', 'plan', 'ls', '--filter', 'filter1'])


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
def test_excludes(module, plan, expected):
    plan.update({'provision': [{'hardware': None}]})
    plan = gluetool.utils.create_cattrs_converter(prefer_attrib_converters=True).structure(plan, TMTPlan)
    assert plan.excludes() == expected


@pytest.mark.parametrize('clone_url, expected_clone_url', [
    (SecretGitUrl('http://example.com/git/myproject'), 'http://example.com/git/myproject'),
    (SecretGitUrl('http://username:secret@example.com/git/myproject'), 'http://hidden@example.com/git/myproject')
])
def test_tmt_output_copr(module, module_dist_git, guest, monkeypatch, tmpdir, clone_url, expected_clone_url):
    # install-copr-build module
    module_copr = create_module(InstallCoprBuild)[1]
    module_copr._config['log-dir-name'] = 'artifact-installation'
    module_copr._config['download-path'] = 'some-download-path'
    primary_task_mock = MagicMock()
    primary_task_mock.repo_url = 'http://copr/project.repo'
    primary_task_mock.rpm_urls = ['http://copr/project/one.rpm', 'http://copr/project/two.rpm']
    primary_task_mock.srpm_urls = ['http://copr/project/one.src.rpm', 'http://copr/project/two.src.rpm']
    primary_task_mock.rpm_names = ['one', 'two']
    primary_task_mock.project = 'owner/project'
    guest.environment.artifacts = [Artifact(type='fedora-copr-build', id='some-artifact')]

    patch_shared(monkeypatch, module_copr, {
        'primary_task': primary_task_mock,
        'tasks': [primary_task_mock],
    })

    # main test-schedule-tmt module
    module_dist_git._repository.clone_url = clone_url
    module.glue.add_shared('dist_git_repository', module_dist_git)

    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        _set_run_outputs(m,
                         '',       # git clone
                         '',       # git config #1
                         '',       # git config #2
                         '',       # git fetch
                         '',       # git checkout
                         'plan1',  # tmt plan ls
                         'plan1',  # tmt run discover plan --name plan1
                         '[]')     # tmt plan export
        schedule_entry = module.create_test_schedule([guest.environment])[0]

    # these are normally done by TestScheduleRunner, but running that is too involved for a unit test
    guest_setup_output = module_copr.setup_guest(
            guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION, log_dirpath=str(tmpdir))

    schedule_entry.guest = guest
    schedule_entry.guest_setup_outputs = {GuestSetupStage.ARTIFACT_INSTALLATION: guest_setup_output.unwrap()}

    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        # tmt run
        _set_run_outputs(m, 'dummy test done')
        module.run_test_schedule_entry(schedule_entry)

    with open(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-run.log')) as f:
        assert 'dummy test done\n' in f.read()

    # COPR installation actually happened
    guest.execute.assert_any_call(
        'dnf -y install --allowerasing http://copr/project/one.rpm http://copr/project/two.rpm')

    # ... and is shown in sut_install_commands.sh
    with open(os.path.join(tmpdir, 'artifact-installation-guest0', INSTALL_COMMANDS_FILE)) as f:
        assert f.read() == '''\
mkdir -pv some-download-path
curl -v http://copr/project.repo --retry 5 --output /etc/yum.repos.d/copr_build-owner_project-1.repo
cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" http://copr/project/one.rpm http://copr/project/two.rpm http://copr/project/one.src.rpm http://copr/project/two.src.rpm
dnf -y reinstall http://copr/project/one.rpm || true
dnf -y reinstall http://copr/project/two.rpm || true
dnf -y install --allowerasing http://copr/project/one.rpm http://copr/project/two.rpm
rpm -q one
rpm -q two
'''

    # ... and is pulled into the reproducer
    with open(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-reproducer.sh')) as f:
        assert f.read() == f'''# tmt reproducer
git clone {expected_clone_url} testcode
git -C testcode config --add remote.origin.fetch +refs/merge-requests/*:refs/remotes/origin/merge-requests/*
git -C testcode config --add remote.origin.fetch +refs/pull/*:refs/remotes/origin/pull/*
git -C testcode fetch {expected_clone_url} myfix:gluetool/myfix
git -C testcode checkout gluetool/myfix
cd testcode
curl -o guest-setup-0.sh -L {tmpdir}/artifact-installation-guest0/{INSTALL_COMMANDS_FILE}
dummytmt --root some-tmt-root run --until provision --verbose provision --how virtual --image guest-compose plan --name ^plan1$
dummytmt --root some-tmt-root run --last login < guest-setup-0.sh
dummytmt --root some-tmt-root run --last --since prepare'''


@pytest.mark.parametrize('clone_url, expected_clone_url', [
    (SecretGitUrl('http://example.com/git/myproject'), 'http://example.com/git/myproject'),
    (SecretGitUrl('http://username:secret@example.com/git/myproject'), 'http://hidden@example.com/git/myproject')
])
def test_tmt_output_koji(module, module_dist_git, guest, monkeypatch, tmpdir, clone_url, expected_clone_url):
    # install-koji-build-execute module
    module_koji = create_module(InstallKojiBuildExecute)[1]
    module_koji._config['log-dir-name'] = 'artifact-installation'

    guest.environment = TestingEnvironment(
        compose='guest-compose',
        arch='x86_64',
        artifacts=[Artifact(id='123', packages=None, type='fedora-koji-build')]
    )

    def evaluate_instructions_mock(workarounds, callbacks):
        callbacks['steps']('instructions', 'commands', workarounds, 'context')

    patch_shared(monkeypatch, module_koji, {}, callables={
        'testing_farm_request': lambda: MagicMock(),
        'evaluate_instructions': evaluate_instructions_mock,
    })

    module_koji.execute()

    # main test-schedule-tmt module
    module_dist_git._repository.clone_url = clone_url
    module.glue.add_shared('dist_git_repository', module_dist_git)

    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        _set_run_outputs(m,
                         '',       # git clone
                         '',       # git config #1
                         '',       # git config #2
                         '',       # git fetch
                         '',       # git checkout
                         'plan1',  # tmt plan ls
                         'plan1',   # tmt run discover plan --name plan1
                         ' - name: plan1\n'  # tmt plan export
                         '   provision:\n'
                         '     how: null\n'
                         '   prepare:\n'
                         '     how: somehow'
                         )
        schedule_entry = module.create_test_schedule([guest.environment])[0]

    # these are normally done by TestScheduleRunner, but running that is too involved for a unit test
    guest_setup_output = module_koji.setup_guest(
            guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION, log_dirpath=str(tmpdir))

    schedule_entry.guest = guest
    schedule_entry.guest_setup_outputs = {GuestSetupStage.ARTIFACT_INSTALLATION: guest_setup_output.unwrap()}

    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        # tmt run
        _set_run_outputs(m, 'dummy test done')

        module.run_test_schedule_entry(schedule_entry)

    with open(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-run.log')) as f:
        assert 'dummy test done\n' in f.read()

    # koji installation actually happened
    guest.execute.assert_any_call(r'''if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then dnf -y install --allowerasing $(cat rpms-list);else echo "Nothing to install, rpms-list is empty"; fi''')  # noqa

    # ... and is shown in sut_install_commands.sh
    with open(os.path.join(tmpdir, 'artifact-installation-guest0', INSTALL_COMMANDS_FILE)) as f:
        assert f.read() == r'''( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch src 123 || koji download-task --arch noarch --arch x86_64 --arch src 123 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123
ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\1 \0/" | awk "{print \$2}" | tee rpms-list
dnf -y reinstall $(cat rpms-list) || true
if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then dnf -y install --allowerasing $(cat rpms-list);else echo "Nothing to install, rpms-list is empty"; fi
if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\n' | xargs -d'\n' rpm -q;else echo 'Nothing to verify, rpms-list is empty'; fi
'''

    # ... and is pulled into the reproducer
    with open(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-reproducer.sh')) as f:
        assert f.read() == f'''# tmt reproducer
git clone {expected_clone_url} testcode
git -C testcode config --add remote.origin.fetch +refs/merge-requests/*:refs/remotes/origin/merge-requests/*
git -C testcode config --add remote.origin.fetch +refs/pull/*:refs/remotes/origin/pull/*
git -C testcode fetch {expected_clone_url} myfix:gluetool/myfix
git -C testcode checkout gluetool/myfix
cd testcode
curl -o guest-setup-0.sh -L {tmpdir}/artifact-installation-guest0/{INSTALL_COMMANDS_FILE}
dummytmt --root some-tmt-root run --until provision --verbose provision --how virtual --image guest-compose plan --name ^plan1$
dummytmt --root some-tmt-root run --last login < guest-setup-0.sh
dummytmt --root some-tmt-root run --last --since prepare'''


TMT_PLANS = ['''
- name: some-plan
  provision:
    - hardware: null
''', '''
- name: some-plan
  provision:
    - hardware: null
  prepare:
    how: somehow
    exclude:
      - exclude1
      - exclude2
''', '''
- name: some-plan
  provision:
    - hardware: null
  prepare:
    - how: somehow1
      exclude:
        - prep1_exclude1
        - prep1_exclude2
    - how: somehow2
      exclude:
        - prep2_exclude1
        - prep2_exclude2
''']

EMPTY_PROVISION_STEP = TMTPlanProvision(hardware=None, kickstart=None, watchdog_dispatch_delay=None,
                                        watchdog_period_delay=None)


@pytest.mark.parametrize('tf_request, mock_output, context_files, tec, expected_command, expected_plan', [
    (None, MagicMock(stdout=TMT_PLANS[0]), [], TestingEnvironment(),
     ['dummytmt', 'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     TMTPlan(name='some-plan', provision=[EMPTY_PROVISION_STEP], prepare=[])),
    (MagicMock(tmt=MagicMock(path='some-tmt-root')), MagicMock(stdout=TMT_PLANS[0]), [], TestingEnvironment(),
     ['dummytmt', '--root', 'some-tmt-root', 'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     TMTPlan(name='some-plan', provision=[EMPTY_PROVISION_STEP], prepare=[])),
    (None, MagicMock(stdout=TMT_PLANS[1]), ['file1', 'file 2'], TestingEnvironment(),
     ['dummytmt', '--context=@file1', '--context=@file 2', 'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     TMTPlan(name='some-plan', provision=[EMPTY_PROVISION_STEP],
             prepare=[TMTPlanPrepare(how='somehow', exclude=['exclude1', 'exclude2'])])),
    (None, MagicMock(stdout=TMT_PLANS[1]), ['file1', 'file 2'], TestingEnvironment(tmt={'context': {'foo': 'bar'}}),
     ['dummytmt', '--context=@file1', '--context=@file 2', '-c', 'foo=bar',
         'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     TMTPlan(name='some-plan', provision=[EMPTY_PROVISION_STEP],
             prepare=[TMTPlanPrepare(how='somehow', exclude=['exclude1', 'exclude2'])])),
    (None, MagicMock(stdout=TMT_PLANS[2]), [], TestingEnvironment(),
     ['dummytmt', 'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     TMTPlan(name='some-plan', provision=[EMPTY_PROVISION_STEP],
             prepare=[TMTPlanPrepare(how='somehow1', exclude=['prep1_exclude1', 'prep1_exclude2']),
                      TMTPlanPrepare(how='somehow2', exclude=['prep2_exclude1', 'prep2_exclude2'])])),
    (None, MagicMock(stdout='[]'), [], TestingEnvironment(),
     ['dummytmt', 'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     None),
])
def test_export(monkeypatch, module, tf_request, mock_output, context_files, tec, expected_command, expected_plan):
    patch_shared(monkeypatch, module, {'testing_farm_request': tf_request})
    mock_command_run = MagicMock(return_value=mock_output)
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt, 'Command', mock_command)

    plan = module.export_plan('some-repo', 'some-plan', context_files, 'tmt-env-file', tec)
    assert plan == expected_plan

    mock_command.assert_called_once_with(expected_command)


TMT_EXPORTED_PLANS = [
    # single_provision_phase
    '''
    - name: plan1
      provision:
        - name: default-0
          how: virtual
    ''',
    # single_provision_phase_with_hardware
    '''
    - name: plan1
      provision:
        - name: default-0
          how: virtual
          hardware:
            tpm:
              version: '2'
    ''',
    # single_provision_phase_with_kickstart
    '''
    - name: plan1
      provision:
        - name: default-0
          how: virtual
          kickstart:
            script: some-script
            metadata: some-metadata
    ''',
    # multiple_provision_phases
    '''
    - name: plan1
      provision:
        - name: default-0
          how: virtual
        - name: default-1
          how: virtual
    ''',
]


@pytest.mark.parametrize('exported_plan, expected_environment, expected_exception', [
        # single_provision_phase
        (
            TMT_EXPORTED_PLANS[0],
            TestingEnvironment(
                arch='x86_64',
                excluded_packages=[],
                snapshots=False
            ),
            None
        ),
        # single_provision_phase_with_hardware
        (
            TMT_EXPORTED_PLANS[1],
            TestingEnvironment(
                arch='x86_64',
                excluded_packages=[],
                hardware={
                    'tpm': {
                        'version': '2'
                    }
                },
                snapshots=False
            ),
            None
        ),
        # single_provision_phase_with_hardware
        (
            TMT_EXPORTED_PLANS[2],
            TestingEnvironment(
                arch='x86_64',
                excluded_packages=[],
                kickstart={
                    'script': 'some-script',
                    'metadata': 'some-metadata',
                },
                snapshots=False
            ),
            None
        ),
        # multiple_provision_phases
        (
            TMT_EXPORTED_PLANS[3],
            None,
            (gluetool.GlueError, 'Multiple provision phases not supported, refusing to continue.')
        ),
    ],
    ids=[
        'single_provision_phase',
        'single_provision_phase_with_hardware',
        'single_provision_phase_with_kickstart',
        'multiple_provision_phases'
    ]
)
def test_tmt_plan_export(module, monkeypatch, exported_plan, expected_environment, expected_exception, tmpdir):
    module_dist_git = create_module(DistGit)[1]
    module_dist_git._repository = DistGitRepository(
        module_dist_git, 'some-package',
        clone_url='http://example.com/git/myproject', ref='myfix'
    )
    module.glue.add_shared('dist_git_repository', module_dist_git)

    with monkeypatch.context() as m:
        m.chdir(tmpdir)
        _set_run_outputs(m,
                         '',       # git clone
                         '',       # git config #1
                         '',       # git config #2
                         '',       # git fetch
                         '',       # git checkout
                         'plan1',  # tmt plan ls
                         'plan1',  # tmt run discover plan --name plan1
                         exported_plan)     # tmt plan export

        if expected_exception:
            with pytest.raises(expected_exception[0], match=expected_exception[1]):
                schedules = module.create_test_schedule([TestingEnvironment('x86_64')])
            return

        schedules = module.create_test_schedule([TestingEnvironment('x86_64')])

    if not expected_exception:
        assert schedules[0].testing_environment == expected_environment
