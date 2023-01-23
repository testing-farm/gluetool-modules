# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import re
import os
import stat
import sys
import tempfile

import enum
import six

import gluetool
from gluetool import GlueError, GlueCommandError, Module
from gluetool.action import Action
from gluetool.log import Logging, format_blob, log_blob, log_dict
from gluetool.log import ContextAdapter, LoggingFunctionType  # Ignore PyUnusedCodeBear
from gluetool.utils import Command, dict_update, from_yaml, load_yaml, new_xml_element

from gluetool_modules_framework.infrastructure.static_guest import StaticLocalhostGuest
from gluetool_modules_framework.libs import create_inspect_callback, sort_children
from gluetool_modules_framework.libs.artifacts import artifacts_location
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.test_schedule import TestSchedule, TestScheduleResult, TestScheduleEntryOutput, \
    TestScheduleEntryStage, TestScheduleEntryAdapter
from gluetool_modules_framework.libs.test_schedule import TestScheduleEntry as BaseTestScheduleEntry


# Type annotations
from typing import cast, Any, Callable, Dict, List, Optional, Tuple  # noqa

# Type annotations
from typing import Any, Dict, List, NamedTuple, Optional  # noqa

# TMT run log file
TMT_LOG = 'tmt-run.log'
TMT_REPRODUCER = 'tmt-reproducer.sh'

# File with environment variables
TMT_ENV_FILE = 'tmt-environment-{}.yaml'

CONTEXT_FILENAME_PREFIX = 'context-'
CONTEXT_FILENAME_SUFFIX = '.yaml'

# Weight of a test result, used to count the overall result. Higher weight has precendence
# when counting the overall result. See https://tmt.readthedocs.io/en/latest/spec/steps.html#execute
RESULT_WEIGHT = {
    'pass': 0,
    'info': 0,
    'fail': 1,
    'warn': 1,
    'error': 2,
}

# Map tmt results to our expected results
#
# Note that we comply to
#
#     https://pagure.io/fedora-ci/messages/blob/master/f/schemas/test-complete.yaml
#
# TMT recognized `error` for a test, but we do not translate it to a TestScheduleResult
# error, as this error is user facing, nothing we can do about it to fix it, it is his problem.
#
# For more context see: https://pagure.io/fedora-ci/messages/pull-request/86
RESULT_OUTCOME = {
    'pass': 'passed',
    'info': 'info',
    'fail': 'failed',
    'warn': 'needs_inspection',
    'error': 'error'
}

# Result weight to TestScheduleResult outcome
#
#     https://tmt.readthedocs.io/en/latest/overview.html#exit-codes
#
# All tmt errors are connected to tests or config, so only higher return code than 3
# is treated as error
PLAN_OUTCOME = {
    0: TestScheduleResult.PASSED,
    1: TestScheduleResult.FAILED,
    2: TestScheduleResult.FAILED,
}

# Result weight to TestScheduleResult outcome
#
#     https://tmt.readthedocs.io/en/latest/overview.html#exit-codes
#
# All tmt errors are connected to tests or config, so only higher return code than 3
# is treated as error
PLAN_OUTCOME_WITH_ERROR = {
    0: TestScheduleResult.PASSED,
    1: TestScheduleResult.FAILED,
    2: TestScheduleResult.ERROR,
}

# Results YAML file, contains list of test run results, relative to plan workdir
RESULTS_YAML = "execute/results.yaml"

#: Represents a test output artifact (in particular, log files)
TestArtifact = NamedTuple('TestArtifact', (
    ('name', str),
    ('path', str)
))

#: Represents a test run result
#:
#: :ivar name: name of the test.
#: :ivar result: test result.
#: :ivar artifacts: artifacts/log files declared by the test
TestResult = NamedTuple('TestResult', (
    ('name', str),
    ('result', str),
    ('artifacts', List[TestArtifact]),
))


# https://tmt.readthedocs.io/en/latest/overview.html#exit-codes
class TMTExitCodes(enum.IntEnum):
    TESTS_PASSED = 0
    TESTS_FAILED = 1
    TESTS_ERROR = 2
    RESULTS_MISSING = 3


class TestScheduleEntry(BaseTestScheduleEntry):
    @staticmethod
    def construct_id(tec, plan=None):
        # type: (TestingEnvironment, Optional[str]) -> str

        entry_id = '{}:{}'.format(tec.compose, tec.arch)

        if plan is None:
            return entry_id

        return '{}:{}'.format(entry_id, plan)

    def __init__(self, logger, tec, plan, repodir, excludes):
        # type: (ContextAdapter, TestingEnvironment, str, str, List[str]) -> None
        """
        Test schedule entry, suited for use with TMT runners.

        :param ContextAdapter logger: logger used as a parent of this entry's own logger.
        :param str plan: Name of the plan.
        """

        # As the ID use the test plan name
        super(TestScheduleEntry, self).__init__(
            logger,
            TestScheduleEntry.construct_id(tec, plan),
            'tmt'
        )

        self.testing_environment = tec
        self.plan = plan
        self.work_dirpath = None  # type: Optional[str]
        self.results = None  # type: Any
        self.repodir = repodir  # type: str
        self.excludes = excludes  # type: List[str]
        self.tmt_reproducer = []  # type: List[str]
        self.tmt_reproducer_filepath = None  # type: Optional[str]

        self.context_files = []  # type: List[str]

    def log_entry(self, log_fn=None):
        # type: (Optional[LoggingFunctionType]) -> None

        log_fn = log_fn or self.debug

        super(TestScheduleEntry, self).log_entry(log_fn=log_fn)

        log_fn('plan: {}'.format(self.plan))


#: Represents run of one plan and results of this run.
#:
#: :ivar str name: name of the plan.
#: :ivar libs.test_schedule.TestScheduleEntry schedule_entry: test schedule entry the task belongs to.
#: :ivar result: overall result of the plan - i.e. agregation of all test results
#: :ivar dict results: result of the plan run, as reported by tmt.
PlanRun = NamedTuple('PlanRun', (
    ('name', str),
    ('schedule_entry', TestScheduleEntry),
    ('result', str),
    ('results', List[TestResult])
))


def gather_plan_results(schedule_entry, work_dir, recognize_errors=False):
    # type: (TestScheduleEntry, str, bool) -> Tuple[TestScheduleResult, List[TestResult]]
    """
    Extracts plan results from tmt logs.

    :param TestScheduleEntry schedule_entry: Plan schedule entry.
    :param str work_dir: Plan working directory.
    :rtype: tuple
    :returns: A tuple with overall_result and results detected for the plan.
    """
    test_results = []  # type: List[TestResult]

    # TMT uses plan name as a relative directory to the working directory, but
    # plan start's with '/' character, strip it so we can use it with os.path.join
    plan_path = schedule_entry.plan[1:]

    results_yaml = os.path.join(work_dir, plan_path, RESULTS_YAML)

    if not os.path.exists(results_yaml):
        schedule_entry.warn("Could not find results file '{}' containing tmt results".format(results_yaml), sentry=True)
        return TestScheduleResult.ERROR, test_results

    # load test results from `results.yaml` which is created in tmt's execute step
    # https://tmt.readthedocs.io/en/latest/spec/steps.html#execute
    try:
        results = load_yaml(results_yaml)
        log_dict(schedule_entry.debug, "loaded results from '{}'".format(results_yaml), results)

    except GlueError as error:
        schedule_entry.warn('Could not load results.yaml file: {}'.format(error))
        return TestScheduleResult.ERROR, results

    # Something went wrong, there should be results. There were tests, otherwise we wouldn't
    # be running `tmt run`, but where are results? Reporting an error...
    if not results:
        schedule_entry.warn('Could not find any results in results.yaml file')
        return TestScheduleResult.ERROR, test_results

    # iterate through all the test results and create TestResult for each
    for name, data in six.iteritems(results):

        # translate result outcome
        try:
            outcome = RESULT_OUTCOME[data['result']]
        except KeyError:
            schedule_entry.warn("Encountered invalid result '{}' in runner results".format(data['result']))
            return TestScheduleResult.ERROR, results

        # log can be a string or a list
        logs = data['log']
        if not isinstance(data['log'], list):
            logs = [logs]

        artifacts_dir = os.path.join(work_dir, plan_path, 'execute')
        artifacts = []

        # attach the artifacts directory itself, useful for browsing;
        # usually all artifacts are in the same dir
        if logs:
            artifacts.append(TestArtifact(
                'log_dir',
                os.path.join(artifacts_dir, os.path.dirname(logs[0]))
            ))

            # the first log is the main one for developers, traditionally called "testout.log"
            testout = logs.pop(0)
            artifacts.append(TestArtifact('testout.log', os.path.join(artifacts_dir, testout)))

        # attach all other logs; name them after their filename; eventually, tmt results.yaml should
        # allow more meta-data, like declaring a HTML viewer
        for log in logs:
            artifacts.append(TestArtifact(os.path.basename(log), os.path.join(artifacts_dir, log)))

        test_results.append(TestResult(name, outcome, artifacts))

    # count the maximum result weight encountered, i.e. the overall result
    max_weight = max(RESULT_WEIGHT[data['result']] for _, data in six.iteritems(results))

    if recognize_errors:
        return PLAN_OUTCOME_WITH_ERROR[max_weight], results

    return PLAN_OUTCOME[max_weight], test_results


class TestScheduleTMT(Module):
    """
    Creates test schedule entries for ``test-scheduler`` module by inspecting FMF configuration using TMT tool.

        `<https://tmt.readthedocs.io>`

    It executes each plan in a separate schedule entry using ``tmt run``. For execution it uses ``how=connect``
    for the provision step.

    By default `tmt` errors are treated as test failures, use `--recognize-errors` option to treat them as errors.
    """

    name = 'test-schedule-tmt'
    description = 'Create test schedule entries for ``test-scheduler`` module by inspecting FMF configuration via TMT.'
    options = [
        ('TMT options', {
            'command': {
                'help': 'TMT command to use (default: %(default)s).',
                'default': 'tmt'
            },
            'plan-filter': {
                'help': """
                        Use the given filter passed to 'tmt plan ls --filter'. See pydoc fmf.filter for details.
                        (default: %(default)s).
                        """,
                'default': 'enabled:true',
                'metavar': 'FILTER'
            },
            'context-template-file': {
                'help': """
                        If specified, files are treated as templates for YAML mappings which, when rendered,
                        are passed to ``tmt`` via ``-c @foo`` option. (default: none)
                        """,
                'action': 'append',
                'default': []
            },
        }),
        ('Result options', {
            'recognize-errors': {
                'help': 'If set, the error from tmt is recognized as test error.',
                'action': 'store_true',
            },
            'reproducer-comment': {
                'help': 'Comment added at the beginning of the tmt reproducer. (default: %(default)s).',
                'default': '# tmt reproducer'
            },
        })
    ]

    shared_functions = ['create_test_schedule', 'run_test_schedule_entry', 'serialize_test_schedule_entry_results']

    def __init__(self, *args, **kwargs):
        # type: (*Any, **Any) -> None
        super(TestScheduleTMT, self).__init__(*args, **kwargs)

    @gluetool.utils.cached_property
    def context_template_files(self):
        # type: () -> List[str]

        return gluetool.utils.normalize_path_option(self.option('context-template-file'))

    def _context_templates(self, filepaths):
        # type: (List[str]) -> List[str]

        templates = []

        for filepath in filepaths:
            try:
                with open(filepath, 'r') as f:
                    templates.append(f.read())

            except IOError as exc:
                raise gluetool.GlueError('Cannot open template file {}: {}'.format(filepath, exc))

        return templates

    def render_context_templates(
        self,
        logger,  # type: gluetool.log.ContextAdapter
        context,  # type: Dict[str, Any]
        template_filepaths=None,  # type: Optional[List[str]]
        filepath_dir=None,  # type: Optional[str]
        filename_prefix=CONTEXT_FILENAME_PREFIX,  # type: str
        filename_suffix=CONTEXT_FILENAME_SUFFIX  # type: str
    ):
        # type: (...) -> List[str]
        """
        Render context template files. For each template file, a file with rendered content is created.

        :param logger: logger to use for logging.
        :param dict context: context to use for rendering.
        :param str template_filepaths: list of paths to template files. If not set, paths set via
            ``--context-template-file`` option are used.
        :param str filepath_dir: all files are created in this directory. If not set, current working directory
            is used.
        :param str filename_prefix: all file names begin with this prefix.
        :param str filename_suffix: all file names end with this suffix.
        :returns: list of paths to rendered files, one for each template.
        """

        template_filepaths = template_filepaths or self.context_template_files
        filepath_dir = filepath_dir or os.getcwd()

        filepaths = []

        for template in self._context_templates(template_filepaths):
            with tempfile.NamedTemporaryFile(
                prefix=filename_prefix,
                suffix=filename_suffix,
                dir=filepath_dir,
                delete=False
            ) as f:
                f.write(
                    six.ensure_binary(gluetool.utils.render_template(template, logger=logger, **context))
                )

                f.flush()

            # Make the "temporary" file readable for investigation when pipeline's done.
            os.chmod(f.name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

            filepaths.append(f.name)

        return filepaths

    def _tmt_context_to_options(self, context):
        # type: (Dict[str, str]) -> List[str]
        if not context:
            return []

        options = []  # type: List[str]

        for name, value in six.iteritems(context):
            options += [
                '-c', '{}={}'.format(name, value)
            ]

        return options

    def _plans_from_git(self, repodir, context_files, testing_environment, filter=None):
        # type: (str, List[str], TestingEnvironment, Optional[str]) -> List[str]
        """
        Return list of plans from given repository.

        :param str repodir: clone of a dist-git repository.
        :param str filter: use the given filter when listing plans.
        """

        command = [
            self.option('command')
        ]

        if context_files:
            command.extend([
                '--context=@{}'.format(filepath)
                for filepath in context_files
            ])

        # using `# noqa` because flake8 and coala are confused by the walrus operator
        # Ignore PEP8Bear
        if (tmt := testing_environment.tmt) and 'context' in tmt:  # noqa: E203 E231
            command.extend(self._tmt_context_to_options(tmt['context']))

        command.extend(['plan', 'ls'])

        if filter:
            command.extend(['--filter', filter])

        # by default we add enabled:true
        else:
            command.extend(['--filter', 'enabled:true'])

        # using `# noqa` because flake8 and coala are confused by the walrus operator
        # Ignore PEP8Bear
        if (tf_request := self.shared('testing_farm_request')) and tf_request.tmt and (plan := tf_request.tmt.plan):  # noqa: E203 E231 E501
            command.extend([plan])

        try:
            tmt_output = Command(command).run(cwd=repodir)

        except GlueCommandError as exc:
            # TODO: remove once tmt-1.21 is out
            # workaround until tmt prints errors properly to stderr
            log_blob(
                self.error,
                "Failed to get list of plans",
                exc.output.stderr or exc.output.stdout or '<no output>'
            )
            raise GlueError('Failed to list plans, TMT metadata are absent or corrupted.')

        if not tmt_output.stdout:
            raise GlueError("Did not find any plans. Command used '{}'.".format(' '.join(command)))

        output_lines = [line.strip() for line in tmt_output.stdout.splitlines()]

        # TMT emits warnings to stdout, spoiling the actual output, and we have to remove them before we consume
        # what's left. And it could be helpful to display them. When TMT gets wiser, we can remove this workaround.
        tmt_warnings = [line for line in output_lines if line.startswith('warning:')]

        plans = [line for line in output_lines if line not in tmt_warnings]

        if tmt_warnings:
            log_dict(self.warn, 'tmt emitted following warnings', tmt_warnings)

        log_dict(self.debug, 'tmt plans', plans)

        if not plans:
            raise GlueError('No plans found, cowardly refusing to continue.')

        return plans

    def hardware_from_tmt(self, exported_plan):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        return cast(Dict[str, Any], exported_plan.get('provision', {}).get('hardware', {}))

    def excludes_from_tmt(self, exported_plan):
        # type: (Dict[str, Any]) -> List[str]
        if 'prepare' not in exported_plan:
            return []

        prepare = exported_plan['prepare']
        prepare_steps = prepare if isinstance(exported_plan['prepare'], list) else [prepare]

        excludes = []  # type: List[str]

        for step in prepare_steps:
            # we are interesed only on `how: install` step
            if step.get('how') != 'install':
                continue

            # no exclude in the step
            if 'exclude' not in step:
                return []

            gluetool.utils.log_dict(
                self.info,
                "Excluded packages from installation for '{}' plan".format(exported_plan['name']),
                step['exclude']
            )

            excludes.extend(cast(List[str], step['exclude']))

        return excludes

    def export_plan(self, repodir, plan, context_files):
        # type: (str, str, List[str]) -> Dict[str, Any]

        command = [self.option('command')] + [
            '--context=@{}'.format(filepath)
            for filepath in context_files
        ] + ['plan', 'export', '^{}$'.format(re.escape(plan))]

        try:
            tmt_output = Command(command).run(cwd=repodir)

        except GlueCommandError as exc:
            # workaround until tmt prints errors properly to stderr
            log_dict(self.error, "Failed to get list of plans", {
                'command': ' '.join(command),
                'exception': exc.output.stderr
            })
            six.reraise(*sys.exc_info())

        output = tmt_output.stdout
        assert output

        try:
            exported_plans = from_yaml(output)
            log_dict(self.debug, "loaded exported plan yaml", exported_plans)

        except GlueError as error:
            raise GlueError('Could not load exported plan yaml: {}'.format(error))

        if not exported_plans or len(exported_plans) != 1:
            self.warn('exported plan is not a single item, cowardly skipping extracting hardware')
            return {}

        return cast(Dict[str, Any], exported_plans[0])

    def create_test_schedule(self, testing_environment_constraints=None):
        # type: (Optional[List[TestingEnvironment]]) -> TestSchedule
        """
        Create a test schedule based on list of tmt plans.

        :param list(gluetool_modules_framework.libs.testing_environment.TestingEnvironment)
            testing_environment_constraints:
                limitations put on us by the caller. In the form of testing environments - with some fields possibly
                left unspecified - the list specifies what environments are expected to be used for testing.
                At this moment, only ``arch`` property is obeyed.
        :returns: a test schedule consisting of :py:class:`TestScheduleEntry` instances.
        """

        if not testing_environment_constraints:
            self.warn('TMT scheduler does not support open constraints', sentry=True)
            return TestSchedule()

        self.require_shared('dist_git_repository')
        repository = self.shared('dist_git_repository')

        repodir = repository.clone(
            logger=self.logger,
            prefix=repository.workdir_prefix
        )

        root_logger = Logging.get_logger()  # type: ContextAdapter

        schedule = TestSchedule()

        for tec in testing_environment_constraints:
            if tec.arch == tec.ANY:
                self.warn('TMT scheduler does not support open constraints', sentry=True)
                continue

            # Construct a custom logger for this particular TEC, to provide more context.
            # If we ever construct a schedule entry based on this TEC, such an entry would
            # construct the very similar logger, too.
            logger = TestScheduleEntryAdapter(root_logger, TestScheduleEntry.construct_id(tec))  # type: ContextAdapter

            logger.info('looking for plans')

            # Prepare tmt context files
            context = gluetool.utils.dict_update(
                self.shared('eval_context'),
                {
                    'TEC': tec
                }
            )

            context_files = self.render_context_templates(logger, context)

            plans = self._plans_from_git(repodir, context_files, tec, self.option('plan-filter'))

            for plan in plans:
                exported_plan = self.export_plan(repodir, plan, context_files)

                schedule_entry = TestScheduleEntry(
                    root_logger,
                    tec,
                    plan,
                    repodir,
                    self.excludes_from_tmt(exported_plan)
                )

                schedule_entry.testing_environment = TestingEnvironment(
                    compose=tec.compose,
                    arch=tec.arch,
                    snapshots=tec.snapshots,
                    pool=tec.pool,
                    hardware=tec.hardware or self.hardware_from_tmt(exported_plan),
                    variables=tec.variables
                )

                schedule_entry.tmt_reproducer.extend(repository.commands)

                schedule_entry.context_files = context_files

                schedule.append(schedule_entry)

        schedule.log(self.debug, label='complete schedule')

        return schedule

    def _prepare_environment(self, schedule_entry):
        # type: (TestScheduleEntry) -> str
        """
        Prepare local environment for running the schedule entry, by setting up some directories and files.

        :returns: a path to a work directory, dedicated for this entry.
        """

        assert schedule_entry.guest is not None

        # Create a working directory, we try hard to keep all the related work inside this directory.
        # This directory is passed to `tmt run --id` and tmt will keep all test artifacts.

        work_dir_prefix = 'work-{}'.format(os.path.basename(schedule_entry.plan))

        # tempfile.mkdtemp returns an absolute path to the directory, but the unspoken convention says
        # we must use paths that are relative to the current working directory. Therefore we must make
        # both schedule entry's work dir relative to the CWD.
        work_dir = os.path.relpath(
            tempfile.mkdtemp(dir=os.getcwd(), prefix=work_dir_prefix),
            os.getcwd()
        )

        # Make sure it's possible to enter our directories for other parties. We're not that concerned with privacy,
        # we'd rather let common users inside the directories when inspecting the pipeline artifacts. Therefore
        # setting their permissions to ug=rwx,o=rx.

        os.chmod(
            work_dir,
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH  # noqa: E501  # line too long
        )

        schedule_entry.info("working directory '{}'".format(work_dir))

        return work_dir

    def _run_plan(self, schedule_entry, work_dirpath, tmt_log_filepath):
        # type: (TestScheduleEntry, str, str) -> Tuple[TestScheduleResult, List[TestResult]]
        """
        Run a test plan, observe and report results.
        """

        # We're going to spawn new thread for `_run_plan`, therefore we will have to setup its thread
        # root action to the current one of this thread.
        current_action = Action.current_action()

        assert schedule_entry.guest is not None

        Action.set_thread_root(current_action)

        dict_update(
            self.shared('eval_context'),
            {
                'GUEST': schedule_entry.guest
            }
        )

        self.info('running in {}'.format(schedule_entry.repodir))

        # work_dirpath is relative to the current directory, but tmt expects it to be a absolute path
        # so it recognizes it as a path instead of run directory name
        command = [
            self.option('command')
        ]  # type: List[str]

        command += [
            '--context=@{}'.format(filepath)
            for filepath in schedule_entry.context_files
        ]

        # reproducer is the command which we present to user for reproducing the execution
        # on his localhost
        reproducer = command.copy()

        reproducer.extend([
            'run',
            '--all',
            '--verbose'
        ])

        command.extend([
            'run',
            '--all',
            '--verbose',
            '--id', os.path.abspath(work_dirpath)
        ])

        assert schedule_entry.testing_environment

        if schedule_entry.testing_environment.tmt and 'context' in schedule_entry.testing_environment.tmt:
            tmt_context = self._tmt_context_to_options(schedule_entry.testing_environment.tmt['context'])
            command.extend(tmt_context)
            reproducer.extend(tmt_context)

        variables = schedule_entry.testing_environment.variables

        if variables:
            # we MUST use a dedicated env file for each plan, to mitigate race conditions
            # plans are handled in threads ...
            tmt_env_file = TMT_ENV_FILE.format(schedule_entry.plan[1:].replace('/', '-'))
            gluetool.utils.dump_yaml(variables, os.path.join(schedule_entry.repodir, tmt_env_file))
            env_options = [
                '-e', '@{}'.format(tmt_env_file)
            ]
            command.extend(env_options)
            reproducer.extend(env_options)

            # reproducer command to download the environment file
            schedule_entry.tmt_reproducer.append(
                'curl -LO {}'.format(
                    artifacts_location(self, os.path.join(schedule_entry.repodir, tmt_env_file), logger=self.logger)
                )
            )

        if isinstance(schedule_entry.guest, StaticLocalhostGuest):
            local_command = [
                # `provision` step
                'provision',

                # `plan` step
                'plan',
                '--name', r'^{}$'.format(re.escape(schedule_entry.plan))
            ]
            command += local_command
            reproducer += local_command

        else:
            # `provision` step
            assert schedule_entry.guest.environment is not None
            assert isinstance(schedule_entry.guest.environment.compose, str)
            reproducer.extend([
                'provision',
                '--how', 'virtual',
                '--image', schedule_entry.guest.environment.compose,
            ])

            assert schedule_entry.guest.key is not None
            assert schedule_entry.guest.hostname is not None

            command.extend([
                'provision',
                '--how', 'connect',
                '--guest', schedule_entry.guest.hostname,
                '--key', schedule_entry.guest.key,
                '--port', str(schedule_entry.guest.port),
            ])

        if self.has_shared('sut_install_commands'):
            commands = '\n'.join(self.shared('sut_install_commands'))
            self.debug('sut_install_commands: {}'.format(commands))
            reproducer.extend([
                # `prepare` step
                'prepare',
                '--how', 'shell',
                '--script', "'\n" + commands + "\n'"
            ])
        else:
            self.debug('no sut_install_commands available')

        # `plan` step
        command.extend([
            'plan',
            '--name', r'^{}$'.format(re.escape(schedule_entry.plan))
        ])
        reproducer.extend([
            'plan',
            '--name', r'^{}$'.format(re.escape(schedule_entry.plan))
        ])

        # add tmt reproducer suitable for local execution
        schedule_entry.tmt_reproducer.append(' '.join(reproducer))

        def _save_output(output):
            # type: (gluetool.utils.ProcessOutput) -> None

            with open(tmt_log_filepath, 'w') as f:
                def _write(label, s):
                    # type: (str, str) -> None
                    f.write('{}\n{}\n\n'.format(label, s))

                _write('# STDOUT:', format_blob(cast(str, output.stdout)))
                _write('# STDERR:', format_blob(cast(str, output.stderr)))

                f.flush()

        def _save_reproducer(reproducer):
            # type: (str) -> None

            assert schedule_entry.tmt_reproducer_filepath
            with open(schedule_entry.tmt_reproducer_filepath, 'w') as f:
                def _write(*args):
                    # type: (Any) -> None
                    f.write('\n'.join(args))

                # TODO: artifacts instalation should be added once new plugin is ready
                _write(
                    self.option('reproducer-comment'),
                    reproducer
                )

                f.flush()

        tmt_output = None

        # run plan via tmt, note that the plan MUST be run in the artifact_dirpath
        try:
            tmt_output = Command(command).run(
                cwd=schedule_entry.repodir,
                inspect=True,
                inspect_callback=create_inspect_callback(schedule_entry.logger)
            )

        except GlueCommandError as exc:
            tmt_output = exc.output

        finally:
            if tmt_output:
                _save_output(tmt_output)
            if schedule_entry.tmt_reproducer:
                _save_reproducer('\n'.join(schedule_entry.tmt_reproducer))

        self.info('tmt exited with code {}'.format(tmt_output.exit_code))

        # check if tmt failed to produce results
        if tmt_output.exit_code == TMTExitCodes.RESULTS_MISSING:
            schedule_entry.warn('tmt did not produce results, skipping results evaluation')

            return TestScheduleResult.FAILED, [
                TestResult(
                    schedule_entry.id,
                    RESULT_OUTCOME['fail'],
                    [
                        TestArtifact('testout.log', tmt_log_filepath),
                        TestArtifact('log_dir', os.path.split(tmt_log_filepath)[0]),
                    ]
                )
            ]

        # gather and return overall plan run result and test results
        return gather_plan_results(schedule_entry, work_dirpath, self.option('recognize-errors'))

    def run_test_schedule_entry(self, schedule_entry):
        # type: (TestScheduleEntry) -> None

        # this schedule entry is not ours, move it along
        if schedule_entry.runner_capability != 'tmt':
            self.overloaded_shared('run_test_schedule_entry', schedule_entry)
            return

        self.shared('trigger_event', 'test-schedule-runner-sti.schedule-entry.started',
                    schedule_entry=schedule_entry)

        work_dirpath = self._prepare_environment(schedule_entry)
        schedule_entry.work_dirpath = work_dirpath

        tmt_log_filepath = os.path.join(work_dirpath, TMT_LOG)
        schedule_entry.tmt_reproducer_filepath = os.path.join(work_dirpath, TMT_REPRODUCER)

        artifacts = artifacts_location(self, tmt_log_filepath, logger=schedule_entry.logger)

        schedule_entry.info('TMT logs are in {}'.format(artifacts))

        plan_result, test_results = self._run_plan(schedule_entry, work_dirpath, tmt_log_filepath)

        schedule_entry.result = plan_result
        schedule_entry.results = test_results

        log_dict(schedule_entry.debug, 'results', test_results)

        self.shared('trigger_event', 'test-schedule-runner-sti.schedule-entry.finished',
                    schedule_entry=schedule_entry)

    def serialize_test_schedule_entry_results(self, schedule_entry, test_suite):
        # type: (TestScheduleEntry, Any) -> None

        def _add_property(properties, name, value):
            # type: (Any, str, str) -> Any
            return new_xml_element('property', _parent=properties, name='baseosci.{}'.format(name), value=value or '')

        def _add_artifact(logs, name, path, href, schedule_entry=None):
            # type: (Any, str, str, str, Optional[TestScheduleEntry]) -> Any

            attrs = {
                'name': name,
                'href': href,
                'schedule-stage': 'running'
            }

            if schedule_entry is not None:
                attrs['schedule-entry'] = schedule_entry.id

                schedule_entry.outputs.append(TestScheduleEntryOutput(
                    stage=TestScheduleEntryStage.RUNNING,
                    label=name,
                    log_path=path,
                    additional_data=None
                ))

            return new_xml_element(
                'log',
                _parent=logs,
                **attrs
            )

        def _add_testing_environment(test_case, name, arch, compose, snapshots):
            # type: (Any, str, Any, Any, bool) -> Any
            parent_elem = new_xml_element('testing-environment', _parent=test_case, name=name)
            new_xml_element('property', _parent=parent_elem, name='arch', value=arch)
            if compose:
                new_xml_element('property', _parent=parent_elem, name='compose', value=compose)
            new_xml_element('property', _parent=parent_elem, name='snapshots', value=str(snapshots))

        if schedule_entry.runner_capability != 'tmt':
            self.overloaded_shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)
            return

        if not schedule_entry.results:
            return

        if schedule_entry.tmt_reproducer_filepath:
            new_xml_element(
                'log',
                _parent=test_suite.logs,
                **{
                    'name': 'tmt-reproducer',
                    'href': artifacts_location(
                        self,
                        schedule_entry.tmt_reproducer_filepath,
                        logger=schedule_entry.logger
                    )
                }
            )

        if schedule_entry.work_dirpath:
            new_xml_element(
                'log',
                _parent=test_suite.logs,
                **{
                    'name': 'workdir',
                    'href': artifacts_location(
                        self,
                        schedule_entry.work_dirpath,
                        logger=schedule_entry.logger
                    )
                }
            )

        for task in schedule_entry.results:

            test_case = new_xml_element('testcase', _parent=test_suite, name=task.name, result=task.result)
            properties = new_xml_element('properties', _parent=test_case)
            logs = new_xml_element('logs', _parent=test_case)

            if task.result == 'failed':
                new_xml_element('failure', _parent=test_case)

            if task.result == 'error':
                new_xml_element('error', _parent=test_case)

            # test properties
            assert schedule_entry.guest is not None
            assert schedule_entry.guest.environment is not None
            assert schedule_entry.guest.hostname is not None
            _add_property(properties, 'arch', str(schedule_entry.guest.environment.arch))
            _add_property(properties, 'connectable_host', schedule_entry.guest.hostname)
            _add_property(properties, 'distro', str(schedule_entry.guest.environment.compose))
            _add_property(properties, 'status', schedule_entry.stage.value.capitalize())
            _add_property(properties, 'testcase.source.url', self.shared('dist_git_repository').web_url)
            _add_property(properties, 'variant', '')

            # artifacts
            for artifact in task.artifacts:
                _add_artifact(
                    logs,
                    name=artifact.name,
                    path=artifact.path,
                    href=artifacts_location(self, artifact.path, logger=schedule_entry.logger),
                    schedule_entry=schedule_entry
                )

            assert schedule_entry.testing_environment is not None
            _add_testing_environment(
                test_case, 'requested',
                schedule_entry.testing_environment.arch,
                schedule_entry.testing_environment.compose,
                schedule_entry.testing_environment.snapshots
            )
            _add_testing_environment(
                test_case, 'provisioned',
                schedule_entry.guest.environment.arch,
                schedule_entry.guest.environment.compose,
                schedule_entry.guest.environment.snapshots
            )

            # sorting
            sort_children(properties, lambda child: child.attrs['name'])

        test_suite['tests'] = len(schedule_entry.results)
