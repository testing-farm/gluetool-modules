# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import re
import os
import os.path
import stat
import sys
import tempfile
import datetime

import enum
import six
import attrs
import cattrs

import gluetool
from gluetool import GlueError, GlueCommandError, Module
from gluetool.action import Action
from gluetool.log import Logging, format_blob, log_blob, log_dict
from gluetool.log import ContextAdapter, LoggingFunctionType  # Ignore PyUnusedCodeBear
from gluetool.utils import Command, from_yaml, load_yaml, create_cattrs_unserializer

from gluetool_modules_framework.libs import create_inspect_callback
from gluetool_modules_framework.libs.artifacts import artifacts_location
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment, dict_nested_value
from gluetool_modules_framework.libs.test_schedule import TestSchedule, TestScheduleResult, TestScheduleEntryOutput, \
    TestScheduleEntryStage, TestScheduleEntryAdapter, TestScheduleEntryState, sanitize_name
from gluetool_modules_framework.libs.test_schedule import TestScheduleEntry as BaseTestScheduleEntry
from gluetool_modules_framework.libs.test_schedule_tmt import \
    DEFAULT_RESULT_LOG_MAX_SIZE, \
    DISCOVERED_TESTS_YAML, \
    PLAN_OUTCOME, PLAN_OUTCOME_WITH_ERROR, \
    RESULTS_YAML, RESULT_OUTCOME, RESULT_WEIGHT, \
    TMTDiscoveredTest, TMTDiscoveredTest, TMTExitCodes, \
    TMT_ENV_FILE, TMT_LOG, TMT_REPRODUCER, TMT_VERBOSE_LOG, \
    get_test_contacts, safe_name
from gluetool_modules_framework.testing_farm.testing_farm_request import TestingFarmRequest
from gluetool_modules_framework.libs.git import RemoteGitRepository
from gluetool_modules_framework.provision.artemis import ArtemisGuest

# Type annotations
from typing import cast, Any, Dict, List, Optional, Tuple, Union, Set  # noqa

from gluetool_modules_framework.libs.results import TestSuite, Log, TestCase, TestCaseCheck, \
    Guest, TestCaseSubresult, Property, FmfId
from secret_type import Secret

GUESTS_YAML = "provision/guests.yaml"


@attrs.define(frozen=True)
class TestArtifact:
    """
    Represents a test output artifact (in particular, log files)
    """

    # Compare artifact uniqueness on `path` attribute only. We might attempt to store a single artifact under various
    # names into results, so store them first into a set and allow only one occurrence of each `path`.
    name: str = attrs.field(eq=False, hash=False)
    path: str


@attrs.define(kw_only=True)
class TestResult:
    """
    Gluetool representation of a test run result

    :ivar name: name of the test.
    :ivar result: test result.
    :ivar artifacts: artifacts/log files declared by the test
    :ivar note: notes attached to the test result.
    :ivar checks: list of checks attached to the test result.
    :ivar guest: guest on which the test was run.
    :ivar serial_number: serial number of the test.
    :ivar duration: duration of the test.
    :ivar start_time: start time of the test.
    :ivar end_time: end time of the test.
    :ivar subresults: subresults of the test.
    :ivar contacts: list of contacts attached to the test result.
    """
    name: str
    result: str
    artifacts: List[TestArtifact]
    note: List[str] = attrs.field(factory=list)
    checks: List[TestCaseCheck]
    guest: Optional['TMTResultGuest'] = None
    serial_number: Optional[int] = None
    duration: Optional[datetime.timedelta] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    subresults: List[TestCaseSubresult]
    contacts: List[str] = attrs.field(factory=list)
    fmf_id: Optional[FmfId] = None


@attrs.define
class TMTResultCheck:
    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    result: str = attrs.field(validator=attrs.validators.instance_of(str))
    event: str = attrs.field(validator=attrs.validators.instance_of(str))
    log: List[str] = attrs.field(validator=attrs.validators.deep_iterable(
        member_validator=attrs.validators.instance_of(str),
        iterable_validator=attrs.validators.instance_of(list)
    ))


@attrs.define(kw_only=True)
class TMTResultSubresult:
    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    result: str = attrs.field(validator=attrs.validators.instance_of(str))
    original_result: str = attrs.field(validator=attrs.validators.instance_of(str))
    end_time: str = attrs.field(validator=attrs.validators.instance_of(str))
    log: List[str] = attrs.field(validator=attrs.validators.deep_iterable(
        member_validator=attrs.validators.instance_of(str),
        iterable_validator=attrs.validators.instance_of(list)
    ))

    @classmethod
    def _structure(cls, data: Dict[str, Any]) -> 'TMTResultSubresult':
        return TMTResultSubresult(
            name=data['name'],
            result=data['result'],
            original_result=data['original-result'],
            end_time=data['end-time'],
            log=data['log'],
        )


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
    arch: Optional[str] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(str))
    )


@attrs.define
class TMTResultGuest:
    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    role: Optional[str] = attrs.field(validator=attrs.validators.optional(attrs.validators.instance_of(str)))


@attrs.define
class TMTResultFmfId:
    url: str = attrs.field(validator=attrs.validators.instance_of(str))
    ref: str = attrs.field(validator=attrs.validators.instance_of(str))
    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    path: Optional[str] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(str))
    )


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
    note: List[str] = attrs.field(validator=attrs.validators.deep_iterable(
        member_validator=attrs.validators.instance_of(str),
        iterable_validator=attrs.validators.instance_of(list)
    ))
    check: List[TMTResultCheck] = attrs.field(validator=attrs.validators.deep_iterable(
        member_validator=attrs.validators.instance_of(TMTResultCheck),
        iterable_validator=attrs.validators.instance_of(list)
    ))
    serial_number: Optional[int] = attrs.field(validator=attrs.validators.optional(attrs.validators.instance_of(int)))
    duration: Optional[datetime.timedelta] = attrs.field(
        validator=attrs.validators.optional(attrs.validators.instance_of(datetime.timedelta)),
    )
    start_time: Optional[str] = attrs.field(validator=attrs.validators.optional(attrs.validators.instance_of(str)))
    end_time: Optional[str] = attrs.field(validator=attrs.validators.optional(attrs.validators.instance_of(str)))
    subresult: List[TMTResultSubresult] = attrs.field(validator=attrs.validators.deep_iterable(
        member_validator=attrs.validators.instance_of(TMTResultSubresult),
        iterable_validator=attrs.validators.instance_of(list)
    ))
    fmf_id: Optional[TMTResultFmfId] = attrs.field(
        validator=attrs.validators.optional(attrs.validators.instance_of(TMTResultFmfId))
    )

    @classmethod
    def _structure(cls, data: Dict[str, Any], converter: cattrs.Converter) -> 'TMTResult':
        note = (data.get('note') or []) if isinstance((data.get('note') or []), list) else [data.get('note')]
        duration = None
        if data['duration'] is not None:
            hours, minutes, seconds = map(int, data['duration'].split(':'))
            duration = datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds)

        return TMTResult(
            name=data['name'],
            result=data['result'],
            log=data['log'],
            guest=converter.structure(data['guest'], TMTResultGuest),
            note=cast(List[str], note),
            check=converter.structure(data['check'], List[TMTResultCheck]),
            serial_number=data.get('serial-number'),
            duration=duration,
            start_time=data['start-time'],
            end_time=data['end-time'],
            subresult=converter.structure(data['subresult'], List[TMTResultSubresult]),
            fmf_id=converter.structure(data['fmf-id'], TMTResultFmfId) if data['fmf-id'] else None,
        )


@attrs.define
class TMTPlanProvision:
    """
    Represents the "provision" step of a TMT plan. See :py:class:`TMTPlan` for more information.
    """
    how: Optional[str] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(str))
    )


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

    @classmethod
    def _structure(cls, data: Dict[str, Any], converter: cattrs.Converter) -> 'TMTPlan':
        """
        In a TMT plan, the "provision" step can be defined either as a single element or a list of
        elements. This converter does the job of converting both possible shapes into a single one - list of
        :py:class:`TMTPlanProvision` instances.
        """

        provision = data.get('provision', []) \
            if isinstance(data.get('provision', []), list) \
            else [data.get('provision')]
        return TMTPlan(
            name=data['name'],
            provision=converter.structure(provision, List[TMTPlanProvision]),
        )


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
    discovered_tests: List[TMTDiscoveredTest] = []

    # TMT uses plan name as a relative directory to the working directory, but
    # plan start's with '/' character, strip it so we can use it with os.path.join
    plan_path = safe_name(schedule_entry.plan[1:])

    results_yaml = os.path.join(work_dir, plan_path, RESULTS_YAML)
    guests_yaml = os.path.join(work_dir, plan_path, GUESTS_YAML)
    discovered_tests_yaml = os.path.join(work_dir, plan_path, DISCOVERED_TESTS_YAML)

    if not os.path.exists(results_yaml):
        schedule_entry.warn("Could not find results file '{}' containing tmt results".format(results_yaml), sentry=True)
        return TestScheduleResult.ERROR, test_results, guests

    # load test results from `results.yaml` which is created in tmt's execute step
    # https://tmt.readthedocs.io/en/latest/spec/steps.html#execute
    try:
        results = load_yaml(results_yaml, unserializer=gluetool.utils.create_cattrs_unserializer(List[TMTResult]))
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

    try:
        discovered_tests = load_yaml(discovered_tests_yaml,
                                     unserializer=gluetool.utils.create_cattrs_unserializer(List[TMTDiscoveredTest]))
        log_dict(schedule_entry.debug,
                 "loaded discovered tests from '{}'".format(discovered_tests_yaml),
                 discovered_tests)
    except GlueError as error:
        schedule_entry.warn('Could not load discovered tests.yaml file: {}'.format(error))
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

        # `artifacts_dir` is e.g. `work-sanitywft7y56u/testing-farm/sanity/execute`
        artifacts_dir = os.path.join(work_dir, plan_path, 'execute')
        artifacts = set()

        # attach the artifacts directory itself, useful for browsing;
        # usually all artifacts are in the same dir
        if result.log:
            # `log_dir` is e.g. `data/guest/default-0/testing-farm/script-1`
            # NOTE: `logs[0]` might possibly be an unexpected directory when dealing with tests with custom results.
            # `logs[0]` is expected to be e.g. `data/guest/default-0/testing-farm/script-1/output.txt` but users are
            # free to influence this results entry.
            log_dir = os.path.normpath(os.path.join(artifacts_dir, os.path.dirname(logs[0])))
            artifacts.add(TestArtifact(
                name='log_dir',
                path=log_dir
            ))
            artifacts.add(TestArtifact(
                name='data',
                path=os.path.join(log_dir, 'data')
            ))

            # list all artifacts under 'data'
            for dir, _, files in os.walk(os.path.join(log_dir, 'data')):
                for file in files:
                    artifacts.add(TestArtifact(
                        name=os.path.join(dir, file).removeprefix(log_dir).lstrip('/'),
                        path=os.path.join(dir, file)
                    ))

            # the first log is the main one for developers, traditionally called "testout.log"
            testout = logs.pop(0)
            artifacts.add(TestArtifact(name='testout.log', path=os.path.join(artifacts_dir, testout)))

        # attach all other logs; name them after their filename; eventually, tmt results.yaml should
        # allow more meta-data, like declaring a HTML viewer
        for log in logs:
            artifacts.add(TestArtifact(name=os.path.basename(log), path=os.path.join(artifacts_dir, log)))

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

        subresults = [
            TestCaseSubresult(
                name=subresult.name,
                result=subresult.result,
                original_result=subresult.original_result,
                end_time=subresult.end_time,
                logs=[
                    Log(
                        href=artifacts_location(module, os.path.join(artifacts_dir, log), logger=schedule_entry.logger),
                        name=os.path.basename(log)
                    )
                    for log in subresult.log
                ]
            ) for subresult in result.subresult
        ]

        test_results.append(TestResult(
            name=result.name,
            result=outcome,
            artifacts=sorted(list(artifacts), key=lambda artifact: artifact.path),
            guest=result.guest,
            checks=checks,
            note=result.note,
            duration=result.duration,
            start_time=result.start_time,
            end_time=result.end_time,
            serial_number=result.serial_number,
            subresults=subresults,
            contacts=get_test_contacts(result.name, result.serial_number, discovered_tests),
            # NOTE: We're creating a new branch with 'gluetool/' prefixing the ref when checking out requested ref.
            # Removing the prefix here from the results.
            fmf_id=FmfId(
                url=result.fmf_id.url,
                ref=result.fmf_id.ref.removeprefix('gluetool/'),
                name=result.fmf_id.name,
                path=result.fmf_id.path,
            ) if result.fmf_id is not None else None,
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
            'accepted-environment-secrets': {
                'help': """
                        A comma delimited list of accepted environment variable names for the ``tmt`` process.
                        In case an environment a variable is provided, which does not match this list, the execution
                        is aborted. These variables are intended for storing secrets and with the use of hide-secrets
                        module, they will be hidden in artifacts.
                        """,
                'action': 'append',
                'default': []
            },
            'environment-variables': {
                'help': """
                        A comma delimited list of additional environment variables and their values
                        for the ``tmt`` process.
                        """,
                'action': 'append',
                'default': [],
                'metavar': 'KEY1=VAL1,KEY2=VAL2'
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
            'result-log-max-size': {
                'help': 'Maximum size of a result log read, in MiB. (default: %(default)s).',
                'default': DEFAULT_RESULT_LOG_MAX_SIZE,
                'type': int
            }
        }),
    ]

    shared_functions = ['create_test_schedule', 'run_test_schedule_entry', 'serialize_test_schedule_entry_results']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(TestScheduleTMTMultihost, self).__init__(*args, **kwargs)

    @gluetool.utils.cached_property
    def accepted_environment_variables(self) -> List[str]:
        return gluetool.utils.normalize_multistring_option(self.option('accepted-environment-variables'))

    @gluetool.utils.cached_property
    def accepted_environment_secrets(self) -> List[str]:
        return gluetool.utils.normalize_multistring_option(self.option('accepted-environment-secrets'))

    @gluetool.utils.cached_property
    def environment_variables(self) -> Dict[str, Any]:
        options = gluetool.utils.normalize_multistring_option(self.option('environment-variables'))

        if not options:
            return {}

        rendered_options = [
            gluetool.utils.render_template(option, **self.shared('eval_context'))
            for option in options
        ]

        return {
            keyval.split('=')[0]: keyval.split('=')[1]
            for keyval in rendered_options
        }

    @gluetool.utils.cached_property
    def test_filter(self) -> Optional[str]:
        if self.option('test-filter'):
            return str(self.option('test-filter'))

        tf_request = cast(Optional[TestingFarmRequest], self.shared('testing_farm_request'))
        if tf_request and tf_request.tmt and tf_request.tmt.test_filter:
            return tf_request.tmt.test_filter

        return None

    @gluetool.utils.cached_property
    def test_name(self) -> Optional[str]:
        tf_request = cast(Optional[TestingFarmRequest], self.shared('testing_farm_request'))
        if tf_request and tf_request.tmt and tf_request.tmt.test_name:
            return tf_request.tmt.test_name

        return None

    def user_data(self, schedule_entry: TestScheduleEntry) -> List[str]:
        artemis_options: Dict[str, Any] = self.shared('artemis_api_options')
        context = self.shared('eval_context')
        user_data = {}

        # Parse and template user-data-vars from YAML
        user_data_tpl_filepath = artemis_options.get('user-data-vars-template-file')

        if user_data_tpl_filepath is not None:
            user_data.update({
                key: gluetool.utils.render_template(str(value), logger=self.logger, **context)
                for key, value in gluetool.utils.load_yaml(user_data_tpl_filepath, logger=self.logger).items()
            })

        if schedule_entry.testing_environment:
            tags = ((schedule_entry.testing_environment.settings or {}).get('provisioning') or {}).get('tags') or {}
            if tags:
                user_data.update(tags)

        log_dict(self.debug, 'user-data', user_data)

        user_data_formatted = []
        for key, value in user_data.items():
            user_data_formatted.append('{}={}'.format(key, value))

        return user_data_formatted

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

    def _is_plan_empty(self,
                       plan: str,
                       tmt_env_file: Optional[str],
                       repodir: str,
                       testing_environment: TestingEnvironment,
                       work_dirpath: str,
                       test_filter: Optional[str] = None,
                       test_name: Optional[str] = None) -> bool:
        """
        Return ``True`` if plan would have no tests after applying test selectors, otherwise return ``False``.
        """

        test_filter = test_filter or self.test_filter
        test_name = test_name or self.test_name

        command = [
            self.option('command')
        ]

        command.extend(self._root_option)

        te_tmt = testing_environment.tmt

        if te_tmt and 'context' in te_tmt:
            command.extend(self._tmt_context_to_options(te_tmt['context']))

        command.append('run')

        if tmt_env_file:
            env_options = [
                '-e', '@{}'.format(tmt_env_file)
            ]
            command.extend(env_options)

        discover_extra_args = dict_nested_value(te_tmt, 'extra_args', 'discover')
        if discover_extra_args:
            for extra_args in discover_extra_args:
                command.extend(['discover'] + gluetool.utils.normalize_shell_option(extra_args))
        else:
            command.append('discover')

        command.extend(['plan', '--name', '^{}$'.format(plan)])

        if test_filter or test_name:
            command.extend(['test'])

        if test_filter:
            command.extend(['--filter', test_filter])

        if test_name:
            command.extend(['--name', test_name])

        try:
            tmt_output = Command(command).run(cwd=repodir)

        except GlueCommandError as exc:
            # It can happen that test discovery will report `No plans found`
            if exc.output.stderr and 'No plans found' in exc.output.stderr:
                return True

            log_blob(
                self.error,
                "Failed to discover tests",
                exc.output.stderr or exc.output.stdout or '<no output>'
            )
            raise GlueError('Failed to discover tests, TMT metadata are absent or corrupted.')

        self._save_output(tmt_output, os.path.join(work_dirpath, 'tmt-discover.log'))

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
            exported_plans = from_yaml(output, unserializer=create_cattrs_unserializer(List[TMTPlan]))
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

        if self.has_shared('testing_farm_request'):
            self.info('fetching in-repository config')
            if repository.testing_farm_config and repository.remote_url:
                tf_request = cast(TestingFarmRequest, self.shared('testing_farm_request'))
                tf_request.modify_with_config(repository.testing_farm_config, repository.remote_url)

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

            for plan in plans:
                tmt_env_file = self._prepare_tmt_env_file(tec, plan, repodir)

                # Prepare environment for test schedule entry execution
                schedule_entry = TestScheduleEntry(root_logger, tec, plan, repodir)
                work_dirpath = self._prepare_environment(schedule_entry)
                schedule_entry.work_dirpath = work_dirpath

                if self._is_plan_empty(plan, tmt_env_file, repodir, tec, work_dirpath):
                    self.info("skipping empty plan '{}'".format(plan))
                    schedule_entry.stage = TestScheduleEntryStage.COMPLETE
                    schedule_entry.state = TestScheduleEntryState.OK
                    schedule_entry.result = TestScheduleResult.SKIPPED
                    schedule.append(schedule_entry)
                    continue

                exported_plan = self.export_plan(repodir, plan, tmt_env_file, tec)

                if exported_plan:
                    for provision_phase in exported_plan.provision:
                        if provision_phase.how == 'artemis':
                            self.warn('The `how` key in provision phase should not be `artemis`.')

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

    def _save_output(self, output: gluetool.utils.ProcessOutput, filepath: str) -> None:

        with open(filepath, 'w') as f:
            def _write(label: str, s: str) -> None:
                f.write('{}\n{}\n\n'.format(label, s))

            _write('# STDOUT:', format_blob(cast(str, output.stdout)))
            _write('# STDERR:', format_blob(cast(str, output.stderr)))

            f.flush()

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

        # create environment variables for the tmt process, start with options coming from options
        tmt_process_environment = self.environment_variables.copy()
        tmt_process_environment.update({
            'TMT_PLUGIN_REPORT_REPORTPORTAL_LINK_TEMPLATE': '{}/#{}_{}'.format(
                self.shared('coldstore_url'),
                schedule_entry.work_dirpath,
                r'{{ PLAN_NAME }}_{{ RESULT.serial_number }}_{{ RESULT.guest.name }}'
            )
        })

        def _check_accepted_environment_variables(variables: Dict[str, str]) -> None:
            for key, _ in six.iteritems(variables):
                if key not in self.accepted_environment_variables + self.accepted_environment_secrets:
                    raise GlueError(
                        "Environment variable '{}' is not allowed to be exposed to the tmt process".format(key)
                    )

        def _sanitize_environment_variables(variables: Dict[str, str]) -> str:
            return ' '.join(["{}=hidden".format(key) for key, _ in six.iteritems(variables)])

        # using `# noqa` because flake8 and coala are confused by the walrus operator
        # Ignore PEP8Bear
        if (tmt := schedule_entry.testing_environment.tmt) and 'environment' in tmt and tmt['environment']:  # noqa: E203 E231 E501

            _check_accepted_environment_variables(tmt['environment'])

            schedule_entry.tmt_reproducer.append(
                'export {}'.format(
                    _sanitize_environment_variables(tmt['environment'])
                )
            )

            if self.has_shared('add_secrets'):
                self.shared('add_secrets', [
                    value for key, value in tmt['environment'].items()
                    if value and key in self.accepted_environment_secrets
                ])

            # add environment variables from testing environment
            tmt_process_environment.update(tmt['environment'])

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
        artemis_provision_timeout = artemis_options['ready-timeout']
        artemis_provision_tick = artemis_options['ready-tick']
        artemis_api_timeout = artemis_options['api-call-timeout']

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

        # `discover` step in case of extra arguments
        discover_extra_args = dict_nested_value(schedule_entry.testing_environment.tmt, 'extra_args', 'discover')
        if discover_extra_args:
            for extra_args in discover_extra_args:
                command.extend(['discover'] + gluetool.utils.normalize_shell_option(extra_args))

        # `prepare` step in case of extra arguments
        prepare_extra_args = dict_nested_value(schedule_entry.testing_environment.tmt, 'extra_args', 'prepare')
        if prepare_extra_args:
            for extra_args in prepare_extra_args:
                command.extend(['prepare'] + gluetool.utils.normalize_shell_option(extra_args))

        if self.test_filter or self.test_name:
            command.append('tests')
            if self.test_filter:
                command.extend(['--filter', self.test_filter])
            if self.test_name:
                command.extend(['--name', self.test_name])

        command.extend([
            'provision',
            '-h', 'artemis',
            '--update-missing',
            '--allowed-how', 'container|artemis',
            '-k', artemis_ssh_key,
            '--api-url', artemis_api_url,
            '--api-version', artemis_api_version,
            '--keyname', artemis_key,
            '--provision-timeout', str(artemis_provision_timeout),
            '--provision-tick', str(artemis_provision_tick),
            '--api-timeout', str(artemis_api_timeout),
        ])

        if schedule_entry.testing_environment.compose:
            command.extend(['--image', cast(str, schedule_entry.testing_environment.compose)])
        if schedule_entry.testing_environment.arch:
            command.extend(['--arch', cast(str, schedule_entry.testing_environment.arch)])
        if schedule_entry.testing_environment.pool:
            command.extend(['--pool', schedule_entry.testing_environment.pool])
        if artemis_skip_prepare_verify_ssh:
            command.extend(['--skip-prepare-verify-ssh'])
        if artemis_post_install_script:
            command.extend(['--post-install-script', artemis_post_install_script])

        user_data = self.user_data(schedule_entry)
        if user_data:
            for data_entry in user_data:
                command.extend(['--user-data', data_entry])

        # `finish` step in case of extra arguments
        finish_extra_args = dict_nested_value(schedule_entry.testing_environment.tmt, 'extra_args', 'finish')
        if finish_extra_args:
            for extra_args in finish_extra_args:
                command.extend(['finish'] + gluetool.utils.normalize_shell_option(extra_args))

        # add tmt reproducer suitable for local execution
        schedule_entry.tmt_reproducer.append(' '.join(command))

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
                self._save_output(tmt_output, tmt_log_filepath)
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
                    checks=[],
                    subresults=[],
                    contacts=[],
                )
            ], {}

        # gather overall plan run result and test results
        plan_results, test_results, guests = gather_plan_results(
            self, schedule_entry, work_dirpath, self.option('recognize-errors'))

        # check if tmt exited with an error and show it on the plan level
        if tmt_output.exit_code == TMTExitCodes.TESTS_ERROR:
            schedule_entry.warn('tmt exited with an error, plan results are overwritten to show the error')
            plan_results = TestScheduleResult.ERROR

        return plan_results, test_results, guests

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
            # When a test suite is skipped, it is most likely due to not finding any tests in discover step. Adding
            # discover log to results.
            if schedule_entry.result == TestScheduleResult.SKIPPED:
                tmt_discover_log_href = artifacts_location(
                    self,
                    os.path.join(schedule_entry.work_dirpath, 'tmt-discover.log'),
                    logger=schedule_entry.logger
                )
                log_to_add = Log(href=tmt_discover_log_href, name='tmt-discover-log')
                if log_to_add not in test_suite.logs:
                    test_suite.logs.append(log_to_add)

            workdir_href = artifacts_location(self, schedule_entry.work_dirpath, logger=schedule_entry.logger)
            log_to_add = Log(href=workdir_href, name='workdir')
            if log_to_add not in test_suite.logs:
                test_suite.logs.append(log_to_add)

            tmt_log_filepath = os.path.join(schedule_entry.work_dirpath, TMT_LOG)
            if os.path.exists(tmt_log_filepath):
                tmt_log_href = artifacts_location(self, tmt_log_filepath, logger=schedule_entry.logger)
                log_to_add = Log(href=tmt_log_href, name='tmt-log')
                if log_to_add not in test_suite.logs:
                    test_suite.logs.append(log_to_add)

            tmt_verbose_log_filepath = os.path.join(schedule_entry.work_dirpath, TMT_VERBOSE_LOG)
            if os.path.exists(tmt_verbose_log_filepath):
                tmt_verbose_log_href = artifacts_location(self, tmt_verbose_log_filepath, logger=schedule_entry.logger)
                log_to_add = Log(href=tmt_verbose_log_href, name='tmt-verbose-log')
                if log_to_add not in test_suite.logs:
                    test_suite.logs.append(log_to_add)

            data_filepath = os.path.join(schedule_entry.work_dirpath, safe_name(schedule_entry.plan[1:]), 'data')
            if os.path.exists(data_filepath):
                log_to_add = Log(
                    href=artifacts_location(self, data_filepath, logger=schedule_entry.logger),
                    name='data'
                )
                if log_to_add not in test_suite.logs:
                    test_suite.logs.append(log_to_add)

            if isinstance(schedule_entry.guest, ArtemisGuest) and schedule_entry.guest.guest_logs:
                for log in schedule_entry.guest.guest_logs:
                    log_filepath = os.path.join(
                        schedule_entry.work_dirpath, log.filename.format(guestname=schedule_entry.guest.artemis_id)
                    )
                    log_href = artifacts_location(self, log_filepath)
                    log_to_add = Log(href=log_href, name=log.name)
                    if log_to_add not in test_suite.logs:
                        test_suite.logs.append(log_to_add)

        if schedule_entry.tmt_reproducer_filepath:
            href = artifacts_location(self, schedule_entry.tmt_reproducer_filepath, logger=schedule_entry.logger)
            log_to_add = Log(href=href, name='tmt-reproducer')
            if log_to_add not in test_suite.logs:
                test_suite.logs.append(log_to_add)

        if not schedule_entry.results:
            return

        for task in schedule_entry.results:
            # artifacts
            guest = schedule_entry.guests[task.guest.name] if task.guest and schedule_entry.guests else None
            test_case = TestCase(
                name=task.name,
                result=task.result,
                subresults=task.subresults,
                note=task.note,
                checks=task.checks,
                duration=task.duration,
                start_time=task.start_time,
                end_time=task.end_time,
                guest=Guest(
                    name=task.guest.name,
                    role=task.guest.role,
                    environment=TestingEnvironment(arch=guest.arch, compose=guest.image) if guest else None
                ) if task.guest else None,
                serial_number=task.serial_number,
                fmf_id=task.fmf_id,
            )

            if schedule_entry.work_dirpath:
                test_case.properties.append(
                    Property('id', '{}_{}_{}_{}'.format(
                        schedule_entry.work_dirpath,
                        sanitize_name(test_suite.name, allow_slash=False),
                        test_case.serial_number,
                        test_case.guest.name if test_case.guest else None
                    ))
                )

            if task.result == 'failed':
                test_case.failure = True

            if task.result == 'error':
                test_case.error = True

            if len(task.contacts) > 0:
                test_case.properties.extend([
                    Property('contact', contact) for contact in task.contacts
                ])

            for artifact in task.artifacts:
                path = artifacts_location(self, artifact.path, logger=schedule_entry.logger)

                outputs = TestScheduleEntryOutput(
                        stage=TestScheduleEntryStage.RUNNING,
                        label=artifact.name,
                        log_path=path,
                        additional_data=None
                )
                if outputs not in schedule_entry.outputs:
                    schedule_entry.outputs.append(outputs)

                log_to_add = Log(
                    href=path,
                    name=artifact.name,
                    schedule_stage='running',
                    schedule_entry=schedule_entry.id
                )
                if log_to_add not in test_case.logs:
                    test_case.logs.append(log_to_add)

                # Add test output to system_out, used only for "native" xunit
                # Process only 'testout.log' (output.txt) which contains reasonable human output
                if os.path.isfile(artifact.path) and artifact.name == 'testout.log':
                    # test output can contain invalid utf characters, make sure to replace them
                    with open(artifact.path, 'r', errors='replace') as f:
                        log_size = os.path.getsize(artifact.path)
                        max_log_size = self.option('result-log-max-size') * 1024 * 1024

                        # TFT-3175
                        # The output can be very large and reading it whole can easily lead to OOM.
                        # Send to Sentry to see how many pipelines are affected by this.
                        if log_size > max_log_size:
                            self.warn(
                                "Artifact '{}' is too large - {} bytes, limiting output to {} bytes".format(
                                    artifact.path, log_size, max_log_size
                                ),
                                sentry=True
                            )
                            f.seek(log_size - max_log_size)

                            test_case.system_out.append(
                                "Output too large, limiting output to last {} MiB.\n\n".format(
                                    self.option('result-log-max-size')
                                )
                                + f.read()
                            )
                        else:
                            test_case.system_out.append(f.read())

            plan_path = safe_name(schedule_entry.plan[1:])
            assert schedule_entry.work_dirpath is not None

            if test_case not in test_suite.test_cases:
                test_suite.test_cases.append(test_case)

        for test_case in test_suite.test_cases:
            if test_case.guest and (test_case.guest.name, test_case.guest.role) not in [
                (guest.name, guest.role) for guest in test_suite.guests
            ]:
                test_suite.guests.append(test_case.guest)
