# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import re
import os
import os.path
import stat
import sys
import tempfile

import enum
import six
import attrs
import cattrs

import gluetool
from gluetool import GlueError, GlueCommandError, Module
from gluetool.action import Action
from gluetool.log import Logging, format_blob, log_blob, log_dict
from gluetool.log import ContextAdapter, LoggingFunctionType  # Ignore PyUnusedCodeBear
from gluetool.utils import Command, dict_update, from_yaml, load_yaml, new_xml_element, create_cattrs_converter

from gluetool_modules_framework.infrastructure.static_guest import StaticLocalhostGuest
from gluetool_modules_framework.libs import create_inspect_callback, sort_children
from gluetool_modules_framework.libs.artifacts import artifacts_location
from gluetool_modules_framework.libs.guest_setup import GuestSetupStage
from gluetool_modules_framework.libs.sut_installation import INSTALL_COMMANDS_FILE
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.test_schedule import TestSchedule, TestScheduleResult, TestScheduleEntryOutput, \
    TestScheduleEntryStage, TestScheduleEntryAdapter
from gluetool_modules_framework.libs.test_schedule import TestScheduleEntry as BaseTestScheduleEntry
from gluetool_modules_framework.testing_farm.testing_farm_request import TestingFarmRequest
from gluetool_modules_framework.libs.git import RemoteGitRepository
from gluetool_modules_framework.provision.artemis import ArtemisGuest

# Type annotations
from typing import cast, Any, Callable, Dict, List, Optional, Tuple, Union  # noqa

from gluetool_modules_framework.libs.results import TestSuite, Log, TestCase, TestCaseCheck, Guest
from secret_type import Secret

from cattrs.gen import make_dict_unstructure_fn, make_dict_structure_fn, override

# TMT run log file
TMT_LOG = 'tmt-run.log'
TMT_REPRODUCER = 'tmt-reproducer.sh'

# File with environment variables
TMT_ENV_FILE = 'tmt-environment-{}.yaml'

# Weight of a test result, used to count the overall result. Higher weight has precendence
# when counting the overall result. See https://tmt.readthedocs.io/en/latest/spec/steps.html#execute
RESULT_WEIGHT = {
    'skip': 0,
    'pass': 1,
    'info': 1,
    'fail': 2,
    'warn': 2,
    'error': 3,
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
    'error': 'error',
    'skip': 'not_applicable'
}

# Result weight to TestScheduleResult outcome
#
#     https://tmt.readthedocs.io/en/latest/overview.html#exit-codes
#
# All tmt errors are connected to tests or config, so only higher return code than 3
# is treated as error
PLAN_OUTCOME = {
    0: TestScheduleResult.SKIPPED,
    1: TestScheduleResult.PASSED,
    2: TestScheduleResult.FAILED,
    3: TestScheduleResult.FAILED,
}

# Result weight to TestScheduleResult outcome
#
#     https://tmt.readthedocs.io/en/latest/overview.html#exit-codes
#
# All tmt errors are connected to tests or config, so only higher return code than 3
# is treated as error
PLAN_OUTCOME_WITH_ERROR = {
    0: TestScheduleResult.SKIPPED,
    1: TestScheduleResult.PASSED,
    2: TestScheduleResult.FAILED,
    3: TestScheduleResult.ERROR,
}

# Results YAML file, contains list of test run results, relative to plan workdir
RESULTS_YAML = "execute/results.yaml"
GUESTS_YAML = "provision/guests.yaml"


@attrs.define
class TestArtifact:
    """
    Represents a test output artifact (in particular, log files)
    """
    name: str
    path: str


@attrs.define(kw_only=True)
class TestResult:
    """
    Gluetool representation of a test run result

    :ivar name: name of the test.
    :ivar result: test result.
    :ivar artifacts: artifacts/log files declared by the test
    """
    name: str
    result: str
    artifacts: List[TestArtifact]
    note: Optional[str] = None
    checks: List[TestCaseCheck]
    guest: Optional['TMTResultGuest'] = None
    serial_number: Optional[int] = None


@attrs.define
class TMTResultCheck:
    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    result: str = attrs.field(validator=attrs.validators.instance_of(str))
    event: str = attrs.field(validator=attrs.validators.instance_of(str))
    log: List[str] = attrs.field(validator=attrs.validators.deep_iterable(
        member_validator=attrs.validators.instance_of(str),
        iterable_validator=attrs.validators.instance_of(list)
    ))


@attrs.define
class TMTGuest:
    """
    Represents a value in `guests.yaml` file generated by tmt. The file `provision/guests.yaml` has
    `Dict[str, 'TMTGuest']` structure, where `str` represents guest name. The actual file contains more items, this
    class lists only those that are used in the gluetool module.

    To see how the file is generated in tmt see `tmt.steps.provision.Provision.save()` in
    https://github.com/teemtee/tmt/blob/main/tmt/steps/provision/__init__.py.
    """

    image: str = attrs.field(validator=attrs.validators.instance_of(str))
    arch: str = attrs.field(validator=attrs.validators.instance_of(str))


@attrs.define
class TMTResultGuest:
    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    role: Optional[str] = attrs.field(validator=attrs.validators.optional(attrs.validators.instance_of(str)))


@attrs.define(kw_only=True)
class TMTResult:
    """
    Represents an element in `results.yaml` file generated by tmt. The file `results.yaml` has `List['TMTResult']`
    structure. The actual file contains more items, this class lists only those that are used in the gluetool module.
    Instances of this class will be further parsed into `TestResult` instances.

    For the complete definition of the `results.yaml` format, see:
      * https://tmt.readthedocs.io/en/stable/spec/plans.html#results-format
      * https://github.com/teemtee/tmt/blob/main/tmt/schemas/results.yaml
    """

    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    result: str = attrs.field(validator=attrs.validators.instance_of(str))
    log: List[str] = attrs.field(validator=attrs.validators.deep_iterable(
        member_validator=attrs.validators.instance_of(str),
        iterable_validator=attrs.validators.instance_of(list)
    ))
    guest: TMTResultGuest = attrs.field(validator=attrs.validators.instance_of(TMTResultGuest))
    note: Optional[str] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(str))
    )
    check: List[TMTResultCheck] = attrs.field(validator=attrs.validators.deep_iterable(
        member_validator=attrs.validators.instance_of(TMTResultCheck),
        iterable_validator=attrs.validators.instance_of(list)
    ))
    serial_number: Optional[int] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(int))
    )

    # We need to map the yaml attributes containing dashes to Python variables
    @classmethod
    def register_hooks(cls, converter: cattrs.Converter) -> None:
        converter.register_structure_hook(TMTResult, make_dict_structure_fn(
            TMTResult,
            converter,
            serial_number=override(rename='serial-number')
        ))
        converter.register_unstructure_hook(TMTResult, make_dict_unstructure_fn(
            TMTResult,
            converter,
            serial_number=override(rename='serial-number')
        ))


@attrs.define
class TMTPlanProvision:
    """
    Represents the "provision" step of a TMT plan. See :py:class:`TMTPlan` for more information.
    """
    hardware: Optional[Dict[str, Any]] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.deep_mapping(
            key_validator=attrs.validators.instance_of(str),
            value_validator=attrs.validators.instance_of(object),  # Anything should pass
            mapping_validator=attrs.validators.instance_of(dict)
        ))
    )
    kickstart: Optional[Dict[str, str]] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.deep_mapping(
            key_validator=attrs.validators.instance_of(str),
            value_validator=attrs.validators.instance_of(str),
            mapping_validator=attrs.validators.instance_of(dict)
        ))
    )
    watchdog_dispatch_delay: Optional[int] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(int))
    )
    watchdog_period_delay: Optional[int] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(int))
    )

    # We need to map the yaml attributes containing dashes to Python variables
    @classmethod
    def register_hooks(cls, converter: cattrs.Converter) -> None:
        converter.register_structure_hook(TMTPlanProvision, make_dict_structure_fn(
            TMTPlanProvision,
            converter,
            watchdog_dispatch_delay=override(rename='watchdog-dispatch-delay'),
            watchdog_period_delay=override(rename='watchdog-period-delay')
        ))
        converter.register_unstructure_hook(TMTPlanProvision, make_dict_unstructure_fn(
            TMTPlanProvision,
            converter,
            watchdog_dispatch_delay=override(rename='watchdog-dispatch-delay'),
            watchdog_period_delay=override(rename='watchdog-period-delay')
        ))


@attrs.define
class TMTPlanPrepare:
    """
    Represents the "prepare" step of a TMT plan. See :py:class:`TMTPlan` for more information.
    """
    how: str = attrs.field(validator=attrs.validators.instance_of(str))
    exclude: Optional[List[str]] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.deep_iterable(
            member_validator=attrs.validators.instance_of(str),
            iterable_validator=attrs.validators.instance_of(list)
        ))
    )

    def converter(prepare_step: Any) -> List['TMTPlanPrepare']:
        """
        In a TMT plan, the "prepare" step can be defined either as a single "prepare" element or a list of "prepare"
        elements. This converter does the job of converting both possible shapes into a single one - list of
        :py:class:`TMTPlanPrepare` instances.

        See the docs for more information about the "prepare" step:
        * https://tmt.readthedocs.io/en/stable/spec/plans.html#prepare
        """
        prepare_steps = prepare_step if isinstance(prepare_step, list) else [prepare_step]
        try:
            # Try to convert a list of dicts into a list of `TMTPlanPrepare` objects
            return [TMTPlanPrepare(**{field.name: prepare_step.get(field.name)
                                      for field in attrs.fields(TMTPlanPrepare)})
                    for prepare_step in prepare_steps]
        except AttributeError:
            # Assume that `prepare_steps` is already a list of `TMTPlanPrepare` objects, so don't do anything
            return prepare_steps


@attrs.define
class TMTPlan:
    """
    Represents a single TMT plan. The actual plan may contain more items, this class lists only those that are used in
    the gluetool module.

    For the complete definition of the TMT plans format, see:
      * https://tmt.readthedocs.io/en/stable/spec/plans.html
      * https://github.com/teemtee/tmt/blob/main/tmt/schemas/plan.yaml
    """
    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    provision: List[TMTPlanProvision] = attrs.field(
        factory=list,
        validator=attrs.validators.deep_iterable(
            member_validator=attrs.validators.instance_of(TMTPlanProvision),
            iterable_validator=attrs.validators.instance_of(list)
        )
    )
    prepare: List[TMTPlanPrepare] = attrs.field(
        factory=list,
        validator=attrs.validators.deep_iterable(
            member_validator=attrs.validators.instance_of(TMTPlanPrepare),
            iterable_validator=attrs.validators.instance_of(list)
        ),
        converter=TMTPlanPrepare.converter  # type: ignore[misc]  # mypy wrongly complains about unsupported converter
    )

    def excludes(self, logger: Optional[ContextAdapter] = None) -> List[str]:
        """
        Gathers all ``exclude`` fields from all ``prepare`` steps into a single flattened list.
        """
        logger = logger or Logging.get_logger()
        excludes: List[str] = []

        for step in self.prepare:
            # we are interesed only on `how: install` step
            if step.how != 'install':
                continue

            # no exclude in the step
            if step.exclude is None:
                return []

            gluetool.utils.log_dict(
                logger.info,
                "Excluded packages from installation for '{}' plan".format(self.name),
                step.exclude
            )

            excludes.extend(step.exclude)

        return excludes


# https://tmt.readthedocs.io/en/latest/overview.html#exit-codes
class TMTExitCodes(enum.IntEnum):
    TESTS_PASSED = 0
    TESTS_FAILED = 1
    TESTS_ERROR = 2
    RESULTS_MISSING = 3
    TESTS_SKIPPED = 4


class TestScheduleEntry(BaseTestScheduleEntry):
    @staticmethod
    def construct_id(tec: TestingEnvironment, plan: Optional[str] = None) -> str:

        entry_id = '{}:{}'.format(tec.compose, tec.arch)

        if plan is None:
            return entry_id

        return '{}:{}'.format(entry_id, plan)

    def __init__(
        self,
        logger: ContextAdapter,
        tec: TestingEnvironment,
        plan: str,
        repodir: str
    ) -> None:
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
        self.testsuite_name = self.plan = plan
        self.results: Optional[List[TestResult]] = None
        self.repodir: str = repodir
        self.tmt_reproducer: List[str] = []
        self.tmt_reproducer_filepath: Optional[str] = None

        self.tmt_env_file: Optional[str] = None

        self.guests: Optional[Dict[str, TMTGuest]] = None

    def log_entry(self, log_fn: Optional[LoggingFunctionType] = None) -> None:

        log_fn = log_fn or self.debug

        super(TestScheduleEntry, self).log_entry(log_fn=log_fn)

        log_fn('plan: {}'.format(self.plan))


def safe_name(name: str) -> str:
    """
    A safe variant of the name which does not contain special characters.

    Spaces and other special characters are removed to prevent problems with
    tools which do not expect them (e.g. in directory names).

    Workaround for https://github.com/teemtee/tmt/issues/1857
    """

    return re.sub(r"[^\w/-]+", "-", name).strip("-")


def gather_plan_results(
    module: gluetool.Module,
    schedule_entry: TestScheduleEntry,
    work_dir: str,
    recognize_errors: bool = False
) -> Tuple[TestScheduleResult, List[TestResult], Dict[str, TMTGuest]]:
    """
    Extracts plan results from tmt logs.

    :param TestScheduleEntry schedule_entry: Plan schedule entry.
    :param str work_dir: Plan working directory.
    :returns: A tuple with overall_result, List[TestResult] and Dict[str, TMTGuest].
    """
    test_results: List[TestResult] = []
    guests: Dict[str, TMTGuest] = {}

    # TMT uses plan name as a relative directory to the working directory, but
    # plan start's with '/' character, strip it so we can use it with os.path.join
    plan_path = safe_name(schedule_entry.plan[1:])

    results_yaml = os.path.join(work_dir, plan_path, RESULTS_YAML)
    guests_yaml = os.path.join(work_dir, plan_path, GUESTS_YAML)

    if not os.path.exists(results_yaml):
        schedule_entry.warn("Could not find results file '{}' containing tmt results".format(results_yaml), sentry=True)
        return TestScheduleResult.ERROR, test_results, guests

    # load test results from `results.yaml` which is created in tmt's execute step
    # https://tmt.readthedocs.io/en/latest/spec/steps.html#execute
    try:
        converter = create_cattrs_converter(prefer_attrib_converters=True)
        TMTResult.register_hooks(converter)
        results = load_yaml(
            results_yaml,
            unserializer=gluetool.utils.create_cattrs_unserializer(List[TMTResult], converter=converter)
        )
        log_dict(schedule_entry.debug, "loaded results from '{}'".format(results_yaml), results)
    except GlueError as error:
        schedule_entry.warn('Could not load results.yaml file: {}'.format(error))
        return TestScheduleResult.ERROR, test_results, guests

    try:
        guests = load_yaml(guests_yaml, unserializer=gluetool.utils.create_cattrs_unserializer(Dict[str, TMTGuest]))
        log_dict(schedule_entry.debug, "loaded guests from '{}'".format(guests_yaml), guests)
    except GlueError as error:
        schedule_entry.warn('Could not load guests.yaml file: {}'.format(error))
        return TestScheduleResult.ERROR, test_results, guests

    # Something went wrong, there should be results. There were tests, otherwise we wouldn't
    # be running `tmt run`, but where are results? Reporting an error...
    if not results:
        schedule_entry.warn('Could not find any results in results.yaml file')
        return TestScheduleResult.ERROR, test_results, guests

    # iterate through all the test results and create TestResult for each
    for result in results:
        # translate result outcome
        try:
            outcome = RESULT_OUTCOME[result.result]
        except KeyError:
            schedule_entry.warn("Encountered invalid result '{}' in runner results".format(result.result))
            return TestScheduleResult.ERROR, test_results, guests

        # copy the logs as we'll do some popping later
        logs: List[str] = result.log[:]

        artifacts_dir = os.path.join(work_dir, plan_path, 'execute')
        artifacts = []

        # attach the artifacts directory itself, useful for browsing;
        # usually all artifacts are in the same dir
        if result.log:
            artifacts.append(TestArtifact(
                name='log_dir',
                path=os.path.join(artifacts_dir, os.path.dirname(logs[0]))
            ))

            # the first log is the main one for developers, traditionally called "testout.log"
            testout = logs.pop(0)
            artifacts.append(TestArtifact(name='testout.log', path=os.path.join(artifacts_dir, testout)))

        # attach all other logs; name them after their filename; eventually, tmt results.yaml should
        # allow more meta-data, like declaring a HTML viewer
        for log in logs:
            artifacts.append(TestArtifact(name=os.path.basename(log), path=os.path.join(artifacts_dir, log)))

        checks = [
            TestCaseCheck(
                name=check.name,
                result=check.result,
                event=check.event,
                logs=[
                    Log(
                        href=artifacts_location(module, os.path.join(artifacts_dir, log), logger=schedule_entry.logger),
                        name=os.path.basename(log)
                    )
                    for log in check.log]
            ) for check in result.check
        ]

        test_results.append(TestResult(
            name=result.name,
            result=outcome,
            artifacts=artifacts,
            guest=result.guest,
            checks=checks,
            note=result.note,
            serial_number=result.serial_number
        ))

    # count the maximum result weight encountered, i.e. the overall result
    max_weight = max(RESULT_WEIGHT[result.result] for result in results)

    if recognize_errors:
        return PLAN_OUTCOME_WITH_ERROR[max_weight], test_results, guests

    return PLAN_OUTCOME[max_weight], test_results, guests


class TestScheduleTMTMultihost(Module):
    """
    A copy of `testing.test_schedule_runner.TestScheduleTMT` module with modifications for multihost pipeline. It is
    intended to be used together with `testing.test_schedule_tmt_multihost.TestScheduleRunnerMultihost`. The future plan
    is to merge multihost features from `TestScheduleRunnerMultihost` and `TestScheduleTMTMultihost` into their original
    counterparts.

    Original description of the `TestScheduleTMT` module:

    Creates test schedule entries for ``test-scheduler`` module by inspecting FMF configuration using TMT tool.

        `<https://tmt.readthedocs.io>`

    It executes each plan in a separate schedule entry using ``tmt run``. For execution it uses ``how=connect``
    for the provision step.

    By default `tmt` errors are treated as test failures, use `--recognize-errors` option to treat them as errors.
    """

    name = 'test-schedule-tmt-multihost'
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
                        (default: 'enabled:true').
                        """,
                'metavar': 'FILTER'
            },
            'test-filter': {
                'help': """
                        Use the given filter passed to 'tmt run discover plan test --filter'.
                        See pydoc fmf.filter for details. (default: none).
                        """,
                'metavar': 'FILTER'
            },
            'accepted-environment-variables': {
                'help': """
                        A comma delimited list of accepted environment variable names for the ``tmt`` process.
                        In case an environment a variable is provided, which does not match this list, the execution
                        is aborted.
                        """,
                'action': 'append',
                'default': []
            },
            'tmt-run-options': {
                'help': "Additional options passed to ``tmt run``, for example -ddddvvv.",
                'action': 'append',
                'default': []
            }
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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(TestScheduleTMTMultihost, self).__init__(*args, **kwargs)

    @gluetool.utils.cached_property
    def accepted_environment_variables(self) -> List[str]:
        return gluetool.utils.normalize_multistring_option(self.option('accepted-environment-variables'))

    @gluetool.utils.cached_property
    def test_filter(self) -> Optional[str]:
        if self.option('test-filter'):
            return str(self.option('test-filter'))

        tf_request = cast(Optional[TestingFarmRequest], self.shared('testing_farm_request'))
        if tf_request and tf_request.tmt and tf_request.tmt.test_filter:
            return tf_request.tmt.test_filter

        return None

    def _tmt_context_to_options(self, context: Dict[str, str]) -> List[str]:
        if not context:
            return []

        options: List[str] = []

        for name, value in six.iteritems(context):
            options += [
                '-c', '{}={}'.format(name, value)
            ]

        return options

    @gluetool.utils.cached_property
    def _root_option(self) -> List[str]:
        """
        Returns metadata ``--root PATH`` option for use in tmt commands. The path is fetched from Testing Farm request.
        """
        tf_request = cast(Optional[TestingFarmRequest], self.shared('testing_farm_request'))
        if tf_request and tf_request.tmt and tf_request.tmt.path:
            return ['--root', tf_request.tmt.path]
        return []

    def _prepare_tmt_env_file(self,
                              testing_environment_constraints: TestingEnvironment,
                              plan: str,
                              repodir: str) -> Optional[str]:
        # variables from testing-farm environment
        variables: Dict[str, Union[str, Secret[str]]] = {}

        if testing_environment_constraints.variables:
            variables.update(testing_environment_constraints.variables)

        eval_context = self.shared('eval_context')
        # variables from rules-engine's user variables, rendered with the evaluation context
        variables.update(self.shared('user_variables', logger=self.logger, context=eval_context) or {})

        # add secrets
        variables.update(testing_environment_constraints.secrets or {})

        if variables:
            # we MUST use a dedicated env file for each plan, to mitigate race conditions
            # plans are handled in threads ...
            tmt_env_file = TMT_ENV_FILE.format(plan[1:].replace('/', '-'))
            # TODO: teach `gluetool.utils.dump_yaml` how to work with `secret_type.Secret`
            gluetool.utils.dump_yaml(
                {k: (v._dangerous_extract() if isinstance(v, Secret) else v) for k, v in variables.items()},
                os.path.join(repodir, tmt_env_file)
            )

            return tmt_env_file

        return None

    def _plans_from_git(self,
                        repodir: str,
                        testing_environment: TestingEnvironment,
                        filter: Optional[str] = None) -> List[str]:
        """
        Return list of plans from given repository.

        :param str repodir: clone of a dist-git repository.
        :param str filter: use the given filter when listing plans.
        """

        command = [
            self.option('command')
        ]

        tf_request = cast(Optional[TestingFarmRequest], self.shared('testing_farm_request'))

        command.extend(self._root_option)

        if testing_environment.tmt and 'context' in testing_environment.tmt:
            command.extend(self._tmt_context_to_options(testing_environment.tmt['context']))

        command.extend(['plan', 'ls'])

        if filter:
            command.extend(['--filter', filter])

        elif tf_request and tf_request.tmt and tf_request.tmt.plan_filter:
            command.extend(['--filter', tf_request.tmt.plan_filter])

        # by default we add enabled:true
        else:
            command.extend(['--filter', 'enabled:true'])

        if tf_request and tf_request.tmt and tf_request.tmt.plan:
            command.extend([tf_request.tmt.plan])

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

    def _apply_test_filter(self,
                           plans: List[str],
                           repodir: str,
                           testing_environment: TestingEnvironment,
                           test_filter: Optional[str] = None) -> List[str]:
        """
        Return list of plans which still have tests after applying test filter.
        """

        test_filter = test_filter or self.test_filter

        # As the loop will remove the items in the list, the copy of the
        # list needs to be created, otherwise a single element would be always skipped after each removed one.
        for plan in plans[:]:
            command = [
                self.option('command')
            ]

            command.extend(self._root_option)

            if testing_environment.tmt and 'context' in testing_environment.tmt:
                command.extend(self._tmt_context_to_options(testing_environment.tmt['context']))

            command.extend(['run', 'discover', 'plan', '--name', plan, 'test', '--filter', test_filter])

            try:
                tmt_output = Command(command).run(cwd=repodir)

            except GlueCommandError as exc:
                log_blob(
                    self.error,
                    "Failed to discover tests",
                    exc.output.stderr or exc.output.stdout or '<no output>'
                )
                raise GlueError('Failed to discover tests, TMT metadata are absent or corrupted.')

            if not tmt_output.stderr:
                raise GlueError("Did not find any plans. Command used '{}'.".format(' '.join(command)))

            output_lines = [line.strip() for line in tmt_output.stderr.splitlines()]

            if any(['No tests found' in line for line in output_lines]):
                plans.remove(plan)

        if not plans:
            raise GlueError('No plans to execute after applying test/plan filters. Cowardly refusing to continue.')

        return plans

    def _is_plan_empty(self,
                       plan: str,
                       repodir: str,
                       testing_environment: TestingEnvironment,
                       tmt_env_file: Optional[str]) -> bool:
        """
        Return list of plans which still have tests after applying test filter.
        """

        command = [
            self.option('command')
        ]

        command.extend(self._root_option)

        if testing_environment.tmt and 'context' in testing_environment.tmt:
            command.extend(self._tmt_context_to_options(testing_environment.tmt['context']))

        command.extend(['run'])

        if tmt_env_file:
            env_options = [
                '-e', '@{}'.format(tmt_env_file)
            ]
            command.extend(env_options)

        command.extend(['discover', 'plan', '--name', plan])

        try:
            tmt_output = Command(command).run(cwd=repodir)

        except GlueCommandError as exc:
            log_blob(
                self.error,
                "Failed to discover tests",
                exc.output.stderr or exc.output.stdout or '<no output>'
            )
            raise GlueError('Failed to discover tests, TMT metadata are absent or corrupted.')

        if not tmt_output.stderr:
            raise GlueError("Did not find any plans. Command used '{}'.".format(' '.join(command)))

        output_lines = [line.strip() for line in tmt_output.stderr.splitlines()]

        if any(['No tests found' in line for line in output_lines]):
            return True

        return False

    def export_plan(self,
                    repodir: str,
                    plan: str,
                    tmt_env_file: Optional[str],
                    testing_environment: TestingEnvironment) -> Optional[TMTPlan]:
        command: List[str] = [self.option('command')]
        command.extend(self._root_option)

        if testing_environment.tmt and 'context' in testing_environment.tmt:
            command.extend(self._tmt_context_to_options(testing_environment.tmt['context']))

        command.extend(['plan', 'export'])

        if tmt_env_file:
            env_options = [
                '-e', '@{}'.format(tmt_env_file)
            ]
            command.extend(env_options)

        command.extend(['^{}$'.format(re.escape(plan))])

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
            converter = create_cattrs_converter(prefer_attrib_converters=True)
            TMTPlanProvision.register_hooks(converter)
            exported_plans = converter.structure(from_yaml(output), List[TMTPlan])
            log_dict(self.debug, "loaded exported plan yaml", exported_plans)

        except GlueError as error:
            raise GlueError('Could not load exported plan yaml: {}'.format(error))

        if not exported_plans or len(exported_plans) != 1:
            self.warn('exported plan is not a single item, cowardly skipping extracting hardware')
            return None

        return exported_plans[0]

    def create_test_schedule(
        self,
        testing_environment_constraints: Optional[List[TestingEnvironment]] = None
    ) -> TestSchedule:
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
        repository = cast(RemoteGitRepository, self.shared('dist_git_repository'))

        repodir = repository.clone(
            logger=self.logger,
            prefix=repository.clonedir_prefix
        )

        root_logger: ContextAdapter = Logging.get_logger()

        schedule = TestSchedule()

        for tec in testing_environment_constraints:
            if tec.arch == tec.ANY:
                self.warn('TMT scheduler does not support open constraints', sentry=True)
                continue

            # Construct a custom logger for this particular TEC, to provide more context.
            # If we ever construct a schedule entry based on this TEC, such an entry would
            # construct the very similar logger, too.
            logger: ContextAdapter = TestScheduleEntryAdapter(root_logger, TestScheduleEntry.construct_id(tec))

            logger.info('looking for plans')

            # Prepare tmt context files
            context = gluetool.utils.dict_update(
                self.shared('eval_context'),
                {
                    'TEC': tec
                }
            )

            plans = self._plans_from_git(repodir, tec, self.option('plan-filter'))

            if self.test_filter:
                plans = self._apply_test_filter(plans, repodir, tec)

            for plan in plans:

                tmt_env_file = self._prepare_tmt_env_file(tec, plan, repodir)

                if self._is_plan_empty(plan, repodir, tec, tmt_env_file):
                    continue

                schedule_entry = TestScheduleEntry(
                    root_logger,
                    tec,
                    plan,
                    repodir,
                )

                # Prepare environment for test schedule entry execution
                work_dirpath = self._prepare_environment(schedule_entry)
                schedule_entry.work_dirpath = work_dirpath

                schedule_entry.testing_environment = TestingEnvironment(
                    arch=tec.arch,
                    compose=tec.compose,
                    snapshots=tec.snapshots,
                    pool=tec.pool,
                    variables=tec.variables,
                    secrets=tec.secrets,
                    artifacts=tec.artifacts,
                    settings=tec.settings,
                    tmt=tec.tmt,
                )

                schedule_entry.tmt_reproducer.extend(repository.commands)

                schedule_entry.tmt_env_file = tmt_env_file

                schedule.append(schedule_entry)

            if not schedule:
                raise GlueError('No plans to execute after removing empty plans. Cowardly refusing to continue.')

        schedule.log(self.debug, label='complete schedule')

        return schedule

    def _prepare_environment(self, schedule_entry: TestScheduleEntry) -> str:
        """
        Prepare local environment for running the schedule entry, by setting up some directories and files.

        :returns: a path to a work directory, dedicated for this entry.
        """

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

    def _run_plan(self,
                  schedule_entry: TestScheduleEntry,
                  work_dirpath: str,
                  tmt_log_filepath: str) -> Tuple[TestScheduleResult, List[TestResult], Dict[str, TMTGuest]]:
        """
        Run a test plan, observe and report results.
        """

        # We're going to spawn new thread for `_run_plan`, therefore we will have to setup its thread
        # root action to the current one of this thread.
        current_action = Action.current_action()

        Action.set_thread_root(current_action)

        self.info('running in {}'.format(schedule_entry.repodir))

        # work_dirpath is relative to the current directory, but tmt expects it to be a absolute path
        # so it recognizes it as a path instead of run directory name
        command: List[str] = [
            self.option('command')
        ]

        tf_request = cast(TestingFarmRequest, self.shared('testing_farm_request'))

        command.extend(self._root_option)

        assert schedule_entry.testing_environment

        # add context from request
        if schedule_entry.testing_environment.tmt and 'context' in schedule_entry.testing_environment.tmt:
            tmt_context = self._tmt_context_to_options(schedule_entry.testing_environment.tmt['context'])
            command.extend(tmt_context)

        # create environment variables for the tmt process
        tmt_process_environment: Dict[str, str] = {}

        def _check_accepted_environment_variables(variables: Dict[str, str]) -> None:
            for key, _ in six.iteritems(variables):
                if key not in self.accepted_environment_variables:
                    raise GlueError(
                        "Environment variable '{}' is not allowed to be exposed to the tmt process".format(key)
                    )

        def _sanitize_environment_variables(variables: Dict[str, str]) -> str:
            return ' '.join(["{}=*****".format(key) for key, _ in six.iteritems(variables)])

        # using `# noqa` because flake8 and coala are confused by the walrus operator
        # Ignore PEP8Bear
        if (tmt := schedule_entry.testing_environment.tmt) and 'environment' in tmt and tmt['environment']:  # noqa: E203 E231 E501
            tmt_process_environment = tmt['environment']

            _check_accepted_environment_variables(tmt_process_environment)

            schedule_entry.tmt_reproducer.append(
                'export {}'.format(
                    _sanitize_environment_variables(tmt['environment'])
                )
            )

        def _save_output(output: gluetool.utils.ProcessOutput) -> None:

            with open(tmt_log_filepath, 'w') as f:
                def _write(label: str, s: str) -> None:
                    f.write('{}\n{}\n\n'.format(label, s))

                _write('# STDOUT:', format_blob(cast(str, output.stdout)))
                _write('# STDERR:', format_blob(cast(str, output.stderr)))

                f.flush()

        def _save_reproducer(reproducer: str) -> None:

            assert schedule_entry.tmt_reproducer_filepath
            with open(schedule_entry.tmt_reproducer_filepath, 'w') as f:
                def _write(*args: Any) -> None:
                    f.write('\n'.join(args))

                # TODO: artifacts instalation should be added once new plugin is ready
                _write(
                    self.option('reproducer-comment'),
                    reproducer
                )

                f.flush()

        tmt_output = None

        artemis_options: Dict[str, Any] = self.shared('artemis_api_options')
        artemis_api_url = artemis_options['api-url']
        artemis_api_version = artemis_options['api-version']
        artemis_ssh_key = artemis_options['ssh-key']
        artemis_key = artemis_options['key']
        artemis_post_install_script = artemis_options['post-install-script']
        artemis_skip_prepare_verify_ssh = artemis_options['skip-prepare-verify-ssh']

        command.extend(['run', '--all', '--id', os.path.abspath(work_dirpath)])
        command.extend(gluetool.utils.normalize_multistring_option(self.option('tmt-run-options')))

        if schedule_entry.tmt_env_file:
            env_options = [
                '-e', '@{}'.format(schedule_entry.tmt_env_file)
            ]
            command.extend(env_options)

            # reproducer command to download the environment file
            schedule_entry.tmt_reproducer.append(
                'curl -LO {}'.format(
                    artifacts_location(
                        self,
                        os.path.relpath(os.path.join(
                            schedule_entry.repodir, schedule_entry.tmt_env_file)), logger=self.logger)
                )
            )

        command.extend([
            'plan',
            '--name', r'^{}$'.format(re.escape(schedule_entry.plan))
        ])

        if self.test_filter:
            command.extend([
                'tests',
                '--filter',
                self.test_filter
            ])

        command.extend([
            'provision', '-h', 'artemis', '--update',
            '-k', artemis_ssh_key,
            '--api-url', artemis_api_url,
            '--api-version', artemis_api_version,
            '--keyname', artemis_key,
        ])

        # add tmt reproducer suitable for local execution
        schedule_entry.tmt_reproducer.append(' '.join(command))

        if schedule_entry.testing_environment.compose:
            command.extend(['--image', cast(str, schedule_entry.testing_environment.compose)])
        if schedule_entry.testing_environment.arch:
            command.extend(['--arch', cast(str, schedule_entry.testing_environment.arch)])
        if artemis_skip_prepare_verify_ssh:
            command.extend(['--skip-prepare-verify-ssh'])
        if artemis_post_install_script:
            command.extend(['--post-install-script', artemis_post_install_script])

        # run plan via tmt, note that the plan MUST be run in the artifact_dirpath
        try:
            tmt_output = Command(command).run(
                cwd=schedule_entry.repodir,
                inspect=True,
                inspect_callback=create_inspect_callback(schedule_entry.logger),
                env=tmt_process_environment or None
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
                    name=schedule_entry.id,
                    result=RESULT_OUTCOME['fail'],
                    artifacts=[
                        TestArtifact(name='testout.log', path=tmt_log_filepath),
                        TestArtifact(name='log_dir', path=os.path.split(tmt_log_filepath)[0]),
                    ],
                    checks=[]
                )
            ], {}

        # gather and return overall plan run result and test results
        return gather_plan_results(self, schedule_entry, work_dirpath, self.option('recognize-errors'))

    def run_test_schedule_entry(self, schedule_entry: TestScheduleEntry) -> None:

        # this schedule entry is not ours, move it along
        if schedule_entry.runner_capability != 'tmt':
            self.overloaded_shared('run_test_schedule_entry', schedule_entry)
            return

        self.shared('trigger_event', 'test-schedule-runner-sti.schedule-entry.started',
                    schedule_entry=schedule_entry)

        # schedule_entry.work_dirpath is created during test schedule creation, see 'create_test_schedule' function
        assert schedule_entry.work_dirpath

        tmt_log_filepath = os.path.join(schedule_entry.work_dirpath, TMT_LOG)
        schedule_entry.tmt_reproducer_filepath = os.path.join(schedule_entry.work_dirpath, TMT_REPRODUCER)

        artifacts = artifacts_location(self, tmt_log_filepath, logger=schedule_entry.logger)

        schedule_entry.info('TMT logs are in {}'.format(artifacts))

        plan_result, test_results, guests = self._run_plan(
            schedule_entry, schedule_entry.work_dirpath, tmt_log_filepath)

        schedule_entry.result = plan_result
        schedule_entry.results = test_results
        schedule_entry.guests = guests

        log_dict(schedule_entry.debug, 'results', test_results)

        self.shared('trigger_event', 'test-schedule-runner-sti.schedule-entry.finished',
                    schedule_entry=schedule_entry)

    def serialize_test_schedule_entry_results(self, schedule_entry: TestScheduleEntry, test_suite: TestSuite) -> None:

        if schedule_entry.runner_capability != 'tmt':
            self.overloaded_shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)
            return

        if schedule_entry.work_dirpath:
            workdir_href = artifacts_location(self, schedule_entry.work_dirpath, logger=schedule_entry.logger)
            test_suite.logs.append(Log(href=workdir_href, name='workdir'))

            tmt_log_filepath = os.path.join(schedule_entry.work_dirpath, TMT_LOG)
            tmt_log_href = artifacts_location(self, tmt_log_filepath, logger=schedule_entry.logger)
            test_suite.logs.append(Log(href=tmt_log_href, name='tmt-log'))

            if isinstance(schedule_entry.guest, ArtemisGuest) and schedule_entry.guest.console_log_file:
                console_log_filepath = os.path.join(schedule_entry.work_dirpath, schedule_entry.guest.console_log_file)
                console_log_href = artifacts_location(self, console_log_filepath)
                test_suite.logs.append(Log(href=console_log_href, name='console.log'))

        if schedule_entry.tmt_reproducer_filepath:
            href = artifacts_location(self, schedule_entry.tmt_reproducer_filepath, logger=schedule_entry.logger)
            test_suite.logs.append(Log(href=href, name='tmt-reproducer'))

        if not schedule_entry.results:
            return

        for task in schedule_entry.results:
            # artifacts
            guest = schedule_entry.guests[task.guest.name] if task.guest and schedule_entry.guests else None
            test_case = TestCase(
                name=task.name,
                result=task.result,
                note=task.note,
                checks=task.checks,
                guest=Guest(
                    name=task.guest.name,
                    role=task.guest.role,
                    environment=TestingEnvironment(arch=guest.arch, compose=guest.image) if guest else None
                ) if task.guest else None,
                serial_number=task.serial_number
            )

            if task.result == 'failed':
                test_case.failure = True

            if task.result == 'error':
                test_case.error = True

            for artifact in task.artifacts:
                path = artifacts_location(self, artifact.path, logger=schedule_entry.logger)

                schedule_entry.outputs.append(
                    TestScheduleEntryOutput(
                        stage=TestScheduleEntryStage.RUNNING,
                        label=artifact.name,
                        log_path=path,
                        additional_data=None
                    )
                )

                test_case.logs.append(Log(
                    href=path,
                    name=artifact.name,
                    schedule_stage='running',
                    schedule_entry=schedule_entry.id
                ))

                # test output can contain invalid utf characters, make sure to replace them
                if os.path.isfile(artifact.path):
                    with open(artifact.path, 'r', errors='replace') as f:
                        test_case.system_out.append(f.read())

            plan_path = safe_name(schedule_entry.plan[1:])
            assert schedule_entry.work_dirpath is not None
            test_suite.test_cases.append(test_case)
        for test_case in test_suite.test_cases:
            if test_case.guest and (test_case.guest.name, test_case.guest.role) not in [
                (guest.name, guest.role) for guest in test_suite.guests
            ]:
                test_suite.guests.append(test_case.guest)
