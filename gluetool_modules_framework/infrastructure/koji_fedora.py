# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import collections
import re
import six

import koji
import requests.exceptions

from bs4 import BeautifulSoup
from gluetool_modules_framework.libs.artifacts import splitFilename
from version_utils.rpm import labelCompare

import gluetool
from gluetool import GlueError, SoftGlueError
from gluetool.action import Action
from gluetool.log import ContextAdapter
from gluetool.log import Logging
from gluetool.log import LoggerMixin
from gluetool.log import log_dict
from gluetool.result import Result
from gluetool.utils import cached_property, dict_update, wait, normalize_multistring_option, render_template
from gluetool.utils import IncompatibleOptionsError

from typing import Any, Dict, List, NamedTuple, Optional, Union, Tuple, Callable, Type, cast, overload  # noqa
from typing_extensions import TypedDict, Literal, NotRequired
from gluetool_modules_framework.helpers.rules_engine import ContextType

InitDetailsType = TypedDict(
    'InitDetailsType',
    {
        'url': str,
        'web_url': str,
        'pkgs_url': str,
        'session': Any,
        'automation_user_ids': List[int]
    }
)

DEFAULT_COMMIT_FETCH_TIMEOUT = 300
DEFAULT_COMMIT_FETCH_TICKS = 30
DEFAULT_API_VERSION_RETRY_TIMEOUT = 300
DEFAULT_API_VERSION_RETRY_TICK = 30


class NotBuildTaskError(SoftGlueError):
    def __init__(self, task_id: int) -> None:
        super(NotBuildTaskError, self).__init__('Task is not a build task')

        self.task_id = task_id


#: Information about task architectures.
#:
#: :ivar bool complete: If ``True``, the task was not limited by its issuer to any particular set of architectures.
#:     ``False`` signals the issuer requested task to build its artifact for specific list of architectures.
#: :ivar list(str) arches: List of architectures.
TaskArches = NamedTuple('TaskArches', [('complete', bool), ('arches', List[str])])

#: Represents ``request`` field of API response on ``getTaskInfo`` query for common build task.
#:
#: :ivar str source: source used for the building process.
#: :ivar str target: target the task built for.
#: :ivar dict options: additional task options.
BuildTaskRequest = NamedTuple(
    'BuildTaskRequest', [
        ('source', str),
        ('target', str),
        ('options', Dict[str, str])
    ]
)

#: Represents ``request`` field of API response on ``getTaskInfo`` query for ``buildArch`` task.
#:
#: :ivar str source: source used for the building process.
#: :ivar something: some value of unknown purpose.
#: :ivar str arch: build architecture.
#: :ivar bool keep_srpm: whether the SRPM was stored among artifacts.
#: :ivar dict options: additional task options.
BuildArchTaskRequest = NamedTuple(
    'BuildArchTaskRequest', [
        ('source', str),
        ('something', str),
        ('arch', str),
        ('keep_srpm', bool),
        ('options', Dict[str, str])
    ]
)

#: Represents an image repository
#:
#: :ivar str arch: Image architecture.
#: :ivar str url: Repository URL.
#: :ivar list(str) alternatives: Other URLs leading to the same image as ``url``.
#: :ivar dict manifest: Manifest describing the image in the repository.
ImageRepository = NamedTuple(
    'ImageRepository',
    [
        ('arch', str),
        ('url', str),
        ('alternatives', List[str]),
        ('manifest', Dict[str, List[Dict[str, Any]]])
    ]
)


#: Represents data we need to initialize a Koji task. A task ID would be enough, but, for some tasks,
#: we may need to override some data we'd otherwise get from Koji API.
#:
#: The specific use case: container builds. Container build B1 was built by Brew task T1. Later,
#: there may be a rebuild of B1, thanks to change in the parent image, yielding B2. But: B2 would
#: point to T1! Thankfully, we can initialize with build ID (starting with B2 then), but because
#: our implementation would try to detect task behind B2 - which is, wrongly but officialy, T1 -
#: and use this task for initialization. Task instance would then try to detect build attached to
#: the task, which would be, according to API, B1... Therefore, we'd initialize with B2, but *nothing*
#: in our state would have any connection to B2, because the task behind B2 would be T1, and build
#: created by T1 would be B1.
#:
#: To solve this trap, we need to preserve information about build after we reduce it to a task,
#: and when a task instance is initialized, we'd force this build to be the task is connected to.
#: Most of our code tries to use build when providing artifact attributes like NVR or component,
#: making it the information source number one.
#:
#: Therefore task initializer, to give us a single package we could pass between involved functions.
#:
#: :ivar int task_id: task ID.
#: :ivar int build_id: if set, it as build we should assign to the task. Otherwise we query API
#:     to find out which - if any - build belongs to the task.
TaskInitializer = NamedTuple(
    'TaskInitializer',
    [
        ('task_id', int),
        ('build_id', Optional[int])
    ]
)

TaskInfoType = TypedDict(
    'TaskInfoType',
    {
        'request': Union[BuildTaskRequest, BuildArchTaskRequest],
        'id': int,
        'method': str,
        'owner_id': str,
        'state': str,
        'waiting': bool,
        'owner': int,
        'arch': str,
        'label': NotRequired[str]
    }
)


def _call_api(session: Any, logger: ContextAdapter, method: str, *args: Any, **kwargs: Any) -> Any:
    with Action('query Koji API', parent=Action.current_action(), logger=logger, tags={
        'method': method,
        'positional-arguments': args,
        'keyword-arguments': kwargs
    }):
        method_callable = getattr(session, method)

        return method_callable(*args, **kwargs)


class KojiTask(LoggerMixin, object):
    """
    Provides abstraction of a koji build task, specified by task ID. For initialization
    koji instance details need to be passed via the instance dictionary with the following keys:

        ``session`` - a koji session initialized via the koji.ClientSession function
        ``url`` - a base URL for the koji instance
        ``pkgs_url`` - a base URL for the packages location

    :param dict details: Instance details, see ``required_instance_keys``
    :param int task_id: Initialize from given Koji task ID.
    :param module: Module that created this task instance.
    :param gluetool.log.ContextLogger logger: logger used for logging
    :param int wait_timeout: Wait this many seconds for task to become non-waiting

    :ivar int id: unique ID of the task on the Koji instance.
    """

    ARTIFACT_NAMESPACE = 'koji-build'

    @staticmethod
    def _check_required_instance_keys(details: InitDetailsType) -> None:
        """
        Checks for required instance details for Koji.
        :raises: GlueError if instance is missing some of the required keys
        """
        required_instance_keys = ('session', 'url', 'pkgs_url', 'web_url')

        if not all(key in details for key in required_instance_keys):
            raise GlueError('instance details do not contain all required keys')

    @overload  # noqa F811  # flake8 thinks the method is being redefined but only the types are being overloaded
    def _call_api(self, method: Literal['listBuilds'], *args: Any,  # noqa F811
                  **kwargs: Any) -> Optional[List[Dict[str, str]]]:
        pass

    @overload  # noqa F811
    def _call_api(self, method: Literal['getFullInheritance', 'getTaskChildren', 'listBuildRPMs'], *args: Any,  # noqa F811
                  **kwargs: Any) -> List[Dict[str, str]]:
        pass

    @overload  # noqa F811
    def _call_api(self, method: Literal['listArchives', 'listTagged'], *args: Any,  # noqa F811
                  **kwargs: Any) -> List[Dict[str, Any]]:
        pass

    @overload  # noqa F811
    def _call_api(self, method: Literal['getBuild', 'getUser', 'getTaskResult', 'getBuildTarget'], *args: Any,  # noqa F811
                  **kwargs: Any) -> Dict[str, Any]:
        pass

    @overload  # noqa F811
    def _call_api(self, method: Literal['getTaskInfo'], *args: Any, **kwargs: Any) -> TaskInfoType:  # noqa F811
        pass

    @overload  # noqa F811
    def _call_api(self, method: Literal['listTaskOutput'], *args: Any, **kwargs: Any) -> List[str]:  # noqa F811
        pass

    @overload  # noqa F811
    def _call_api(self, method: Literal['getAPIVersion'], *args: Any, **kwargs: Any) -> str:  # noqa F811
        pass

    def _call_api(self, method: str, *args: Any, **kwargs: Any) -> Any:  # noqa F811
        return _call_api(self.session, self.logger, method, *args, **kwargs)

    def _assign_build(self, build_id: Optional[int]) -> None:

        # Helper method - if build_id is specified, don't give API a chance, use the given
        # build, and emit a warning.

        if build_id is None:
            return

        self._build = self._call_api('getBuild', build_id)

        log_dict(self.debug, 'build for task ID {}'.format(self.id), self._build)

        self.warn('for task {}, build was set explicitly to {}, {}'.format(
            self.id, build_id, self._build.get('nvr', '<unknown NVR>')
        ))

    def __init__(
            self,
            details: InitDetailsType,
            task_id: int,
            module: 'Koji',
            logger: Optional[ContextAdapter] = None,
            wait_timeout: Optional[int] = None,
            build_id: Optional[int] = None,
            artifact_namespace: Optional[str] = None
    ) -> None:

        super(KojiTask, self).__init__(logger or Logging.get_logger())

        self._check_required_instance_keys(details)

        self._module = module

        self.id = self.dispatch_id = int(task_id)  # type: Union[str, int]
        self.api_url = details['url']
        self.web_url = details['web_url']
        self.pkgs_url = details['pkgs_url']
        self.session = details['session']

        if artifact_namespace:
            module.warn("Forcing ARTIFACT_NAMESPACE to '{}'".format(artifact_namespace))
            self.ARTIFACT_NAMESPACE = artifact_namespace

        if not self._is_valid:
            raise NotBuildTaskError(self.id)

        wait_result = wait(
            'waiting for task to be finished (closed, canceled or failed)',
            self._check_finished_task,
            timeout=wait_timeout
        )

        if not gluetool.utils.normalize_bool_option(module.option('accept-failed-tasks')):
            if wait_result == koji.TASK_STATES['CANCELED']:
                raise SoftGlueError("Task '{}' was canceled".format(self.id))

            if wait_result == koji.TASK_STATES['FAILED']:
                raise SoftGlueError("Task '{}' has failed".format(self.id))

        self._assign_build(build_id)

    def __repr__(self) -> str:
        return '{}({})'.format(self.__class__.__name__, self.id)

    @cached_property
    def _is_valid(self) -> bool:
        """
        Verify the task is valid by checking its ``method`` attribute. List of values that are considered
        `valid` is provided by the user via ``--valid-methods`` option of the module, and generaly limits
        what tasks the pipeline deals with, e.g. it is designed to run tests on Docker images, therefore
        disallows any other method than ``buildContainer``. If there is no specific list of valid methods,
        all methods are considered valid.

        :rtype: bool
        """
        try:
            if not self._module._valid_methods:
                return True

            return self._task_info['method'] in self._module._valid_methods
        except AttributeError:
            raise GlueError("Module '{}' has no attribute _valid_methods".format(self._module))

    def _flush_task_info(self) -> None:
        """
        Remove cached task info we got from API. Handle the case when such info does not yet exist.
        """

        try:
            del self._task_info

        except AttributeError:
            pass

    def _check_finished_task(self) -> Result[str, str]:
        """
        Verify that the task is finished (closed, canceled or failed).

        :returns: True if task is closed, canceled or failed, False otherwise

        The koji documentation has been updated to explain 'state' and 'waiting' and the behaviour of koji.
        https://pagure.io/koji/issue/3267
        https://docs.pagure.org/koji/writing_koji_code/

        """

        self._flush_task_info()

        # If a task is 'CLOSED' and not 'waiting', it's ready to be acted upon.
        if self._task_info['state'] == koji.TASK_STATES['CLOSED'] and not self._task_info['waiting']:
            return Result.Ok(self._task_info['state'])

        # If a task has 'FAILED' or has been 'CANCELED', it should not be acted upon,
        # and there is no need to wait for it to be non-waiting.
        if self._task_info['state'] in [koji.TASK_STATES['CANCELED'], koji.TASK_STATES['FAILED']]:
            if self._task_info['waiting']:
                self.warn("task {} has finished('{}') but is still waiting.".format(self.id, self._task_info['state']))
            return Result.Ok(self._task_info['state'])

        return Result.Error('task is not closed')

    @cached_property
    def _subtasks(self) -> List[TaskInfoType]:
        """
        A list of children tasks in raw form, as JSON data returned by Koji API.

        :rtype: list(dict)
        """

        subtasks = cast(List[TaskInfoType], self._call_api('getTaskChildren', self.id, request=True))
        log_dict(self.debug, 'subtasks', subtasks)

        return subtasks

    @cached_property
    def _build_arch_subtasks(self) -> List[TaskInfoType]:
        """
        A list of children task of ``buildArch`` type, as JSON data returned by Koji API.

        :rtype: list(dict)
        """

        subtasks = [task for task in self._subtasks if task['method'] == 'buildArch']

        log_dict(self.debug, 'buildArch subtasks', subtasks)

        for task in subtasks:
            KojiTask.swap_request_info(task, BuildArchTaskRequest, 5)

        return subtasks

    @staticmethod
    def swap_request_info(task_info: TaskInfoType,
                          klass: Union[Type[BuildArchTaskRequest], Type[BuildTaskRequest]],
                          nr_fields: int) -> None:
        """
        Replace ``request`` key of task info - a JSON structure, returned by API - with
        an object with properties, representing the content of ``request`` key.
        """

        request_info = task_info.get('request', None)

        if request_info is None:
            raise GlueError("Task {} has no request field in task info".format(task_info['id']))

        if len(request_info) < nr_fields:
            raise GlueError("Task {} has unexpected number of items in request field".format(task_info['id']))

        task_info['request'] = klass(*[request_info[i] for i in range(0, nr_fields)])  # type: ignore

    @cached_property
    def _task_info(self) -> TaskInfoType:
        """
        Task info as returned by API.

        :rtype: dict
        """

        task_info = self._call_api('getTaskInfo', self.id, request=True)

        if not task_info:
            raise GlueError("Task '{}' not found".format(self.id))

        log_dict(self.debug, 'task info', task_info)

        KojiTask.swap_request_info(task_info, BuildTaskRequest, 3)

        return task_info

    @cached_property
    def _build(self) -> Optional[Dict[str, str]]:
        """
        Build info as returned by API, or ``None`` for scratch builds.

        :rtype: dict
        """

        if self.scratch:
            return None

        builds = self._call_api('listBuilds', taskID=self.id)
        log_dict(self.debug, 'builds for task ID {}'.format(self.id), builds)

        if not builds:
            return None

        return builds[0]

    @cached_property
    def _result(self) -> Dict[str, Any]:
        """
        Task result info as returned by API.

        :rtype: dict
        """

        result = self._call_api('getTaskResult', self.id)

        log_dict(self.debug, 'task result', result)

        return result

    @cached_property
    def _task_request(self) -> Union[BuildTaskRequest, BuildArchTaskRequest]:
        return self._task_info['request']

    @cached_property
    def has_build(self) -> bool:
        """
        Whether there is a build for this task.

        If there is a ``self.build_id``, then we have a build. ``self.build_id`` is extracted from ``self._build``,
        therefore we can inject ``self._build`` - like Brew's ``buildContainer`` tasks do - and this will work
        like a charm.
        """

        return self.build_id is not None

    @cached_property
    def is_build_task(self) -> bool:
        """
        Whether this task is a "build" task, i.e. building common RPMs.
        """

        return self._task_info['method'] == 'build'

    @cached_property
    def build_id(self) -> Optional[int]:
        """
        Build ID for standard tasks, or ``None`` for scratch builds.

        :rtype: int
        """

        if not self._build:
            return None

        return cast(int, self._build['build_id'])

    @cached_property
    def owner(self) -> str:
        """
        Name of the owner of the task.

        :rtype: str
        """

        owner_id = self._task_info["owner"]
        return cast(str, self._call_api('getUser', owner_id)["name"])

    @cached_property
    def issuer(self) -> str:
        """
        Name of the issuer of the task. The same as :py:attr:`owner`.

        :rtype: str
        """

        return self.owner

    @cached_property
    def target(self) -> str:
        """
        Build target name

        :rtype: str
        """

        if cast(BuildTaskRequest, self._task_request).target:
            return cast(BuildTaskRequest, self._task_request).target

        # inform admins about this weird build
        self.warn("task '{}' build '{}' has no build target".format(self.id, self.nvr), sentry=True)

        return '<no build target available>'

    def previous_tags(self, tags: List[str]) -> List[str]:
        """
        Return previous tags according to the inheritance tag hierarchy to the given tags.

        :param str tags: Tags used for checking.
        :rtype: list(str)
        :returns: List of previous tags, empty list if not previous tags found.
        :raises gluetool.glue.GlueError: In case previous tag search cannot be performed.
        """

        previous_tags = []

        for tag in tags:
            if tag == '<no build target available>':
                raise GlueError('Cannot check for previous tag as build target does not exist')

            try:
                previous_tags.append(self._call_api('getFullInheritance', tag)[0]['name'])
            except (KeyError, IndexError, koji.GenericError):
                self.warn("Failed to find inheritance tree for tag '{}'".format(tag), sentry=True)

        return previous_tags

    @cached_property
    def source(self) -> str:
        """
        Task's source, e.g. git+https://src.fedoraproject.org/rpms/rust-tokio-proto.git?#b59219

        By default try to get from build's info. Fallback to taskinfo's request[0] field.

        :rtype: str
        """

        if self.has_build and self._build and self._build.get('source', None):
            return self._build['source']

        if self._task_request.source:
            return self._task_request.source

        raise GlueError("task '{}' has no source defined in the request field".format(self.id))

    @cached_property
    def scratch(self) -> bool:
        """
        Whether the task is a scratch build.

        :rtype: bool
        """

        return cast(bool, self._task_request.options.get('scratch', False))

    @cached_property
    def task_arches(self) -> TaskArches:
        """
        Return information about arches the task was building for.

        :rtype: TaskArches
        """

        arches = self._task_request.options.get('arch_override', None)

        if arches is not None:
            return TaskArches(False, [arch.strip() for arch in arches.split(' ')])

        children = self._build_arch_subtasks
        child_arches = []

        # Workarond for TFT-2460
        # If there is a noarch build subtask, or noarch label, we can assume that the task is noarch
        # even if task arch is not noarch
        for child in children:

            child_request = child.get('request')
            if isinstance(child_request, BuildArchTaskRequest):

                request_arch = child_request.arch

                if child.get('label') == request_arch == 'noarch':
                    child_arches.append('noarch')

            if child_arches:
                return TaskArches(True, child_arches)

        return TaskArches(True, [child['arch'] for child in self._build_arch_subtasks])

    @cached_property
    def url(self) -> str:
        """
        URL of the task info web page.

        :rtype: str
        """

        return "{}/taskinfo?taskID={}".format(self.web_url, self.id)

    def latest_released(self, tags: Optional[List[str]] = None) -> Optional[Union['KojiTask', 'BrewTask']]:
        """
        Returns task of the latest builds tagged with the same destination tag or build target.

        If no builds are found ``None`` is returned.

        In case the build found is the same as this build, the previous build is returned.

        The tags for checking can be overriden with the ``tags`` parameter. First match wins.

        :param list(str) tags: Tags to use for searching.
        :rtype: :py:class:`KojiTask`
        """
        if tags is None:
            assert self.destination_tag is not None
            tags = [self.destination_tag, self.target]

        for tag in tags:
            try:
                builds = self._call_api('listTagged', tag, None, True, latest=2, package=self.component)
            except koji.GenericError as error:
                self.warn(
                    "ignoring error while listing latest builds tagged to '{}': {}".format(tag, error),
                    sentry=True
                )
                continue
            if builds:
                break
        else:
            log_dict(self.debug, "no latest builds found for package '{}' on tags".format(self.component), tags)
            return None

        # for scratch builds the latest released package is the latest tagged
        if self.scratch:
            build: Optional[Dict[str, Any]] = builds[0]

        # for non scratch we return the latest released package
        # in case it is the same, return the previously released package,
        # in case it has no previous package, return the same package
        else:
            if self.nvr != builds[0]['nvr'] or len(builds) == 1:
                build = builds[0]
            else:
                build = builds[1]

        if build is None:
            raise GlueError("Could not find the baseline build.")

        assert build is not None

        if 'task_id' not in build:
            raise GlueError("No 'task_id' found for the build.")

        if build['task_id'] is None:
            raise GlueError('Could not fetch the build task_id.')

        return self._module.task_factory(TaskInitializer(task_id=build['task_id'], build_id=None)) if build else None

    @cached_property
    def latest(self: Union['KojiTask', 'BrewTask']) -> Optional[str]:
        """
        NVR of the latest released package with the same build target, or ``None`` if none found.

        In case the latest package is the same as this task, the previosly released package's NVR is returned.

        :rtype: str
        """

        latest_released = self.latest_released()

        return latest_released.nvr if latest_released else None

    @cached_property
    def _tags_from_map(self) -> List[str]:
        """
        Unfortunately tags used for looking up baseline builds need to be resolved
        from a rules file due to contradicting use cases.

        Nice examples for this are:

        * rhel-8 builds, which have ``destination_tag`` set to rhel-8.x.y-gate, but that
          is incorrrect for the lookup, we need to use the ``build_target``, which
          in this case is the final destination of the builds after gating

        * for some non-rhel products we have to use ``destination_tag`` only, because
          ``build_target`` is not a tag to which builds get tagged
        """

        self._module.require_shared('evaluate_instructions', 'evaluate_rules')

        # use dictionary which can be altered in _tags_callback
        map: Dict[str, Any] = {
            'tags': []
        }

        def _tags_callback(instruction: Any,
                           command: Any,
                           argument: List[str],
                           context: Optional[ContextType]) -> None:
            map['tags'] = []

            for arg in argument:
                map['tags'].append(self._module.shared('evaluate_rules', arg, context=context))

        context = dict_update(self._module.shared('eval_context'), {
            'TASK': self
        })

        commands = {
            'tags': _tags_callback,
        }

        self._module.shared(
            'evaluate_instructions', self._module.baseline_tag_map,
            commands=commands, context=context
        )

        log_dict(self.debug, 'Tags from baseline tag map', map['tags'])

        return cast(List[str], map['tags'])

    @cached_property
    def baseline(self) -> Optional[str]:
        """
        Return baseline task NVR if `baseline-method` specified, otherwise return None.

        :rtype: str
        """
        if not self._module.option('baseline-method'):
            return None

        if self.baseline_task is None:
            return None

        return self.baseline_task.nvr

    @cached_property
    def baseline_task(self) -> Optional[Union['KojiTask', 'BrewTask']]:
        """
        Return baseline task. For documentation of the baseline methods see the module's help.

        :rtype: KojiTask
        :returns: Initialized task for the baseline build or None if not baseline found.
        :raises gluetool.glue.GlueError: if specific build does not exist or no baseline-method specified.
        """
        method = self._module.option('baseline-method')

        if not method:
            raise GlueError("Cannot get baseline because no 'baseline-method' specified")

        if method == 'previous-released-build':
            previous_tags = self.previous_tags(tags=self._tags_from_map)
            if not previous_tags:
                return None

            baseline_task = self.latest_released(tags=previous_tags)

        elif method == 'previous-build':
            baseline_task = self.latest_released(tags=self._tags_from_map)

        elif method == 'specific-build':
            nvr = self._module.option('baseline-nvr')
            task_initializers = self._module._find_task_initializers(nvrs=[nvr])
            if not task_initializers:
                raise GlueError("Specific build with nvr '{}' not found".format(nvr))
            # we know we have just one initializer ...
            baseline_task = self._module.task_factory(task_initializers[0])

        else:
            # this really should not happen ...
            self.warn("Unknown baseline method '{}'".format(method), sentry=True)
            return None

        if not baseline_task:
            return None

        if baseline_task.id == self.id:
            self.debug("Baseline task is the same, ignoring")
            return None

        return baseline_task

    @cached_property
    def branch(self) -> Optional[str]:
        return None

    @cached_property
    def task_artifacts(self) -> Dict[int, List[str]]:
        """
        Artifacts of ``buildArch`` subtasks, in a mapping where subtask IDs are the keys
        and lists of artifact names are the values.

        Usually, this is a mix of logs and RPMs, and gets empty when task's directory
        on the server is removed.

        :rtype: dict(int, list(str))
        """

        artifacts = {}

        for task in self._build_arch_subtasks:
            task_id = task['id']

            task_output = self._call_api('listTaskOutput', task_id)

            log_dict(self.debug, 'task output of subtask {}'.format(task_id), task_output)

            artifacts[task_id] = task_output

        log_dict(self.debug, 'subtask artifacts', artifacts)

        return artifacts

    @cached_property
    def build_artifacts(self) -> Dict[str, List[Dict[str, str]]]:
        """
        Artifacts of the build, in a mapping where architectures are the keys
        and lists of artifact names are the values.

        Usualy, the set consists of RPMs only, and makes sense for builds only, since it is
        not possible to get task RPMs this way.

        :rtype: dict(str, list(dict(str, str)))
        """

        if not self.has_build:
            return {}

        build_rpms = self._call_api('listBuildRPMs', self.build_id)

        log_dict(self.debug, 'build RPMs', build_rpms)

        artifacts = collections.defaultdict(list)

        for rpm in build_rpms:
            artifacts[rpm['arch']].append(rpm)

        log_dict(self.debug, 'build rpms', artifacts)

        return artifacts

    @cached_property
    def build_archives(self) -> List[Dict[str, Any]]:
        """
        A list of archives of the build.

        :rtype: list(dict)
        """

        if not self.has_build:
            return []

        archives = self._call_api('listArchives', buildID=self.build_id)
        log_dict(self.debug, 'build archives', archives)

        return archives

    @cached_property
    def has_artifacts(self) -> bool:
        """
        Whether there are any artifacts on for the task.

        :rtype: bool
        """

        has_task_artifacts = [bool(subtask_artifacts) for subtask_artifacts in six.itervalues(self.task_artifacts)]
        has_build_artifacts = [bool(arch_artifacts) for arch_artifacts in six.itervalues(self.build_artifacts)]

        return bool(has_task_artifacts and all(has_task_artifacts)) \
            or bool(has_build_artifacts and all(has_build_artifacts))

    @cached_property
    def _srcrpm_subtask(self) -> Tuple[Optional[int], Optional[str]]:
        """
        Search for SRPM-like artifact in ``buildArch`` subtasks, and if there is such artifact,
        provide its name and ID of its subtask. If no such artifact exists, both values are ``None``.

        :rtype: tuple(int, str)
        """

        if not self.has_artifacts:
            self.debug('task has no artifacts, it is pointless to search them for srpm')
            return None, None

        for subtask, artifacts in six.iteritems(self.task_artifacts):
            for artifact in artifacts:
                if not artifact.endswith('.src.rpm'):
                    continue

                return subtask, artifact

        return None, None

    @cached_property
    def srpm_names(self) -> List[str]:
        """
        List of source RPM name or empty list if it's impossible to find it.

        :rtype: list(str)
        """

        task_not_closed = self._task_info['state'] != koji.TASK_STATES["CLOSED"]
        if task_not_closed and not gluetool.utils.normalize_bool_option(self._module.option('accept-failed-tasks')):
            raise GlueError('Task {} is not a successfully completed task'.format(self.id))

        # "build container" tasks have no SRPM
        if not self.is_build_task:
            return []

        # For standard (non-scratch) builds, we may fetch an associated build and dig info from it
        if self.has_build:
            assert self._build
            self.debug('srpm name deduced from build')
            return ['{}.src.rpm'.format(self._build['nvr'])]

        # Search all known artifacts for SRPM-like files
        _, srcrpm = self._srcrpm_subtask

        if srcrpm is not None:
            self.debug('srpm name deduced from a subtask artifact')
            return [srcrpm]

        # Maybe it's in Source option!
        source = self._task_request.options.get('Source', None)
        if source:
            self.debug('srpm name deduced from task Source option')
            return [source.split('/')[-1].strip()]

        # Or in one of the subtasks!
        for subtask in self._build_arch_subtasks:
            if not subtask['request'].source:
                continue

            self.debug('srpm name deduced from subtask Source option')
            return [
                subtask['request'].source.split('/')[-1].strip()
            ]

        # Nope, no SRPM anywhere.
        return []

    @cached_property
    def distgit_ref(self) -> Optional[str]:
        """
        Distgit ref id from which package has been built or ``None`` if it's impossible to find it.

        :rtype: str
        """
        try:
            # In case the detected source is a branch, make sure we remove the remote `origin/`.
            # It causes issues when being used during cloning.
            return six.ensure_str(
                self._task_request.source.split('#')[1].encode('ascii')
            ).removeprefix('origin/')
        except IndexError:
            self.debug('Distgit ref not found')
        return None

    @cached_property
    def _rpm_urls_from_subtasks(self) -> List[str]:
        """
        Resolves RPM urls from subtasks' results. This is the only
        option for scratch rpm builds.
        """
        rpms: List[Dict[str, Any]] = []

        for task in self._build_arch_subtasks:
            try:
                rpms.extend(self._call_api('getTaskResult', task['id'])['rpms'])
            except AttributeError:
                self.warn("No rpms found for task '{}'".format(task['id']))

        return ['/'.join([self.pkgs_url, 'work', str(rpm)]) for rpm in rpms]

    @cached_property
    def _rpm_urls_from_build(self) -> List[str]:
        """
        Resolves RPM urls from build rpms.
        """
        assert self._build is not None
        return [
            "{0}/packages/{1}/{2}/{3}/{4}/{5}.{4}.rpm".format(
                self.pkgs_url,
                self._build['package_name'],
                self._build['version'],
                self._build['release'],
                rpm['arch'],
                rpm['nvr']
            )
            for rpm in self._call_api('listBuildRPMs', self.build_id) if rpm['arch'] != 'src'
        ]

    @cached_property
    def rpm_urls(self) -> List[str]:
        """
        List of URLs of all RPMs in the build.
        """
        if not self.is_build_task:
            return []

        # If build_id is around, use listRPMs to get all the builds
        if self.build_id:
            return self._rpm_urls_from_build

        # For scratch build tasks, our only option is to resolve RPMs from task.
        # If the task is expired (i.e. has no artifacts), the links will be 404.
        return self._rpm_urls_from_subtasks

    @cached_property
    def srpm_urls(self) -> List[str]:
        """
        List of URL of the SRPM (:py:attr:`srcrpm`) or empty list if SRPM is not known.
        """

        if not self.srpm_names:
            return []

        if not self.scratch:
            assert self._build is not None
            return ["{}/packages/{}/{}/{}/src/{}.src.rpm".format(
                self.pkgs_url,
                self._build['package_name'],
                self._build['version'],
                self._build['release'],
                self._build['nvr']
            )]

        srcrpm_task, srcrpm = self._srcrpm_subtask

        # we have SRPM name but no parent task, i.e. it's not possible to construct URL
        if srcrpm_task is None:
            return []

        assert srcrpm is not None
        base_path = koji.pathinfo.taskrelpath(srcrpm_task)

        return ['/'.join(['{0}/work'.format(self.pkgs_url), base_path, srcrpm])]

    @cached_property
    def _split_srcrpm(self) -> Tuple[str, str, str, str, str]:
        """
        SRPM name split into its NVREA pieces.

        :raises gluetool.glue.GlueError: when SRPM name is not known.
        :rtype: tuple(str)
        """

        if not self.srpm_names:
            raise GlueError('Cannot find SRPM name')

        return cast(Tuple[str, str, str, str, str], splitFilename(self.srpm_names[0]))

    @cached_property
    def nvr(self) -> str:
        """
        NVR of the built package.

        :rtype: str
        """

        if self.is_build_task:
            name, version, release, _, _ = self._split_srcrpm

            return six.ensure_str('-'.join([name, version, release]))

        raise GlueError('Cannot deduce NVR for task {}'.format(self.id))

    @cached_property
    def component(self) -> str:
        """
        Package name of the built package (``N`` of ``NVR``).

        :rtype: str
        """

        if self.is_build_task:
            return self._split_srcrpm[0]

        raise GlueError('Cannot find component info for task {}'.format(self.id))

    @cached_property
    def dist_git_repository_name(self) -> str:
        """
        Extract dist-git repository name from the source field. This can be different from the package name.

        If repository name cannot be extracted from source (e.g. build built from src.rpm, not git) `component`
        property is returned.

        :rtype: str
        """

        # Examples of possible sources:
        #   git://pkgs.fedoraproject.org/rpms/bash?#d430777020da4c1e68807f59b0ffd38324adbdb7
        #   git://pkgs/rpms/mead-cron-scripts#dcdc64da7180ae49361756a373c8a5de3a59e732
        #   git+https://src.fedoraproject.org/rpms/bash.git#1f2779c9385142e93c875274eba0621e29a49146
        match = re.match(r'.*/([^#\?]*)\??#.*', self.source)
        if match is not None:
            return cast(str, match.group(1))

        self.debug('Could not extract component name from source field.')
        return self.component

    @cached_property
    def version(self) -> str:
        """
        Version of the built package (``V`` of ``NVR``).

        :rtype: str
        """

        if self.is_build_task:
            return self._split_srcrpm[1]

        raise GlueError('Cannot find version info for task {}'.format(self.id))

    @cached_property
    def release(self) -> str:
        """
        Release of the built package (``R`` of ``NVR``).

        :rtype: str
        """

        if self.is_build_task:
            return self._split_srcrpm[2]

        raise GlueError('Cannot find release info for task {}'.format(self.id))

    @cached_property
    def full_name(self) -> str:
        """
        String with human readable task details. Used for slightly verbose representation e.g. in logs.

        :rtype: str
        """

        name = [
            "task '{}'".format(self.id),
            "build '{}'".format(self.nvr),
            "target '{}'".format(self.target)
        ]

        if self.scratch:
            name.append('(scratch)')

        if not self.has_artifacts:
            name.append('(no artifacts)')

        return ' '.join(name)

    @cached_property
    def short_name(self) -> str:
        """
        Short version of :py:attr:`full_name``.

        :rtype: str
        """

        return "{t.id}:{scratch}{t.nvr}".format(t=self, scratch='S:' if self.scratch else '')

    @cached_property
    def destination_tag(self) -> Optional[str]:
        """
        Build destination tag
        """

        try:
            return cast(str, self._call_api('getBuildTarget', self.target)["dest_tag_name"])
        except TypeError:
            return None

    @cached_property
    def component_id(self) -> str:
        """
        Used by task dispatcher to search their configurations. Identifies the component the task belongs to.

        :rtype: str
        """

        return self.component

    def compare_nvr(self, nvr: Optional[str]) -> int:
        """
        Do an NVR comparison with given nvr.

        :rtype: int
        :returns: 0 if NVRs are same, 1 if artifact has higher version, -1 if artifact has lower version
        """

        if not nvr:
            return 1

        match = re.match(r'(.*)-(.*)-(.*)', nvr)
        if not match:
            raise GlueError("nvr '{}' seems to be invalid".format(nvr))
        (name, version, release) = match.groups()

        if self.component != name:
            raise GlueError("Compared nvrs belong to different components {} {}".format(self.component, nvr))

        # Since `labelCompare` compares EVR (epoch, version, release) and we have only VR
        # we have to add `0` as dummy epoch to both sides
        return cast(int, labelCompare(('0', self.version, self.release), ('0', version, release)))

    @cached_property
    def is_newer_than_latest(self) -> bool:
        return self.compare_nvr(self.latest) > 0


class BrewTask(KojiTask):
    """
    Provides abstraction of a brew build task, specified by task ID. For initialization
    brew instance details need to be passed via the instance dictionary with the following keys:

        ``automation_user_ids`` - list of user IDs that trigger resolving of user from dist git
        ``session`` - a koji session initialized via the koji.ClientSession function
        ``url`` - a base URL for the koji instance
        ``pkgs_url`` - a base URL for the packages location

    This class extends :py:class:`KojiTask` with Brew only features.

    :param dict instance: Instance details, see ``required_instance_keys``
    :param int task_id: Initialize from given TaskID
    :param module: Module that created this task instance.
    :param gluetool.log.ContextLogger logger: logger used for logging
    :param bool wait_timeout: Wait for task to become non-waiting
    """

    ARTIFACT_NAMESPACE = 'brew-build'

    @staticmethod
    def _check_required_instance_keys(details: InitDetailsType) -> None:
        """
        Checks for required instance details for Brew.
        :raises: GlueError if instance is missing some of the required keys
        """
        required_instance_keys = ('automation_user_ids', 'session', 'url', 'pkgs_url')

        if not all(key in details for key in required_instance_keys):
            raise GlueError('instance details do not contain all required keys')

    def __init__(
            self,
            details: InitDetailsType,
            task_id: int,
            module: 'Brew',
            logger: Optional[ContextAdapter] = None,
            wait_timeout: Optional[int] = None,
            build_id: Optional[int] = None,
            artifact_namespace: Optional[str] = None
    ) -> None:

        super(BrewTask, self).__init__(details, task_id, module,
                                       logger=logger,
                                       wait_timeout=wait_timeout,
                                       build_id=build_id,
                                       artifact_namespace=artifact_namespace)

        self.automation_user_ids = details['automation_user_ids']

        if self.is_build_container_task:
            # Try to assign build for container task only when there was no build ID specified.
            # If `build_id` is set, we already have a build, specified explicitly by the caller.

            if build_id is None:
                if not self._result:
                    raise GlueError('Container task {} does not have a result'.format(self.id))

                if 'koji_builds' not in self._result or not self._result['koji_builds']:
                    self.warn('Container task {} does not have a build assigned'.format(self.id))

                else:
                    self._assign_build(int(self._result['koji_builds'][0]))

            # Container builds need specific dispatch ID - given how broken is the integration
            # between Brew and image building service, build ID nor task ID are not enough.
            if self.build_id:
                self.dispatch_id = '{}:{}'.format(str(self.build_id), str(self.id))

    @cached_property
    def is_build_container_task(self) -> bool:
        return bool(self._task_info['method'] == 'buildContainer')

    @cached_property
    def has_artifacts(self) -> bool:
        """
        Whether there are any artifacts on for the task.

        :rtype: bool
        """

        if self.is_build_container_task:
            return bool(self.build_archives) or bool(self.image_repositories)

        return bool(super(BrewTask, self).has_artifacts)

    @cached_property
    def source_members(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Return :py:attr:`source` attribute split into its pieces, a component and a GIT commit hash.

        :rtype: tuple(str, str)
        """

        # It might be worth moving this into a config file, it's way too dependant on Brew internals

        def _split(namespace: str) -> Union[Tuple[None, None], Tuple[str, str]]:
            match_git_hash = re.search("#[^']*", self.source)
            match_component = re.search("/{}/([^#?]*)".format(namespace), self.source)

            if match_git_hash is None or match_component is None:
                return None, None
            return match_component.group(1), match_git_hash.group()[1:]

        self.debug("source '{}'".format(self.source))

        component, git_hash = None, None

        # docker containers are usualy under "containers" namespace
        if self.is_build_container_task:
            component, git_hash = _split('containers')

        log_dict(self.debug, 'source members after "containers" namespace split attempt', [
            component, git_hash
        ])

        # but, some containers still reside under "rpms", like the common components
        if component is None and git_hash is None:
            component, git_hash = _split('rpms')

        log_dict(self.debug, 'source members after "rpms" namespace split attempt', [
            component, git_hash
        ])

        return component, git_hash

    @cached_property
    def _parsed_commit_html(self) -> Optional[Result[Union[BeautifulSoup, bool], str]]:
        """
        :returns: BeatifulSoup4 parsed html from cgit for given component and commit hash
        """

        component, git_hash = self.source_members

        if not component or not git_hash:
            return None

        context = dict_update(self._module.shared('eval_context'), {
                'SOURCE_COMPONENT': component,
                'SOURCE_COMMIT': git_hash
            })

        overall_urls: List[str] = []

        # Callback for 'url' command
        def _url_callback(instruction: Any, command: Any, argument: List[str], context: ContextType) -> None:
            overall_urls[:] = [
                render_template(arg, **context) for arg in argument
            ]

            self.debug("final dist git url set to '{}'".format(overall_urls))

        commands = {
            'url': _url_callback,
        }

        self._module.shared(
            'evaluate_instructions', cast(Brew, self._module).repo_url_map,
            commands=commands, context=context
        )

        # The dt=2 parameter removes diffs from the commit url, making it lighter
        overall_urls = [url + '&dt=2' for url in overall_urls]

        # get git commit html
        for url in overall_urls:
            # Using `wait` for retries would be much easier if we wouldn't be interested
            # in checking another URL - that splits errors into two sets, with different
            # solutions: the first one are "accepted" errors (e.g. URL is wrong), and we
            # want to move on and try another URL, the second set is not so good, and we
            # have to retry once again for the same URL, hoping for better results.

            # We can safely ignore "Cell variable url defined in loop" warning - yes, `url`
            # is updated by the loop and `_fetch` is called with the most actual value of
            # `url`, but that is correct.
            def _fetch() -> Result[Union[BeautifulSoup, bool], str]:
                try:
                    with gluetool.utils.requests(logger=self.logger) as req:
                        res = req.get(url, timeout=self._module.option('commit-fetch-timeout'))

                    if res.ok:
                        return Result.Ok(BeautifulSoup(res.content, 'html.parser'))

                    # Special case - no such URL, we should stop dealing with this one and try another.
                    # Tell `wait` control code to quit.
                    if res.status_code == 404:
                        return Result.Ok(True)

                    # Ignore (possibly transient) HTTP errors 5xx - server knows it encountered an error
                    # or is incapable of finishing the request now. Try again.
                    if 500 <= res.status_code <= 599:
                        return Result.Error('transient HTTP error')

                    # Other not-ok-ish codes should be reported, they probably are not going do disappear
                    # on their own and signal something is really wrong.
                    res.raise_for_status()

                except (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.RequestException):

                    # warn as we needed to retry
                    self.warn("Failed to fetch commit info from '{}' (retrying)".format(url))

                    return Result.Error('connection error')

                return Result.Error('unknown error')

            ret = cast(Union[Result[Union[BeautifulSoup, bool], str], bool], wait(
                'fetching commit web page {}'.format(url), _fetch,
                logger=self.logger,
                timeout=self._module.option('commit-fetch-timeout'),
                tick=self._module.option('commit-fetch-tick')
            ))

            # If our `_fetch` returned `True`, it means it failed to fetch the commit
            # page *in the expected* manner - e.g. the page does not exist. Issues like
            # flapping network would result in another spin of waiting loop.
            if ret is True:
                continue

            return cast(Result[Union[BeautifulSoup, bool], str], ret)

        return None

    @cached_property
    def nvr(self) -> str:
        """
        NVR of the built package.

        :rtype: str
        """

        if self.is_build_container_task:
            if self.has_build:
                assert self._build is not None
                return self._build['nvr']

            return six.ensure_str('-'.join([self.component, self.version, self.release]))

        return super(BrewTask, self).nvr

    @cached_property
    def component(self) -> str:
        """
        Package name of the built package (``N`` of ``NVR``).

        :rtype: str
        """

        if self.is_build_container_task:
            if self.has_build:
                assert self._build is not None
                return self._build['package_name']

            component, _ = self.source_members

            if component:
                # Source repository is named 'foo-bar', but the component name - as known to Brew and Bugzilla - is
                # actually foo-bar-container. Add the suffix.
                # This is not necessary when there's a build (which means non-scratch tasks), in that case we're
                # using build's package_name as the source, and that's correct already.

                # This is another good candidate for a mapping file - insert task, let configuration
                # yield the component.
                return '{}-container'.format(component)

        return super(BrewTask, self).component

    @cached_property
    def version(self) -> str:
        """
        Version of the built package (``V`` of ``NVR``).

        :rtype: str
        """

        if self.is_build_container_task:
            # if there's a build, the versions should be there
            if self.has_build:
                assert self._build is not None
                return self._build['version']

            # It's not there? Ah, we have to inspect manifests, it might be there. So much work :/
            # It should be the same in all repositories - it's the same image, with the same metadata.
            # Just check all manifests we have.
            for i, repository in enumerate(self.image_repositories):
                for j, entry in enumerate(repository.manifest.get('history', [])):
                    data = gluetool.utils.from_json(entry.get('v1Compatibility', '{}'))
                    log_dict(self.debug, 'repository #{}, history entry #{}'.format(i, j), data)

                    version = data.get('config', {}).get('Labels', {}).get('version', None)
                    self.debug("version extracted: '{}'".format(version))

                    if version:
                        return cast(str, version)

            # Nope, no idea where else to look for release...
            return 'UNKNOWN-VERSION'

        return super(BrewTask, self).version

    @cached_property
    def release(self) -> str:
        """
        Release of the built package (``R`` of ``NVR``).

        :rtype: str
        """

        if self.is_build_container_task:
            # if there's a build, the release should be there
            if self.has_build and self._build:
                return self._build['release']

            # ok, it might be in task request!
            release = self._task_request.options.get('release', None)

            if release:
                return release

            # It's not there? Ah, we have to inspect manifests, it might be there. So much work :/
            # It should be the same in all repositories - it's the same image, with the same metadata.
            # Just check all manifests we have
            for i, repository in enumerate(self.image_repositories):
                for j, entry in enumerate(repository.manifest.get('history', [])):
                    data = gluetool.utils.from_json(entry.get('v1Compatibility', '{}'))
                    log_dict(self.debug, 'repository #{}, history entry #{}'.format(i, j), data)

                    release = data.get('config', {}).get('Labels', {}).get('release', None)
                    self.debug("release extracted: '{}'".format(release))

                    if release:
                        return release

            # Nope, no idea where else to look for release...
            return 'UNKNOWN-RELEASE'

        return super(BrewTask, self).release

    @cached_property
    def branch(self) -> Optional[str]:
        """
        :returns: git branches of brew task or None if branch could not be found
        """

        # Docker image builds provide this in task' options. If it's not there, just fall back to the old way.
        if self.is_build_container_task:
            git_branch = self._task_request.options.get('git_branch', None)

            if git_branch:
                return git_branch

        if self._parsed_commit_html is None:
            return None

        try:
            branches = [branch.string for branch in self._parsed_commit_html.find_all(class_='branch-deco')]  # type: ignore  # noqa
            return six.ensure_str(' '.join(branches))
        except AttributeError:
            raise GlueError("could not find 'branch-deco' class in html output of cgit, please inspect")

    @cached_property
    def issuer(self) -> str:
        """
        :returns: issuer of brew task and in case of build from automation, returns issuer of git commit
        """
        owner_id = self._task_info["owner"]
        if owner_id not in self.automation_user_ids:
            return self.owner

        if self.source.endswith('.src.rpm'):
            self.info('Build was built from src.rpm, skipping detection from dist-git as commit is unknown')
            return self.owner

        self.info("Automation user detected, need to get git commit issuer")

        if self._parsed_commit_html is None:
            self.warn('could not find git commit issuer', sentry=True)
            return self.owner

        assert isinstance(self._parsed_commit_html, BeautifulSoup)
        issuer = self._parsed_commit_html.find(class_='commit-info').find('td')
        issuer = re.sub(".*lt;(.*)@.*", "\\1", str(issuer))

        return cast(str, issuer)

    @cached_property
    def rhel(self) -> str:
        """
        :returns: major version of RHEL
        """
        return re.sub(".*rhel-(\\d+).*", "\\1", self.target)

    @cached_property
    def task_arches(self) -> TaskArches:
        """
        Return information about arches the task was building for.

        :rtype: TaskArches
        """

        if self.is_build_container_task:
            arches = []

            if self.has_build:
                for archive in self.build_archives:
                    if archive['btype'] != 'image':
                        continue

                    arches.append(archive['extra']['image']['arch'])

            else:
                # This is workaround for Brew deficiency: the image architecture is *not* mentioned anywhere
                # in Brew API responses. For regular builds, it's in build info, for scratch builds - nowhere :/
                # Only relevant source is the actual image itself...
                arches = [
                    repository.arch for repository in self.image_repositories
                ]

            return TaskArches(False, arches)

        return cast(TaskArches, super(BrewTask, self).task_arches)

    @cached_property
    def build_archives(self) -> List[Dict[str, Any]]:
        """
        A list of archives of the build.

        Overriding parent method to enhance image archives with image URL.

        :rtype: list(dict)
        """

        archives: List[Dict[str, Any]] = super(BrewTask, self).build_archives

        if self.is_build_container_task:
            context = dict_update(self._module.shared('eval_context'), {
                'MODULE': self._module,
                'TASK': self
            })

            for archive in archives:
                if archive.get('btype', None) != 'image':
                    continue

                archive['image_url'] = render_template(self._module.option('docker-image-url-template'),
                                                       logger=self.logger,
                                                       ARCHIVE=archive, **context)

        return archives

    @cached_property
    def build_archives_sha(self) -> Dict[str, str]:
        """
        A map of build arches and build archive SHAs
        """
        build_archives_sha = {}
        for archive in self.build_archives:
            arch = archive.get('extra', {}).get('image', {}).get('arch', {})
            if not arch:
                continue
            if 'docker-image-sha256' not in archive['filename']:
                continue
            re_sha = re.match(r'docker-image-sha256:([a-f0-9]+)\.', archive['filename'])
            if not re_sha:
                continue
            build_archives_sha[arch] = re_sha.group(1)

        self.debug('Build arches and their SHAs: {}'.format(build_archives_sha))

        return build_archives_sha

    @cached_property
    def image_repositories(self) -> List[ImageRepository]:
        """
        A list of Docker image repositories build by the task.

        :rtype: list(dict)
        """

        if not self._result:
            return []

        if 'repositories' not in self._result:
            return []

        log_dict(self.debug, 'raw image repositories', self._result['repositories'])

        # Task provides usually more than one repository, and often some of them lead to the same image.
        # We want to provide our users list of unique repositories (images). To do that, we have to check
        # each repository, find out what is the ID of the image, and group them by their corresponding images.
        # By checking the image manifest, we get access to image architecture as well - this is important,
        # there is no other place to get this info from for scratch container builds, it's not in Brew task
        # info nor result.

        images: Dict[Tuple[str, ...], Any] = {}

        for repository_url in self._result['repositories']:
            # split repository URL into parts
            match = re.match('(.*?)/(.*?):(.*)$', repository_url)
            if match is None:
                self.warn("Cannot decypher repository URL '{}'".format(repository_url), sentry=True)
                continue

            netloc, image_name, reference = match.groups()

            manifest_url = 'http://{}/v2/{}/manifests/{}'.format(netloc, image_name, reference)
            self.debug("manifest URL: '{}'".format(manifest_url))

            # manifest = requests.get(manifest_url).json()
            _, content = gluetool.utils.fetch_url(manifest_url, logger=self.logger)
            manifest = gluetool.utils.from_json(content)

            log_dict(self.debug, '{} manifest'.format(repository_url), manifest)

            # With v2 manifests, we'd just look up image ID. With v1, there's no such field, but different URLs,
            # leading to the same image, should have same FS layers.
            image_id = tuple([
                layer['blobSum'] for layer in manifest['fsLayers']
            ])

            image_arch = manifest['architecture']

            # translate arch from dockerish to our world
            if image_arch == 'amd64':
                image_arch = 'x86_64'

            if image_id in images:
                # We've already seen this image
                image = images[image_id]

            else:
                # First time seeing this image
                image = images[image_id] = {
                    'arch': image_arch,
                    # there can be multiple "repositories", URLs leading to this image
                    'repositories': [],
                    # they should provide the same manifest though - no list then, just store the first one
                    'manifest': manifest
                }

            if image['arch'] != image_arch:
                # This should not happen. URLs leading to the same image should have the same architecture.
                # If it happens, must investigate.
                raise GlueError('Mismatching repository architectures')

            image['repositories'].append(repository_url)

        # Now, we must find the most specific URL for each image - under `repositories` key, there's a list
        # of URLs leading to the same image. Pretty naive but quite successfull method could be finding the
        # longest one - whatever the image name might be, the longest URL should have a timestamp-like value
        # at the end, which would make it longer than any other.

        # And we're still returning "repositories", not "images" - above, we've been gathering images, to deal
        # with different URLs leading to the same image, but we want to return them as repositories, as these
        # are the task artifacts.
        repositories = [
            ImageRepository(image['arch'], max(image['repositories'], key=len), image['repositories'], image['manifest'])  # noqa
            for image in six.itervalues(images)  # noqa
        ]

        log_dict(self.debug, 'image repositories', repositories)

        return repositories

    @cached_property
    def extra(self) -> Dict[str, Any]:
        """
        :returns: extra field from build, empty dictionary otherwise
        """
        assert self._build is not None
        return cast(Dict[str, Any], self._build.get('extra', {}))

    @cached_property
    def image_full_names(self) -> List[str]:
        """
        :returns: list of image full names extracted from the build extra field, empty list otherwise
        """
        if not self.extra:
            self.warn('No extra field found, returning empty list for image full names')
            return []

        images: List[str] = self.extra.get('image', {}).get('index', {}).get('pull', [])

        if not images:
            self.warn('Could not extract image full names from extra field, returning empty list')

        return images

    @cached_property
    def image_id(self) -> Optional[str]:
        """
        :returns: image id represented as sha256 digest,
                  i.e. sha256:67dad89757a55bfdfabec8abd0e22f8c7c12a1856514726470228063ed86593b
        """
        if not self.extra:
            self.warn('No extra field found in build metadata, returning None as image ID', sentry=True)
            return None

        digests = self.extra.get('image', {}).get('index', {}).get('digests', {})

        if not digests:
            log_dict(
                self.warn,
                'Could not extract image ID from extra fields image digests, returning None',
                self.extra
            )
            return None

        # Warn if things are unexpected, rather then fail.
        # This extra fields API is not something set to stone.
        # It might change unexpectedly, for example the docker manifest might get v3 sometime.
        # Rather not make the code fail here, as it would blow up the testing.
        # Rather scream about it, inform sentry, but expect the right side of the first element is the one.
        if len(digests.keys()) > 1 or 'application/vnd.docker.distribution.manifest.list.v2+json' not in digests.keys():
            log_dict(self.warn, 'image.index.digest in extra field has unexpected keys', self.extra)

        return cast(str, list(digests.values())[0])


class Koji(gluetool.Module):
    """
    Provide various information related to a task from Koji build system.

    The task can be specified using on the command line with
        - option ``--build-id`` with a build ID
        - options ``--name`` and ``--tag`` with the latest build from the given tag
        - option ``--nvr`` with a string with an NVR of a build
        - option ``--task-id`` with a build task ID

    The task can be specified also by using the ``task`` shared function. The shared function
    supports only initialization from task ID.

    If option ``--baseline-method`` is specified, the module finds a baseline build according
    to given method and exposes it under ``baseline_task`` attribute of the primary task. The following
    baseline methods are supported:

    * ``previous-build`` - finds the previously built package on the same tag
    * ``previous-released-build`` - finds the previously released build, i.e. build tagged to the previous
                                  tag according to the tag inheritance
    * ``specific-build`` - finds the build specified with ``--baseline-nvr`` option

    For the baseline methods it is expected to provide a rules file via the ``--baseline-tag-map`` option
    which provides a list of tags which will be used to lookup. Each rule needs to provide `tags` attribute
    with list of possible values. Each list item is interpreted as a rule. All rules are evaluated and the
    last matching wins. Below is an example we use now:

    .. code-block:: yaml

        - tags:
            - TASK.destination_tag
            - TASK.target

        - rule: MATCH('.*-gate$', TASK.destination_tag)
          tags:
            - SUB(r'([^-]*)-([^-]*)-.*', r'\1-\2-candidate', TASK.target)
    """

    name = 'koji'
    description = 'Provide Koji task details to other modules'
    supported_dryrun_level = gluetool.glue.DryRunLevels.DRY

    options = [
        ('General options', {
            'url': {
                'help': 'Koji Hub instance base URL',
            },
            'pkgs-url': {
                'help': 'Koji packages base URL',
            },
            'web-url': {
                'help': 'Koji instance web ui URL',
            },
            'task-id': {
                'help': 'Initialize from task ID (default: none).',
                'action': 'append',
                'default': [],
                'type': int
            },
            'build-id': {
                'help': 'Initialize from build ID (default: none).',
                'action': 'append',
                'default': [],
                'type': int
            },
            'name': {
                'help': """
                        Initialize from package name, by choosing latest tagged build (requires ``--tag``)
                        (default: none).
                        """,
                'action': 'append',
                'default': []
            },
            'nvr': {
                'help': 'Initialize from package NVR (default: none).',
                'action': 'append',
                'default': []
            },
            'tag': {
                'help': 'Use given build tag.',
            },
            'valid-methods': {
                'help': """
                        List of task methods that are considered valid, e.g. ``build`` or ``buildContainer``
                        (default: none, i.e. any method is considered valid).
                        """,
                'metavar': 'METHOD1,METHOD2,...',
                'action': 'append',
                'default': []
            },
            'wait': {
                'help': 'Wait timeout for task to become non-waiting and closed (default: %(default)s)',
                'type': int,
                'default': 60,
            }
        }),
        ('Baseline options', {
            'baseline-method': {
                'help': 'Method for choosing the baseline package.',
                'choices': ['previous-build', 'specific-build', 'previous-released-build'],
                'metavar': 'METHOD',
            },
            'baseline-nvr': {
                'help': "NVR of the build to use with 'specific-build' baseline method",
            },
            'baseline-tag-map': {
                'help': 'Optional rules providing tags which are used for finding baseline package'
            }
        }),
        ('Workarounds', {
            'accept-failed-tasks': {
                'help': """
                        If set, even failed task will be accepted without stopping the pipeline (default: %(default)s).
                        """,
                'metavar': 'yes|no',
                'default': 'no'
            },
            'artifact-namespace': {
                 'help': """
                         If set, forces the ARTIFACT_NAMESPACE property of KojiTask/BrewTask to the given value.
                         """,
            },
            'commit-fetch-timeout': {
                'help': """
                        The maximum time for trying to fetch one (dist-git) URL with commit info
                        (default: %(default)s).
                        """,
                'metavar': 'SECONDS',
                'type': int,
                'default': DEFAULT_COMMIT_FETCH_TIMEOUT
            },
            'commit-fetch-tick': {
                'help': """
                        Delay between attempts to fetch one (dist-git) URL with commit info failed
                        (default: %(default)s).
                        """,
                'metavar': 'SECONDS',
                'type': int,
                'default': DEFAULT_COMMIT_FETCH_TICKS
            }
        }),
        ('Retry_Options', {
            'api-version-retry-timeout': {
                'help': """
                    The number of seconds until a new retry is initiated. (default: %(default)s).
                    """,
                'default': DEFAULT_API_VERSION_RETRY_TIMEOUT
            },
            'api-version-retry-tick': {
                'help': """
                    Number of retries for getting API version (default: %(default)s).
                    """,
                'type': int,
                'default': DEFAULT_API_VERSION_RETRY_TICK
            },
        })
    ]

    options_note = """
    Options ``--task-id``, ``--build-id``, ``--name`` and ``--nvr`` can be used multiple times, and even mixed
    together, to specify tasks for a single pipeline in many different ways.
    """

    required_options = ['url', 'pkgs-url', 'web-url']
    shared_functions = ['tasks', 'primary_task', 'koji_session']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(Koji, self).__init__(*args, **kwargs)

        self._session = None
        self._tasks: List[KojiTask] = []

    @cached_property
    def _valid_methods(self) -> List[str]:
        return gluetool.utils.normalize_multistring_option(self.option('valid-methods'))

    @cached_property
    def baseline_tag_map(self) -> Union[List[Any], Dict[str, Any]]:
        if not self.option('baseline-tag-map'):
            return []

        return cast(Union[List[Any], Dict[str, Any]], gluetool.utils.load_yaml(self.option('baseline-tag-map')))

    def task_factory(self,
                     task_initializer: TaskInitializer,
                     wait_timeout: Optional[int] = None,
                     details: Optional[InitDetailsType] = None,
                     task_class: Optional[Type[KojiTask]] = None) -> KojiTask:
        task_class = task_class or KojiTask

        details = cast(InitDetailsType, dict_update({
            'session': self._session,
            'url': self.option('url'),
            'pkgs_url': self.option('pkgs-url'),
            'web_url': self.option('web-url'),
        }, cast(Optional[Dict[Any, Any]], details) or {}))

        task = task_class(details, task_initializer.task_id, self,
                          logger=self.logger,
                          wait_timeout=wait_timeout if wait_timeout else self.option('wait'),
                          build_id=task_initializer.build_id,
                          artifact_namespace=self.option('artifact-namespace'))

        return task

    def _call_api(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return _call_api(self._session, self.logger, method, *args, **kwargs)

    def _objects_to_builds(
        self,
        name: str,
        object_ids: Optional[Union[List[int], List[str]]],
        finder: Callable[..., List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:

        if not object_ids:
            return []

        log_dict(self.debug, 'finding builds for {} ids'.format(name), object_ids)

        builds: List[Dict[str, Any]] = []

        for object_id in object_ids:
            build = finder(object_id)

            log_dict(self.debug, "for '{}' found".format(object_id), build)

            if None in build:
                self.warn('Looking for {} {}, remote server returned None - skipping this ID'.format(name, object_id))
                continue

            builds += build

        log_dict(self.debug, 'found builds', builds)

        return builds

    def _find_task_initializers(
            self,
            task_initializers: Optional[List[TaskInitializer]] = None,
            task_ids: Optional[List[int]] = None,
            build_ids: Optional[List[int]] = None,
            nvrs: Optional[List[str]] = None,
            names: Optional[List[str]] = None
    ) -> List[TaskInitializer]:
        """
        Tries to gather all available task IDs for different given inputs - build IDs, NVRs, package names
        and actual task IDs as well. Some of these may be unknown to the backend, some of them may not lead
        to a task ID. This helper method will find as many task IDs as possible.

        :param list(TaskInitializer) task_initializers: if set, it is a list of already found tasks. New ones
            are added to this list.
        :param list(int) task_ids: Task IDs
        :param list(int) build_ids: Build IDs.
        :param list(str) nvrs: Package NVRs.
        :param list(str) names: Package names. The latest build with a tag - given via module's ``--tag``
            option - is the possible solution.
        :rtype: list(TaskInitializer)
        :return: Gathered task initializers.
        """

        log_dict(self.debug, '[find task initializers] task initializers', task_initializers)
        log_dict(self.debug, '[find task initializers] from task IDs', task_ids)
        log_dict(self.debug, '[find task initializers] from build IDs', build_ids)
        log_dict(self.debug, '[find task initializers] from NVRs', nvrs)
        log_dict(self.debug, '[find task initializers] from names', names)

        task_initializers = task_initializers or []

        # Task IDs are easy.
        task_ids = task_ids or []

        task_initializers += [
            TaskInitializer(task_id=task_id, build_id=None) for task_id in task_ids
        ]

        # Other options represent builds, and from those builds we must extract their tasks. First, let's find
        # all those builds.
        builds = []

        builds += self._objects_to_builds(
            'build',
            build_ids,
            lambda build_id: [self._call_api('getBuild', build_id)]
        )
        builds += self._objects_to_builds(
            'nvr',
            nvrs,
            lambda nvr: [self._call_api('getBuild', nvr)]
        )
        builds += self._objects_to_builds(
            'name',
            names,
            lambda name: cast(
                List[Dict[str, Any]],
                self._call_api(
                    'listTagged',
                    self.option('tag'),
                    package=name,
                    inherit=True,
                    latest=True
                )
            )
        )

        # Now extract task IDs.
        for build in builds:
            if 'task_id' not in build or not build['task_id']:
                log_dict(self.debug, '[find task initializers] build does not provide task ID', build)
                continue

            task_initializers.append(
                TaskInitializer(task_id=int(build['task_id']), build_id=int(build['build_id']))
            )

        log_dict(self.debug, '[find task initializers] found initializers', task_initializers)

        return task_initializers

    def koji_session(self) -> Optional[koji.ClientSession]:
        return self._session

    def _assert_tasks(self) -> None:
        if not self._tasks:
            self.debug('No tasks specified.')

    def tasks(
            self,
            task_initializers: Optional[List[TaskInitializer]] = None,
            task_ids: Optional[List[int]] = None,
            build_ids: Optional[List[int]] = None,
            nvrs: Optional[List[str]] = None,
            names: Optional[List[str]] = None,
            **kwargs: Any
    ) -> List[KojiTask]:
        """
        Returns a list of current tasks. If options are specified, new set of tasks is created using
        the provided options to find all available tasks, and this set becomes new set of current tasks,
        which is then returned.

        Method either returns non-empty list of tasks, or raises an exception

        :param list(TaskInitializer) task_initializers: Task initializers.
        :param list(int) task_ids: Task IDs
        :param list(int) build_ids: Build IDs.
        :param list(str) nvr: Package NVRs.
        :param list(str) names: Package names. The latest build with a tag - given via module's ``--tag``
            option - is the possible solution.
        :param dict kwargs: Additional arguments passed to :py:meth:`task_factory`.
        :rtype: list(KojiTask)
        :returns: Current task instances.
        :raises gluetool.glue.GlueError: When there are no tasks.
        """

        # Re-initialize set of current tasks only when any of the options is set.
        # Otherwise leave it untouched.
        task_initializers = task_initializers or []

        if any([task_initializers, task_ids, build_ids, nvrs, names]):
            task_initializers = self._find_task_initializers(
                task_initializers=task_initializers,
                task_ids=task_ids,
                build_ids=build_ids,
                nvrs=nvrs, names=names
            )

            self._tasks = [
                self.task_factory(task_initializer, **kwargs)
                for task_initializer in task_initializers
            ]

        self._assert_tasks()

        return self._tasks

    def primary_task(self) -> Optional[KojiTask]:
        """
        Returns a `primary` task, the first task in the list of current tasks.

        Method either returns a task, or raises an exception.

        :rtype: :py:class:`KojiTask`
        :raises gluetool.glue.GlueError: When there are no tasks, therefore not even a primary one.
        """

        log_dict(self.debug, 'primary task - current tasks', self._tasks)

        self._assert_tasks()

        return self._tasks[0] if self._tasks else None

    @property
    def eval_context(self) -> Dict[str, Any]:
        __content__ = {  # noqa
            # common for all artifact providers
            'ARTIFACT_TYPE': """
                             Type of the artifact, either ``koji-build`` or ``brew-build``.
                             """,
            'BUILD_TARGET': """
                            Build target of the primary task, as known to Koji/Brew.
                            """,
            'NVR': """
                   NVR of the primary task.
                   """,
            'PRIMARY_TASK': """
                            Primary task, represented as ``KojiTask`` or ``BrewTask`` instance.
                            """,
            'TASKS': """
                     List of all tasks known to this module instance.
                     """,

            # Brew/Koji specific
            'SCRATCH': """
                       ``True`` if the primary task represents a scratch build, ``False`` otherwise.
                       """
        }

        primary_task = self.primary_task()

        if not primary_task:
            self.debug('No primary task available, cannot pass it to eval_context')
            return {}

        return {
            # common for all artifact providers
            'ARTIFACT_TYPE': primary_task.ARTIFACT_NAMESPACE,
            'BUILD_TARGET': primary_task.target,
            'NVR': primary_task.nvr,
            'PRIMARY_TASK': primary_task,
            'TASKS': self.tasks(),

            # Brew/Koji specific
            'SCRATCH': primary_task.scratch
        }

    def sanity(self) -> None:

        # make sure that no conflicting options are specified

        # name option requires tag
        if self.option('name') and not self.option('tag'):
            raise IncompatibleOptionsError("You need to specify 'tag' with package name")

        # name option requires tag
        if self.option('tag') and not self.option('name'):
            raise IncompatibleOptionsError("You need to specify package name with '--name' option")

        method = self.option('baseline-method')
        if method and method == 'specific-build' and not self.option('baseline-nvr'):
            raise IncompatibleOptionsError("You need to specify build NVR with '--baseline-nvr' option")

    def execute(self) -> None:
        url = self.option('url')
        wait_timeout = self.option('wait')

        def _api_version() -> Result[bool, bool]:

            try:
                version = self._call_api('getAPIVersion')
            except (koji.ServerOffline, requests.exceptions.ConnectionError) as error:
                self.warn('Retrying getAPIVersion due to exception: {}'.format(error))
                return Result.Error(False)

            return Result.Ok(version)

        self._session = koji.ClientSession(url)
        version = gluetool.utils.wait(
            "getting api version",
            _api_version,
            timeout=self.option('api-version-retry-timeout'),
            tick=self.option('api-version-retry-tick')
        )
        self.info('connected to {} instance \'{}\' API version {}'.format(self.unique_name, url, version))

        task_initializers = self._find_task_initializers(
            task_ids=self.option('task-id'),
            build_ids=self.option('build-id'),
            nvrs=normalize_multistring_option(self.option('nvr')),
            names=normalize_multistring_option(self.option('name'))
        )

        if task_initializers:
            self.tasks(task_initializers=task_initializers, wait_timeout=wait_timeout)

        for task in self._tasks:
            self.info('Initialized with {}: {} ({})'.format(task.id, task.full_name, task.url))

            # init baseline build if requested
            if self.option('baseline-method'):
                if task.baseline_task:
                    self.info('Baseline build: {} ({})'.format(task.baseline_task.nvr, task.baseline_task.url))
                else:
                    self.warn('Baseline build was not found')


class Brew(Koji, (gluetool.Module)):
    """
    Provide various information related to a task from Brew build system.

    The task can be specified using on the command line with
        - option ``--build-id`` with a build ID
        - options ``--name`` and ``--tag`` with the latest build from the given tag
        - option ``--nvr`` with a string with an NVR of a build
        - option ``--task-id`` with a build task ID

    The task can be specified also by using the ``task`` shared function. The shared function
    supports only initialization from task ID.

    If option ``--baseline-method`` is specified, the module finds a baseline build according
    to given method and exposes it under ``baseline_task`` attribute of the primary task. The following
    baseline methods are supported:

    * ``previous-build`` - finds the previously built package on the same tag
    * ``previous-released-build`` - finds the previously released build, i.e. build tagged to the previous
                                  tag according to the tag inheritance
    * ``specific-build`` - finds the build specified with ``--baseline-nvr`` option

    For the baseline methods it is expected to provide a rules file via the ``--baseline-tag-map`` option
    which provides a list of tags which will be used to lookup. Each rule needs to provide `tags` attribute
    with list of possible values. Each list item is interpreted as a rule. All rules are evaluated and the
    last matching wins. Below is an example we use now:

    .. code-block:: yaml

        - tags:
            - TASK.destination_tag
            - TASK.target

        - rule: MATCH('.*-gate$', TASK.destination_tag)
          tags:
            - SUB(r'([^-]*)-([^-]*)-.*', r'\1-\2-candidate', TASK.target)
    """
    name = 'brew'
    description = 'Provide Brew task details to other modules'

    # Koji.options contain hard defaults
    options = Koji.options + [
        ('Brew options', {
            'automation-user-ids': {
                'help': 'List of comma delimited user IDs that trigger resolving of issuer from dist git commit instead'
            },
            'docker-image-url-template': {
                'help': """
                        Template for constructing URL of a Docker image. It is given a task (``TASK``)
                        and an archive (``ARCHIVE``) describing the image, as returned by the Koji API.
                        """
            },
            'repo-url-map': {
                'help': 'File with URLs of repositories.'
            }
        }),  # yes, the comma is correct - `)` closes inner tuple, `,` says it is part of the outer tuple
    ]

    required_options = Koji.required_options + [
        'automation-user-ids', 'docker-image-url-template'
    ]

    @cached_property
    def repo_url_map(self) -> Any:
        if not self.option('repo-url-map'):
            return []

        return gluetool.utils.load_yaml(self.option('repo-url-map'), logger=self.logger)

    def task_factory(self,
                     task_initializer: TaskInitializer,
                     wait_timeout: Optional[int] = None,
                     details: Optional[InitDetailsType] = None,
                     task_class: Optional[Type[KojiTask]] = None) -> KojiTask:

        # options checker does not handle multiple modules in the same file correctly, therefore it
        # raises "false" negative for the following use of parent's class options
        details = cast(InitDetailsType, dict_update({}, {
            'automation_user_ids': [int(user.strip()) for user in self.option('automation-user-ids').split(',')]
        }, cast(Optional[Dict[Any, Any]], details) or {}))

        return super(Brew, self).task_factory(
            task_initializer,
            details=details,
            task_class=BrewTask,
            wait_timeout=wait_timeout if wait_timeout else self.option('wait')
        )

    def _find_task_initializers(
            self,
            task_initializers: Optional[List[TaskInitializer]] = None,
            task_ids: Optional[List[int]] = None,
            build_ids: Optional[List[int]] = None,
            nvrs: Optional[List[str]] = None,
            names: Optional[List[str]] = None,
    ) -> List[TaskInitializer]:
        """
        Containers integration with Brew is messy.

        Some container builds may not set their ``task_id`` property, instead there's
        an ``extra.container_koji_task_id`` key. This method tries to extract task ID
        from such builds.

        If such build is detected, this method creates a task initializer, preserving
        the build ID. The original ``_find_task_initializers`` is then called to deal
        with the rest of arguments. Given that this method tries to extract data from
        builds, extending list of task initializers, it is interested only in a limited
        set of parameters its original accepts, therefore all remaining keyword arguments
        are passed to the overriden ``_find_task_initializers``.

        :param list(TaskInitializer) task_initializers: if set, it is a list of already found tasks. New ones
            are added to this list.
        :param list(int) build_ids: Build IDs.
        :rtype: list(int)
        :return: Gathered task IDs.
        """

        log_dict(self.debug, '[find task initializers - brew] task initializers', task_initializers)
        log_dict(self.debug, '[find task initializers - brew] from task IDs', task_ids)
        log_dict(self.debug, '[find task initializers - brew] from build IDs', build_ids)
        log_dict(self.debug, '[find task initializers - brew] from NVRs', nvrs)
        log_dict(self.debug, '[find task initializers - brew] from names', names)

        task_initializers = task_initializers or []
        build_ids = build_ids or []

        # Just like the original, fetch builds for given build IDs
        builds = self._objects_to_builds(
            'build',
            build_ids,
            lambda build_id: cast(List[Dict[str, Any]], [self._call_api('getBuild', build_id)])
        )

        # Check each build - if it does have task_id, it passes through. If it does not have task_id,
        # but it does have extras.container_koji_task_id, we create an initializer (with the correct
        # task and build IDs), but we drop the build from list of builds we were given - we don't
        # want it there anymore because it was already converted to a task initializer.
        #
        # If there's no task ID at all, just let the build pass through, our parent will deal with
        # it somehow, we don't have to care.
        cleansed_build_ids = []

        for build_id, build in zip(build_ids, builds):
            if 'task_id' in build and build['task_id']:
                cleansed_build_ids.append(build_id)
                continue

            if 'extra' not in build or 'container_koji_task_id' not in build['extra']:
                cleansed_build_ids.append(build_id)
                continue

            log_dict(self.debug, 'build provides container koji task ID', build)

            task_initializers.append(
                TaskInitializer(task_id=int(build['extra']['container_koji_task_id']), build_id=int(build_id))
            )

        log_dict(self.debug, '[find task initializers - brew] found task initializers', task_initializers)

        return super(Brew, self)._find_task_initializers(
            task_initializers=task_initializers,
            task_ids=task_ids,
            build_ids=cleansed_build_ids,
            nvrs=nvrs,
            names=names,
        )
