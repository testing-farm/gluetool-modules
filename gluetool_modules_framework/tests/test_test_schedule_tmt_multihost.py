# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import base64
import os
import shutil
import re
from mock import MagicMock
from typing import List

import pytest
import logging

import gluetool
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
import gluetool_modules_framework.testing.test_schedule_tmt_multihost
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
from gluetool_modules_framework.testing.test_schedule_tmt_multihost import gather_plan_results, TestScheduleEntry
from gluetool_modules_framework.testing.test_schedule_tmt_multihost import (gather_plan_results, TestScheduleEntry,
                                                                            TMTPlan, TMTPlanProvision)
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
    module = create_module(gluetool_modules_framework.testing.test_schedule_tmt_multihost.TestScheduleTMTMultihost)[1]
    module._config['command'] = 'dummytmt'
    module._config['reproducer-comment'] = '# tmt reproducer'
    module._config['tmt-run-options'] = '-ddddvvv,--log-topic=cli-invocations'
    module._config['result-log-max-size'] = 10
    patch_shared(
        monkeypatch,
        module,
        {
            'testing_farm_request': MagicMock(
                environments_requested=[{}],
                tmt=MagicMock(plan=None, plan_filter=None, path="some-tmt-root", test_filter=None, test_name=None),
                plans=None),
            'artemis_api_options': {
                'api-url': 'http://artemis.example.com/v0.0.56',
                'api-version': '0.0.56',
                'ssh-key': 'master-key',
                'key': 'path/to/key',
                'post-install-script': 'echo hello',
                'skip-prepare-verify-ssh': True,
                'ready-timeout': 300,
                'ready-tick': 3,
                'api-call-timeout': 60,
                'user-data-vars-template-file': None
            }
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
    assert isinstance(module, gluetool_modules_framework.testing.test_schedule_tmt_multihost.TestScheduleTMTMultihost)


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/testing/test_schedule_tmt_multihost.py',
                   'TestScheduleTMTMultihost')


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
        _load_assets('multihost-passed'),
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
    import cattrs.errors
    import traceback
    try:
        outcome, results, guests = gather_plan_results(module, schedule_entry, ASSETS_DIR)
    except cattrs.errors.IterableValidationError as e:
        traceback.print_exc()
        print(e.exceptions)
        raise e

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
    orig_gather_plan_results = gluetool_modules_framework.testing.test_schedule_tmt_multihost.gather_plan_results

    def inject_gather_plan_results(module, schedule_entry, work_dir, recognize_errors=False):
        shutil.copytree(os.path.join(ASSETS_DIR, 'passed'), os.path.join(work_dir, 'passed'))
        return orig_gather_plan_results(module, schedule_entry, work_dir, recognize_errors=recognize_errors)

    # run tmt with the mock plan
    with monkeypatch.context() as m:
        _set_run_outputs(m, 'dummy test done')
        try:
            gluetool_modules_framework.testing.test_schedule_tmt_multihost.gather_plan_results = inject_gather_plan_results
            module.run_test_schedule_entry(schedule_entry)
        finally:
            gluetool_modules_framework.testing.test_schedule_tmt_multihost.gather_plan_results = orig_gather_plan_results

    # generate results.xml
    test_suite = TestSuite(name='some-suite', result='some-result')
    module.shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)
    # Second call shouldn't produce duplicated results
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
    assert testcase_docs.logs[2].name == 'journal.txt'
    assert testcase_docs.logs[2].href.endswith('/passed/execute/logs/tests/core/docs/journal.txt')
    assert testcase_docs.logs[3].name == 'testout.log'
    assert testcase_docs.logs[3].href.endswith('/passed/execute/logs/tests/core/docs/out.log')

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
    orig_gather_plan_results = gluetool_modules_framework.testing.test_schedule_tmt_multihost.gather_plan_results

    def inject_gather_plan_results(module, schedule_entry, work_dir, recognize_errors=False):
        shutil.copytree(os.path.join(ASSETS_DIR, 'passed'), os.path.join(work_dir, 'passed'))
        return orig_gather_plan_results(module, schedule_entry, work_dir, recognize_errors=recognize_errors)

    # run tmt with the mock plan
    with monkeypatch.context() as m:
        _set_run_outputs(m, 'dummy test done')
        try:
            gluetool_modules_framework.testing.test_schedule_tmt_multihost.gather_plan_results = inject_gather_plan_results
            module.run_test_schedule_entry(schedule_entry)
        finally:
            gluetool_modules_framework.testing.test_schedule_tmt_multihost.gather_plan_results = orig_gather_plan_results

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
            TestingEnvironment('x86_64', 'rhel-9', pool='foo'),
            '''# tmt reproducer
dummytmt --root some-tmt-root run --all --id {work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^plan1$ tests --name some-name provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image rhel-9 --arch x86_64 --pool foo --skip-prepare-verify-ssh --post-install-script echo hello''',  # noqa
            None,
            None
        ),
        (  # local - provision done by tmt
            {'how': 'local'},  # NOTE: option does not exist, used only to signal usage of StaticLocalhostGuest
            {},
            TestingEnvironment('x86_64', 'rhel-9'),
            '''# tmt reproducer
dummytmt --root some-tmt-root run --all --id {work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^plan1$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image rhel-9 --arch x86_64 --skip-prepare-verify-ssh --post-install-script echo hello''',  # noqa
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
dummytmt --root some-tmt-root run --all --id {work_dirpath} -ddddvvv --log-topic=cli-invocations -e @tmt-environment-lan1.yaml plan --name ^plan1$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image rhel-9 --arch x86_64 --skip-prepare-verify-ssh --post-install-script echo hello""",  # noqa
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
dummytmt --root some-tmt-root -c distro=rhel -c trigger=push run --all --id {work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^plan1$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image rhel-9 --arch x86_64 --skip-prepare-verify-ssh --post-install-script echo hello""",  # noqa
            None,
            None
        ),
        (  # with tmt process environment variables from options only
            {
                'environment-variables': [
                    'VARIABLE1=VAL1'
                ]
            },
            {},
            TestingEnvironment('x86_64', 'rhel-9'),
            """# tmt reproducer
dummytmt --root some-tmt-root run --all --id {work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^plan1$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image rhel-9 --arch x86_64 --skip-prepare-verify-ssh --post-install-script echo hello""",  # noqa
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
dummytmt --root some-tmt-root run --all --id {work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^plan1$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image rhel-9 --arch x86_64 --skip-prepare-verify-ssh --post-install-script echo hello""",  # noqa

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
dummytmt --root some-tmt-root run --all --id {tmpdir}/{work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^plan1$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image rhel-9 --arch x86_64 --skip-prepare-verify-ssh --post-install-script echo hello""",  # noqa

            None,
            (gluetool.glue.GlueError, "Environment variable 'VARIABLE2' is not allowed to be exposed to the tmt process")
        ),
        (  # with user data
            {},
            {},
            TestingEnvironment('x86_64', 'rhel-9',
                               settings={'provisioning': {'tags': {'Foo': 'Bar', "Baz Baz": "Boo Boo"}}}),
            """# tmt reproducer
dummytmt --root some-tmt-root run --all --id {work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^plan1$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image rhel-9 --arch x86_64 --skip-prepare-verify-ssh --post-install-script echo hello --user-data Foo=Bar --user-data Baz Baz=Boo Boo""",  # noqa
            None,
            None
        ),
        (  # with tmt prepare extra arguments
            {},
            {},
            TestingEnvironment('x86_64', 'rhel-9', tmt={
                'extra_args': {
                    'prepare': [
                        '--args1',
                        '--args2'
                    ],
                    'discover': [
                        '--args1',
                        '--args2'
                    ],
                    'finish': [
                        '--args1',
                        '--args2 --args3'
                    ]
                }
            }),
            """# tmt reproducer
dummytmt --root some-tmt-root run --all --id {work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^plan1$ discover --args1 discover --args2 prepare --args1 prepare --args2 provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image rhel-9 --arch x86_64 --skip-prepare-verify-ssh --post-install-script echo hello finish --args1 finish --args2 --args3""",  # noqa
            None,
            None
        ),

    ],
    ids=[
        'virtual', 'local', 'variables', 'tmt_context',
        'tmt_process_environment_options_only', 'tmt_process_environment', 'tmt_process_environment_not_accepted', 'user_data',
        'tmt_extra_args'
    ]
)
def test_tmt_output_dir(
    module, monkeypatch, tmpdir,
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

    schedule_entry.tmt_env_file = module._prepare_tmt_env_file(testing_environment, 'plan1', tmpdir)
    schedule_entry.work_dirpath = os.path.join(tmpdir, 'some-workdir')
    os.mkdir(schedule_entry.work_dirpath)

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
        assert c == expected_reproducer.format(tmpdir=tmpdir, work_dirpath=schedule_entry.work_dirpath)

    with open(os.path.join(tmpdir, schedule_entry.work_dirpath, 'tmt-reproducer.sh')) as f:
        c = f.read()
        print(c)
        print(expected_reproducer)
        assert c == expected_reproducer.format(tmpdir=tmpdir, work_dirpath=schedule_entry.work_dirpath)

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


@pytest.mark.parametrize('additional_options, additional_shared, clone_url, expected_tmt_reproducer', [
        (  # Test case no. 1
            {},
            {
                'testing_farm_request': MagicMock(
                    environments_requested=[{}],
                    tmt=MagicMock(plan=None, plan_filter=None, path="some-tmt-root", test_filter=None, test_name='some-name'),  # noqa
                    plans=None)
            },
            SecretGitUrl('http://example.com/git/myproject'),
            r'''# tmt reproducer
git clone --depth 1 -b myfix http://example.com/git/myproject testcode
cd testcode
dummytmt --root some-tmt-root run --all --id {tmpdir}/{work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^myfix$ tests --name some-name provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image guest-compose --skip-prepare-verify-ssh --post-install-script echo hello'''  # noqa

        ),
        (  # Test case no. 2
            {'context-template-file': [os.path.abspath(os.path.join(ASSETS_DIR, 'context-template.yaml'))]},
            {},
            SecretGitUrl('http://example.com/git/myproject'),
            r'''# tmt reproducer
git clone --depth 1 -b myfix http://example.com/git/myproject testcode
cd testcode
dummytmt --root some-tmt-root run --all --id {tmpdir}/{work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^myfix$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image guest-compose --skip-prepare-verify-ssh --post-install-script echo hello'''  # noqa

        ),
        (  # Test case no. 3
            {},
            {},
            SecretGitUrl('http://username:secret@example.com/git/myproject'),
            r'''# tmt reproducer
git clone --depth 1 -b myfix http://hidden@example.com/git/myproject testcode
cd testcode
dummytmt --root some-tmt-root run --all --id {tmpdir}/{work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^myfix$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image guest-compose --skip-prepare-verify-ssh --post-install-script echo hello'''  # noqa
        ),
    ]
)
def test_tmt_output_distgit(module, guest, monkeypatch, additional_options, additional_shared, clone_url,
                            expected_tmt_reproducer, tmpdir):
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
            'plan1',   # tmt run discover plan --name plan1
        ]

        _set_run_outputs(
            m,
            *run_outputs,
            r'[{"name": "plan_name", "prepare": [{"how": "foo"}, {"how": "install"}], "provision": {}}]'     # tmt plan export  # noqa
        )

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
        assert expected_tmt_reproducer.format(tmpdir=tmpdir, work_dirpath=schedule_entry.work_dirpath) == f.read()

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
        print(schedule.__dict__)
        print(expected_schedule)
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
    testing_environment = TestingEnvironment('x86_64')
    filter = None
    with monkeypatch.context() as m:
        _set_run_outputs(m, tmt_plan_ls)

        if expected_exception:
            with pytest.raises(expected_exception[0], match=expected_exception[1]):
                module._plans_from_git(repodir, testing_environment, filter)
        else:
            module._plans_from_git(repodir, testing_environment, filter)

    for log_level, log_message in expected_logs:
        assert log.match(levelno=log_level, message=log_message)


def test_plans_from_git_filter(module, monkeypatch):
    repodir = 'foo'
    testing_environment = TestingEnvironment('x86_64')
    filter = 'filter1'

    mock_output = MagicMock(exit_code=0, stdout='plan1', stderr='')
    mock_command_run = MagicMock(return_value=mock_output)
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt_multihost, 'Command', mock_command)

    module._plans_from_git(repodir, testing_environment, filter)

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
    testing_environment = TestingEnvironment('x86_64',  variables={'variable1': 'value1'})

    mock_output = MagicMock(exit_code=0, stdout='', stderr='plan1')
    mock_command_run = MagicMock(return_value=mock_output)
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt_multihost, 'Command', mock_command)

    tmt_env_file = module._prepare_tmt_env_file(testing_environment, 'plan1', tmpdir)

    assert not module._is_plan_empty(
        plan='plan1',
        tmt_env_file=tmt_env_file,
        repodir=repodir,
        testing_environment=testing_environment,
        work_dirpath=tmpdir,
        test_filter=test_filter,
        test_name=test_name
    )

    mock_command.assert_called_once_with(expected_command)


def test_is_plan_empty(module, monkeypatch, tmpdir):
    repodir = 'foo'
    testing_environment = TestingEnvironment('x86_64')

    # Plan with tests
    mock_output = MagicMock(exit_code=0, stdout='', stderr='plan1')
    mock_command_run = MagicMock(return_value=mock_output)
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt_multihost, 'Command', mock_command)

    module._is_plan_empty(
        plan='plan1',
        repodir=repodir,
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
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt_multihost, 'Command', mock_command)

    assert module._is_plan_empty(
        plan='plan1',
        repodir=repodir,
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
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt_multihost, 'Command', mock_command)

    module._plans_from_git(repodir, testing_environment, filter)

    mock_command.assert_called_once_with(['dummytmt', '--root', 'some-tmt-root', 'plan', 'ls', '--filter', 'filter1'])


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
dummytmt --root some-tmt-root run --all --id {tmpdir}/{schedule_entry.work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^plan1$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image guest-compose --skip-prepare-verify-ssh --post-install-script echo hello'''


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
        artifacts=[Artifact(id='123', packages=None, type='fedora-koji-build')],
        pool='foo'
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
dummytmt --root some-tmt-root run --all --id {tmpdir}/{schedule_entry.work_dirpath} -ddddvvv --log-topic=cli-invocations plan --name ^plan1$ provision -h artemis --update-missing --allowed-how container|artemis -k master-key --api-url http://artemis.example.com/v0.0.56 --api-version 0.0.56 --keyname path/to/key --provision-timeout 300 --provision-tick 3 --api-timeout 60 --image guest-compose --arch x86_64 --pool foo --skip-prepare-verify-ssh --post-install-script echo hello'''


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

EMPTY_PROVISION_STEP = TMTPlanProvision(how=None)


@pytest.mark.parametrize('tf_request, mock_output, tec, expected_command, expected_plan', [
    (None, MagicMock(stdout=TMT_PLANS[0]), TestingEnvironment(),
     ['dummytmt', 'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     TMTPlan(name='some-plan', provision=[EMPTY_PROVISION_STEP])),
    (MagicMock(tmt=MagicMock(path='some-tmt-root')), MagicMock(stdout=TMT_PLANS[0]), TestingEnvironment(),
     ['dummytmt', '--root', 'some-tmt-root', 'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     TMTPlan(name='some-plan', provision=[EMPTY_PROVISION_STEP])),
    (None, MagicMock(stdout=TMT_PLANS[1]), TestingEnvironment(),
     ['dummytmt', 'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     TMTPlan(name='some-plan', provision=[EMPTY_PROVISION_STEP])),
    (None, MagicMock(stdout=TMT_PLANS[2]), TestingEnvironment(),
     ['dummytmt', 'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     TMTPlan(name='some-plan', provision=[EMPTY_PROVISION_STEP])),
    (None, MagicMock(stdout='[]'), TestingEnvironment(),
     ['dummytmt', 'plan', 'export', '-e', '@tmt-env-file', '^some\\-plan$'],
     None),
])
def test_export(monkeypatch, module, tf_request, tec, mock_output, expected_command, expected_plan):
    patch_shared(monkeypatch, module, {'testing_farm_request': tf_request})
    mock_command_run = MagicMock(return_value=mock_output)
    mock_command = MagicMock(return_value=MagicMock(run=mock_command_run))
    monkeypatch.setattr(gluetool_modules_framework.testing.test_schedule_tmt_multihost, 'Command', mock_command)

    plan = module.export_plan('some-repo', 'some-plan', 'tmt-env-file', tec)
    assert plan == expected_plan

    mock_command.assert_called_once_with(expected_command)


TMT_EXPORTED_PLANS = [
    # single_provision_phase
    '''
    - name: plan1
      provision:
        - name: default-0
    ''',
    # single_provision_phase_with_hardware
    '''
    - name: plan1
      provision:
        - name: default-0
          how: container
          hardware:
            tpm:
              version: '2'
    ''',
    # single_provision_phase_with_kickstart
    '''
    - name: plan1
      provision:
        - name: default-0
          kickstart:
            script: some-script
            metadata: some-metadata
    ''',
    # multiple_provision_phases
    '''
    - name: plan1
      provision:
        - name: default-0
          how: container
        - name: default-1
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
                snapshots=False
            ),
            None
        ),
        # single_provision_phase_with_kickstart
        (
            TMT_EXPORTED_PLANS[2],
            TestingEnvironment(
                arch='x86_64',
                excluded_packages=[],
                snapshots=False,
            ),
            None
        ),
        # multiple_provision_phases
        (
            TMT_EXPORTED_PLANS[3],
            TestingEnvironment(
                arch='x86_64',
                excluded_packages=[],
                snapshots=False
            ),
            None
        ),
    ],
    ids=[
        'single_provision_phase',
        'single_provision_phase_with_hardware',
        'single_provision_phase_with_kickstart',
        'multiple_provision_phases',
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


def test_serialize_test_schedule_entry_large_artifact(module, module_dist_git, guest, monkeypatch, tmpdir, log):
    # this doesn't appear anywhere in results.xml, but _run_plan() needs it
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
    schedule_entry.work_dirpath = os.path.join(tmpdir, 'some-workdir')
    os.mkdir(schedule_entry.work_dirpath)

    # gather_plan_results() is called in _run_plan() right after calling tmt; we need to inject
    # writing results.yaml in between, which we can't do with a mock
    orig_gather_plan_results = gluetool_modules_framework.testing.test_schedule_tmt_multihost.gather_plan_results

    # Test with 20MiB log size
    log_size = 20*1024*1024
    max_size = module._config['result-log-max-size']*1024*1024

    def inject_gather_plan_results(module, schedule_entry, work_dir, recognize_errors=False):
        shutil.copytree(os.path.join(ASSETS_DIR, 'passed'), os.path.join(work_dir, 'passed'))
        # generate 20MiB output log, note that base encoding does not preserve desired length
        with open(os.path.join(work_dir, 'passed/execute/logs/tests/core/docs/out.log'), 'wb') as log:
            with open('/dev/urandom', 'rb') as random:
                log.write(base64.b64encode(random.read(log_size))[0:log_size])
        return orig_gather_plan_results(module, schedule_entry, work_dir, recognize_errors=recognize_errors)

    # run tmt with the mock plan
    with monkeypatch.context() as m:
        _set_run_outputs(m, 'dummy test done')
        try:
            gluetool_modules_framework.testing.test_schedule_tmt_multihost.gather_plan_results = inject_gather_plan_results
            module.run_test_schedule_entry(schedule_entry)
        finally:
            gluetool_modules_framework.testing.test_schedule_tmt_multihost.gather_plan_results = orig_gather_plan_results

    # generate results.xml
    test_suite = TestSuite(name='some-suite', result='some-result')

    module.shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)

    preamble = 'Output too large, limiting output to last {} MiB.\n\n'.format(module._config['result-log-max-size'])

    assert test_suite.test_cases[0].system_out[0][0:len(preamble)] == preamble
    assert len(test_suite.test_cases[0].system_out[0])-len(preamble) == max_size

    assert log.match(
        levelno=logging.WARN,
        message="Artifact '{}' is too large - {} bytes, limiting output to {} bytes".format(
            test_suite.test_cases[0].logs[3].href,
            log_size,
            max_size
        )
    )

    shutil.rmtree(schedule_entry.work_dirpath)
