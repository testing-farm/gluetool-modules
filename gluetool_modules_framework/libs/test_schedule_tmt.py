# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0
#
# This file contains common code user in:
# * gluetool_modules_framework/testing/test_schedule_tmt.py
# * gluetool_modules_framework/testing/test_schedule_tmt_multihost.py
import datetime
from attr import validators
import attrs
import enum
from enum import StrEnum
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Union, cast
import re

import cattrs

from gluetool_modules_framework.libs.test_schedule import TestScheduleResult
from gluetool_modules_framework.libs.results import TestCaseCheck, TestCaseSubresult, FmfId
from gluetool_modules_framework.testing_farm.testing_farm_request import Artifact

from gluetool.log import ContextAdapter, Logging, log_dict


# TMT run log file
TMT_LOG = 'tmt-run.log'
TMT_REPRODUCER = 'tmt-reproducer.sh'
TMT_VERBOSE_LOG = 'log.txt'

# File with environment variables
TMT_ENV_FILE = 'tmt-environment-{}.yaml'

# Maximum size of logs read in MiB
DEFAULT_RESULT_LOG_MAX_SIZE = 10

# Discovered YAML file, contains list of tests, relative to plan workdir
DISCOVERED_TESTS_YAML = "discover/tests.yaml"

# Results YAML file, contains list of test run results, relative to plan workdir
RESULTS_YAML = "execute/results.yaml"

# Weight of a test result, used to count the overall result. Higher weight has precendence
# when counting the overall result. See https://tmt.readthedocs.io/en/latest/spec/steps.html#execute
RESULT_WEIGHT = {
    'skip': 0,
    'pass': 1,
    'info': 1,
    'fail': 2,
    'warn': 2,
    'pending': 3,
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
    'skip': 'not_applicable',
    'pending': 'pending',
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


def safe_name(name: str) -> str:
    """
    A safe variant of the name which does not contain special characters.

    Spaces and other special characters are removed to prevent problems with
    tools which do not expect them (e.g. in directory names).

    Workaround for https://github.com/teemtee/tmt/issues/1857
    """

    return re.sub(r"[^\w/-]+", "-", name).strip("-")


# TFT-4839: refuse unsafe SSH options configured via the `unsafe-ssh-options` module option.
# Directive names are matched case-insensitively and ignoring separators.
def normalize_ssh_directive(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.lower())


def _dangerous_ssh_option_directive(option: str, directives: FrozenSet[str]) -> Optional[str]:
    """
    If the given SSH option (the value passed to ``ssh -o``, e.g. ``ProxyCommand=...`` or ``ProxyCommand ...``)
    uses one of the dangerous directives, return the directive as written, otherwise ``None``.
    """

    # An SSH option is `Directive=value` or `Directive value`; we only care about the directive name.
    directive = re.split(r'[=\s]', option.strip(), maxsplit=1)[0]

    if normalize_ssh_directive(directive) in directives:
        return directive

    return None


def _dangerous_ssh_env_directive(name: str, directives: FrozenSet[str]) -> Optional[str]:
    """
    If the given environment variable name would make tmt inject a dangerous SSH option, return the variable name,
    otherwise ``None``. tmt turns ``TMT_SSH_<NAME>=<value>`` into ``-o<Name>=<value>`` (see
    ``tmt.guest.configure_ssh_options``).
    """

    match = re.match(r'TMT_SSH_([a-zA-Z_]+)', name)
    if not match:
        return None

    if normalize_ssh_directive(match.group(1)) in directives:
        return name

    return None


def detect_unsafe_ssh_options(
    ssh_options: Optional[List[str]] = None,
    environment: Optional[Dict[str, str]] = None,
    directives: Optional[FrozenSet[str]] = None,
) -> List[str]:
    """
    Return human-readable descriptions of any dangerous SSH options found in the given plan ``ssh-option`` list
    and/or ``tmt`` process environment. See TFT-4839 for details.

    :param ssh_options: SSH options coming from the plan ``provision.ssh-option`` metadata.
    :param environment: environment variables passed to the ``tmt`` process.
    :param directives: set of unsafe directives to match against, already normalized via
        :py:func:`normalize_ssh_directive`. When omitted, no directives are refused.
    """

    if directives is None:
        directives = frozenset()

    offenders: List[str] = []

    for option in ssh_options or []:
        directive = _dangerous_ssh_option_directive(option, directives)
        if directive:
            offenders.append("plan ssh-option '{}'".format(directive))

    for name in sorted(environment or {}):
        if _dangerous_ssh_env_directive(name, directives):
            offenders.append("environment variable '{}'".format(name))

    return offenders


# https://tmt.readthedocs.io/en/latest/overview.html#exit-codes
class TMTExitCodes(enum.IntEnum):
    TESTS_PASSED = 0
    TESTS_FAILED = 1
    TESTS_ERROR = 2
    RESULTS_MISSING = 3
    TESTS_SKIPPED = 4


class TmtArtifactProviderType(StrEnum):
    KOJI_TASK = 'koji.task'
    KOJI_NVR = 'koji.nvr'
    BREW_TASK = 'brew.task'
    BREW_NVR = 'brew.nvr'
    COPR_BUILD = 'copr.build'
    REPOSITORY_URL = 'repository-url'
    REPOSITORY_FILE = 'repository-file'


@attrs.define
class TmtArtifactProvider:
    """Stores tmt ``--provide`` provider prefixes for artifact installation."""
    id_provider: TmtArtifactProviderType
    nvr_provider: Optional[TmtArtifactProviderType] = None

    _PROVIDERS: ClassVar[Dict[str, 'TmtArtifactProvider']] = {}

    @classmethod
    def register(cls, artifact_type: str, id_provider: TmtArtifactProviderType,
                 nvr_provider: Optional[TmtArtifactProviderType] = None) -> None:
        cls._PROVIDERS[artifact_type] = cls(id_provider=id_provider, nvr_provider=nvr_provider)

    @classmethod
    def provide_id(cls, artifact: Artifact) -> Optional[str]:
        """
        Map a Testing Farm artifact to a tmt ``--provide`` identifier.

        Returns ``None`` for unsupported artifact types.
        """

        mapping = cls._PROVIDERS.get(artifact.type)

        if not mapping:
            return None

        if artifact.id.isdigit() or not mapping.nvr_provider:
            provider = mapping.id_provider
        else:
            provider = mapping.nvr_provider

        return '{}:{}'.format(provider, artifact.id)


@attrs.define(kw_only=True)
class TMTDiscoveredTest:
    """
    Represents an element in `tests.yaml` file generated by tmt in the discovery phase.
    The file contains a list of discovered tests (list['TMTDiscoveredTest']).
    The actual file contains more items, this class lists only those that are used in the
    gluetool module.

    Instances of this class will be futher parsed into `TestResult` instances.

    For the complete defintion of the `tests.yaml` file, see:
    * https://tmt.readthedocs.io/en/stable/spec/plans.html#discover
    """
    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    # According to the tmt documentation, contact is optional.
    # https://github.com/teemtee/tmt/blob/main/tmt/schemas/test.yaml
    contact: List[str] = attrs.field(
        default=[],
        validator=attrs.validators.deep_iterable(
            member_validator=attrs.validators.instance_of(str),
            iterable_validator=attrs.validators.instance_of(list)
        )
    )
    serial_number: int = attrs.field(validator=validators.instance_of(int))

    @classmethod
    def _structure(cls, data: Dict[str, Any]) -> 'TMTDiscoveredTest':
        return TMTDiscoveredTest(name=data["name"],
                                 contact=data.get("contact", []),
                                 serial_number=data["serial-number"]
                                 )


def get_test_contacts(test_name: str, test_serial_number: Union[int, None],
                      tests: List[TMTDiscoveredTest]) -> List[str]:
    """
    Extracts contacts from a list of tests for a specific test name..

    Args:
    * test_name: Name of the test
    * test: TMTDiscoveredTest instance
    * test_serial_number: Serial number of the test

    Returns:
    * List of contacts or None
    """
    for test in tests:
        if test_name == test.name and test_serial_number == test.serial_number:
            return test.contact
    return []


@attrs.define
class TMTResultGuest:
    name: str = attrs.field(validator=attrs.validators.instance_of(str))
    role: Optional[str] = attrs.field(validator=attrs.validators.optional(attrs.validators.instance_of(str)))


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
    end_time: Optional[str] = attrs.field(validator=attrs.validators.optional(attrs.validators.instance_of(str)))
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
class TMTResultFmfId:
    url: Optional[str] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(str))
    )
    ref: Optional[str] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(str))
    )
    name: Optional[str] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(str))
    )
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
    web_link: Optional[str] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(str))
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
            web_link=data.get('web-link'),
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


@attrs.define(kw_only=True)
class TMTPlanProvision:
    """
    Represents the "provision" step of a TMT plan. See :py:class:`TMTPlan` for more information.
    """
    how: Optional[str] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(str))
    )
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
    pool: Optional[str] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(str))
    )
    ssh_option: Optional[List[str]] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.deep_iterable(
            member_validator=attrs.validators.instance_of(str),
            iterable_validator=attrs.validators.instance_of(list)
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

    @classmethod
    def _structure(cls, data: Dict[str, Any]) -> 'TMTPlanProvision':
        # tmt's `ssh-option` is a list, but the fmf metadata also allows a single string - normalize to a list
        ssh_option = data.get('ssh-option')
        if isinstance(ssh_option, str):
            ssh_option = [ssh_option]

        return TMTPlanProvision(
            how=data.get('how'),
            hardware=data.get('hardware'),
            kickstart=data.get('kickstart'),
            pool=data.get('pool'),
            ssh_option=ssh_option,
            watchdog_dispatch_delay=data.get('watchdog-dispatch-delay'),
            watchdog_period_delay=data.get('watchdog-period-delay'),
        )


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
        )
    )

    @classmethod
    def _structure(cls, data: Dict[str, Any], converter: cattrs.Converter) -> 'TMTPlan':
        """
        In a TMT plan, the "prepare" and "provision" steps can be defined either as a single element or a list of
        elements. This converter does the job of converting both possible shapes into a single one - list of
        :py:class:`TMTPlanPrepare` or :py:class:`TMTPlanProvision` instances.

        See the docs for more information about the "prepare" step:
        * https://tmt.readthedocs.io/en/stable/spec/plans.html#prepare
        """

        provision = data.get('provision', []) \
            if isinstance(data.get('provision', []), list) \
            else [data.get('provision')]
        prepare = data.get('prepare', []) if isinstance(data.get('prepare', []), list) else [data.get('prepare')]
        return TMTPlan(
            name=data['name'],
            provision=converter.structure(provision, List[TMTPlanProvision]),
            prepare=converter.structure(prepare, List[TMTPlanPrepare])
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

            # no exclude in the step, continue processing the next step
            if step.exclude is None:
                continue

            log_dict(
                logger.info,
                "Excluded packages from installation for '{}' plan".format(self.name),
                step.exclude
            )

            excludes.extend(step.exclude)

        return excludes


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
    web_link: Optional[str] = None
