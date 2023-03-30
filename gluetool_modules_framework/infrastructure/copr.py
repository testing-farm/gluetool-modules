# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import collections
import re
import requests
import six

import gluetool
from gluetool.utils import cached_property, dict_update, render_template
from gluetool.log import log_dict, log_blob

# Type annotations
from typing import Tuple, cast, Any, Dict, List, Optional, Union  # noqa

#: Information about task architectures.
#:
#: :ivar list(str) arches: List of architectures.
TaskArches = collections.namedtuple('TaskArches', ['arches'])


class CoprApi(object):

    def __init__(self, copr_url: str, module: gluetool.Module) -> None:
        self.copr_url = copr_url
        self.module = module

    def _api_request(self, url: str, label: str, full_url: bool = False) -> requests.Response:
        if not full_url:
            url = '{}/{}'.format(self.copr_url, url)

        self.module.debug('[copr API] {}: {}'.format(label, url))

        try:
            request = requests.get(url)
        except Exception:
            raise gluetool.GlueError('Unable to GET: {}'.format(url))

        if request.status_code != 200:
            self.module.warn('Request to copr API ended up with status code {}'.format(request.status_code))

        return request

    def _get_text(self, url: str, label: str, full_url: bool = False) -> str:
        # Using `.content` instead of `.text` - `text` provides unicode string, and we'd have to encode them
        # anyway.
        output = six.ensure_str(self._api_request(url, label, full_url=full_url).content)
        log_blob(self.module.debug, '[copr API] {} output'.format(label), output)
        return six.ensure_str(output, 'utf-8')

    def _get_json(self, url: str, label: str, full_url: bool = False) -> Dict[str, Any]:
        try:
            output: Dict[str, Any] = self._api_request(url, label, full_url=full_url).json()
        except Exception:
            raise gluetool.GlueError('Unable to get: {}'.format(url))

        log_dict(self.module.debug, '[copr API] {} output'.format(label), output)
        return output

    def get_build_info(self, build_id: int) -> Dict[str, Any]:
        build_info = self._get_json('api_3/build/{}'.format(build_id), 'build info')

        error = build_info.get('error')
        if error:
            self.module.warn(error)
            return {
                'error': error,
                'ownername': 'UNKNOWN-COPR-OWNER',
                'projectname': 'UNKNOWN-COPR-PROJECT',
                'source_package': {
                    'name': 'UNKNOWN-COPR-COMPONENT',
                    'version': 'UNKNOWN-COPR-VERSION',
                }
            }

        return build_info

    def get_build_task_info(self, build_id: int, chroot_name: str) -> Any:
        build_task_info = self._get_json(
            'api_3/build-chroot?build_id={}&chrootname={}'.format(build_id, chroot_name),
            'build tasks info'
        )

        error = build_task_info.get('error')
        if error:
            self.module.warn('Build task info for {}:{} not found: {}'.format(build_id, chroot_name, error))
            return {
                'state': 'UNKNOWN-COPR-STATUS',
                'error': error
            }

        return build_task_info

    def get_project_builds(self, ownername: str, projectname: str) -> Any:
        return self._get_json(
            'api_3/build/list/?ownername={}&projectname={}'.format(ownername, projectname),
            'get project builds'
        )['items']

    def _result_url(self, build_id: int, chroot_name: str) -> Any:
        build_task_info = self.get_build_task_info(build_id, chroot_name)
        return build_task_info.get('result_url', 'UNKNOWN-COPR-RESULT-DIR-URL')

    def _get_builder_live_log(self, build_id: int, chroot_name: str) -> Optional[str]:
        result_url = self._result_url(build_id, chroot_name)

        if result_url == 'UNKNOWN-COPR-RESULT-DIR-URL':
            return None

        result_url = '{}/builder-live.log.gz'.format(result_url)
        return self._get_text(result_url, 'builder live log', full_url=True)

    def _find_in_log(self, regex: str, build_id: int, chroot_name: str) -> List[str]:
        builder_live_log = self._get_builder_live_log(build_id, chroot_name)

        if not builder_live_log:
            return []

        return list(set(re.findall(regex, builder_live_log)))

    def get_rpm_names(self, build_id: int, chroot_name: str) -> List[str]:
        return self._find_in_log(r'Wrote: /builddir/build/RPMS/(.*)\.rpm', build_id, chroot_name)

    def get_srpm_names(self, build_id: int, chroot_name: str) -> List[str]:
        return self._find_in_log(r'Wrote: /builddir/build/SRPMS/(.*)\.src\.rpm', build_id, chroot_name)

    def add_result_url(self, build_id: int, chroot_name: str, file_names: str) -> List[str]:
        result_url = self._result_url(build_id, chroot_name)
        return ['{}{}.rpm'.format(result_url, file_name) for file_name in file_names]

    def get_repo_url(self, owner: str, project: str, chroot: str) -> str:
        # strip architecture - string following last dash

        match = re.match('(.+)-.+', chroot)
        if not match:
            raise gluetool.GlueError("unable to match chroot with architecture in '{}'".format(chroot))

        return '{0}/coprs/{1}/{2}/repo/{3}/{1}-{2}-{3}.repo'.format(
                self.copr_url,
                owner,
                project,
                match.group(1)
            )


class BuildTaskID(object):
    """
    Build task ID consist of build ID and chroot name. This class covers both values and provides them like
    one string, with following format: '[build_id]:[chroot_name]'
    """

    def __init__(self, build_id: int, chroot_name: str) -> None:
        self.build_id = build_id
        self.chroot_name = chroot_name

    def __str__(self) -> str:
        return '{}:{}'.format(self.build_id, self.chroot_name)

    def __repr__(self) -> str:
        return self.__str__()


class CoprTask(object):
    """
    Covers copr build task and provides all necessary information about it.

    :param BuildTaskID task_id: Task id used to initialization.
    :param gluetool.Module module: Reference to parent's module (used eg. for logging).
    """

    ARTIFACT_NAMESPACE = 'copr-build'

    def __init__(self, task_id: BuildTaskID, module: 'Copr') -> None:
        # as an "official ID", use string representation - some users might be confused by the object,
        # despite it has proper __str__ and __repr__
        self.id = self.dispatch_id = str(task_id)
        self.task_id = task_id

        self.module = module

        self.copr_api = module.copr_api()

        build = self.copr_api.get_build_info(task_id.build_id)
        build_task = self.copr_api.get_build_task_info(task_id.build_id, task_id.chroot_name)

        self.error = build.get('error') or build_task.get('error')
        self.status = build_task['state']
        self.component: str = build['source_package']['name']
        self.target = task_id.chroot_name
        # required API for our modules providing artifacts, we have no tags in copr, use target
        self.destination_tag = self.target
        self.nvr = '{}-{}'.format(self.component, build['source_package']['version'])
        self.owner = build['ownername']
        self.project = build['projectname']
        # issuer is optional item
        self.issuer = build.get('submitter', 'UNKNOWN-COPR-ISSUER')
        self.repo_url = self.copr_api.get_repo_url(self.owner, self.project, self.task_id.chroot_name)

        # this string identifies component in static config file
        self.component_id = '{}/{}/{}'.format(self.owner, self.project, self.component)

        if not self.error:
            self.module.info('Initialized with {}: {} ({})'.format(self.id, self.full_name, self.url))

    @cached_property
    def has_artifacts(self) -> bool:
        # We believe Copr keeps artifacts "forever" - or, at least, long enough to matter to us - therefore
        # we don't even bother to check for their presence.
        return True

    @cached_property
    def rpm_names(self) -> List[str]:
        return self.copr_api.get_rpm_names(self.task_id.build_id, self.task_id.chroot_name)

    @cached_property
    def srpm_names(self) -> List[str]:
        return self.copr_api.get_srpm_names(self.task_id.build_id, self.task_id.chroot_name)

    @cached_property
    def rpm_urls(self) -> List[str]:
        return self.copr_api.add_result_url(
            self.task_id.build_id,
            self.task_id.chroot_name,
            self.rpm_names
        )

    @cached_property
    def srpm_urls(self) -> List[str]:
        return self.copr_api.add_result_url(
            self.task_id.build_id,
            self.task_id.chroot_name,
            self.srpm_names
        )

    @cached_property
    def task_arches(self) -> TaskArches:
        """
        :rtype: TaskArches
        :return: information about arches the task was building for
        """

        return TaskArches([self.target.split('-')[-1]])

    @cached_property
    def url(self) -> str:
        context = dict_update(self.module.shared('eval_context'), {
            'TASK': self
        })

        return render_template(self.module.option('copr-web-url-template'), **context)

    @cached_property
    def full_name(self) -> str:
        """
        String with human readable task details. Used for slightly verbose representation e.g. in logs.

        :rtype: str
        """

        name = [
            "package '{}'".format(self.component),
            "build '{}'".format(self.task_id.build_id),
            "target '{}'".format(self.task_id.chroot_name)
        ]

        return ' '.join(name)

    @cached_property
    def dist_git_repository_name(self) -> str:
        return self.component


class Copr(gluetool.Module):

    name = 'copr'
    description = 'Copr'
    supported_dryrun_level = gluetool.glue.DryRunLevels.DRY

    options = {
        'copr-url': {
            'help': 'Url of Copr build server',
            'type': str
        },
        'copr-web-url-template': {
            'help': """
                    Template of URL leading to the Copr website, displaying the artifact. It has
                    access to all variables available in the eval context, with ``TASK`` representing
                    the task module generates URL for. (default: %(default)s).
                    """,
            'type': str,
            'default': None
        },
        'task-id': {
            'help': 'Copr build task ID, in a form of ``build-id:chroot-name``.',
            'type': str
        }
    }

    required_options = ('copr-url', 'copr-web-url-template')

    shared_functions = ['primary_task', 'tasks', 'copr_api']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(Copr, self).__init__(*args, **kwargs)
        self.task: Optional[CoprTask] = None
        self._tasks: Optional[List[CoprTask]] = None

    def primary_task(self) -> Optional[CoprTask]:
        return self.task

    def tasks(self, task_ids: Optional[List[str]] = None, **kwargs: Any) -> Optional[List[CoprTask]]:

        if not task_ids:
            return self._tasks

        self._tasks = []

        for task_id in task_ids:
            try:
                build_id, chroot_name = [s.strip() for s in task_id.split(':')]
            except ValueError:
                raise gluetool.GlueError(
                    "Invalid copr build with id '{}', must be 'build_id:chroot_name'".format(task_id)
                )

            try:
                self._tasks.append(CoprTask(BuildTaskID(int(build_id), chroot_name), self))
            except gluetool.glue.GlueError as error:
                self.error(str(error))
                raise gluetool.GlueError(
                    "Could not find copr build id '{}' for chroot '{}'".format(build_id, chroot_name)
                )

        return self._tasks

    @property
    def eval_context(self) -> Dict[str, Union[str, Optional[List[CoprTask]], CoprTask]]:
        __content__ = {  # noqa
            'ARTIFACT_TYPE': """
                             Type of the artifact, ``copr-build`` in the case of ``copr`` module.
                             """,
            'BUILD_TARGET': """
                            Build target of the primary task, as known to Koji/Beaker.
                            """,
            'NVR': """
                   NVR of the primary task.
                   """,
            'PRIMARY_TASK': """
                            Primary task, represented as ``CoprTask`` instance.
                            """,
            'TASKS': """
                     List of all tasks known to this module instance.
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
            'TASKS': self.tasks()
        }

    @cached_property
    def _copr_api(self) -> CoprApi:
        return CoprApi(self.option('copr-url'), self)

    def copr_api(self) -> CoprApi:
        return cast(CoprApi, self._copr_api)

    def execute(self) -> None:
        if not self.option('task-id'):
            return

        build_id, chroot_name = [s.strip() for s in self.option('task-id').split(':')]

        build_task_id = BuildTaskID(int(build_id), chroot_name)

        self.task = CoprTask(build_task_id, self)

        if self.task.error:
            raise gluetool.GlueError(
                'Error resolving copr build {}:{}: {}'.format(build_id, chroot_name, self.task.error)
            )

        if not self.task.rpm_urls:
            raise gluetool.GlueError(
                'Error looking up rpm urls for {}:{}, expired build?'.format(build_id, chroot_name)
            )

        self._tasks = [self.task]
