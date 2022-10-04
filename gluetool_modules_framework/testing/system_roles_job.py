# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
import gluetool_modules_framework.libs.dispatch_job

from gluetool.utils import render_template, from_yaml, cached_property

# Type annotations
from typing import cast, Any, Callable, Dict, List, Optional, Tuple, Union, NamedTuple, Set  # noqa

PlatformType = List[Dict[str, Any]]


class SystemRolesJob(gluetool_modules_framework.libs.dispatch_job.DispatchJenkinsJobMixin, gluetool.Module):
    """
    Jenkins job module dispatching system roles testing, as defined in
    ``ci-test-github-ts_sti-artemis-system-roles.yaml`` file

    .. note::

       This module dispatches a Jenkins job, therefore it requires other module to provide connection
       to a Jenkins instance via the shared function ``jenkins``.
    """

    name = 'system-roles-job'
    description = 'Job module dispatching system roles test.'

    # DispatchJenkinsJobMixin.options contains hard defaults
    # pylint: disable=gluetool-option-no-default-in-help,gluetool-option-hard-default
    options = gluetool.utils.dict_update(
        {},
        gluetool_modules_framework.libs.dispatch_job.DispatchJenkinsJobMixin.options,
        {
            'ansible-options': {
                'help': 'Additional options for ``ansible-options`` module.',
                'default': ''
            },
            'dist-git-options': {
                'help': 'Additional options for ``dist-git`` module.',
                'default': ''
            },
            'guess-environment-options': {
                'help': 'Additional options for ``guess-environment`` module.',
                'default': ''
            },
            'artemis-options': {
                'help': 'Additional options for artemis module.',
                'default': ''
            },
            'pipeline-state-reporter-options': {
                'help': 'Additional options for pipeline-state-reporter module',
                'default': ''
            },
            'github-options': {
                'help': 'Additional options for github module.',
                'default': ''
            },
            'test-scheduler-options': {
                'help': 'Additional options for test-scheduler module.',
                'default': ''
            },
            'test-scheduler-system-roles-options': {
                'help': 'Additional options for test-scheduler-system-roles module.',
                'default': ''
            },
            'test-scheduler-sti-options': {
                'help': 'Additional options for test-scheduler-sti module.',
                'default': ''
            },
            'test-schedule-runner-options': {
                'help': 'Additional options for test-schedule-runner module.',
                'default': ''
            },
            'test-schedule-runner-sti-options': {
                'help': 'Additional options for test-schedule-runner-sti module.',
                'default': ''
            },
            'composes-ansibles-matrix': {
                'help': 'List of composes/ansibles variants which will be tested.',
                'action': 'append',
                'default': []
            },
            'compose-sub-to-artemis-options': {
                'help': 'Dictionary of compose substring/artemis options key-value pairs',
                'action': 'append',
                'default': []
            },
            'metadata-path': {
                'help': 'Path to a metadata file',
                'default': ''
            }
        }
    )

    def composes_ansibles_matrix(self):
        # type: () -> List[Dict[str, Any]]
        matrix = []
        for line in self.option('composes-ansibles-matrix').split(',\n'):
            splitted_line = line.split(':')
            matrix.append({
                'compose': render_template(splitted_line[0], **self.shared('eval_context')),
                'ansible-version': splitted_line[1],
                'ansible-path': splitted_line[2]
            })
        return matrix

    def compose_sub_to_artemis_options(self):
        # type: () -> Dict[str, str]
        mapping = {}

        if self.option('compose-sub-to-artemis-options'):
            for pair in self.option('compose-sub-to-artemis-options').split(',\n'):
                try:
                    splitted_pair = pair.split(':')
                    mapping[splitted_pair[0]] = splitted_pair[1]
                except Exception as exc:
                    self.error(cast(str, exc))
                    self.warning('Pair {} has the invalid format, skipping this one'.format(pair))
                    continue

        return mapping

    def test_pull_request(self):
        # type: () -> bool
        primary_task = self.shared('primary_task')

        # Check if 'citest' comment exists and author is collaborator
        if primary_task.comment:
            return '[citest' in primary_task.comment \
                and primary_task.comment_author_is_collaborator

        # Check if '[citest skip]' is not present and author is collaborator
        if primary_task.pull_head_branch_owner_is_collaborator:
            return '[citest skip]' not in primary_task.title

        return False

    @cached_property
    def parse_platforms_from_meta(self):
        # type: () -> Optional[PlatformType]

        meta_file = from_yaml(
            self.shared('primary_task').get_file_from_pull_request(self.option('metadata-path'))
        )

        return cast(Optional[PlatformType], meta_file['galaxy_info'].get('platforms', None))

    @cached_property
    def get_transformed_platforms(self):
        # type: () -> Optional[PlatformType]
        """
        In Ansible the 'EL' platform means both RHEL and CentOS.
        It is needed to be transformed to simplify compose check.
        The method replaces `EL` platform with `RHEL` and `CentOS`
        and `CentOS-Stream`.
        """
        platforms = self.parse_platforms_from_meta
        if not platforms:
            return None

        transformed_platforms = []
        for platform in platforms:
            if platform['name'] == 'EL':
                transformed_platforms.append({'name': 'RHEL', 'versions': platform['versions']})
                transformed_platforms.append({'name': 'CentOS', 'versions': platform['versions']})
                transformed_platforms.append({'name': 'CentOS-Stream', 'versions': platform['versions']})
            else:
                transformed_platforms.append(platform)
        return transformed_platforms

    def is_compose_supported(self, compose):
        # type: (str) -> bool
        """
        Check if compose is supported. The method tries to find
        `name-version` substring in compose name where name is a distribution name
        and version is a major version of the distribution.
        """
        platforms = self.get_transformed_platforms
        # If no platform is specified, the role supports all platforms
        if not platforms:
            return True

        for platform in platforms:
            for version in platform['versions']:
                if version == 'all':
                    if platform['name'] in compose:
                        return True
                else:
                    if '{}-{}'.format(platform['name'], version) in compose:
                        return True
        return False

    def execute(self):
        # type: () -> None

        common_build_params = {
            'ansible_options': self.option('ansible-options'),
            'dist_git_options': self.option('dist-git-options'),
            'guess_environment_options': self.option('guess-environment-options'),
            'artemis_options': self.option('artemis-options'),
            'github_options': self.option('github-options'),
            'pipeline_state_reporter_options': self.option('pipeline-state-reporter-options'),
            'test_scheduler_options': self.option('test-scheduler-options'),
            'test_scheduler_system_roles_options': self.option('test-scheduler-system-roles-options'),
            'test_scheduler_sti_options': self.option('test-scheduler-sti-options'),
            'test_schedule_runner_options': self.option('test-schedule-runner-options'),
            'test_schedule_runner_sti_options': self.option('test-schedule-runner-sti-options'),
        }

        # Do nothing if branch or comment author is not a collaborator or [citest skip]
        if not self.test_pull_request():
            return

        primary_task = self.shared('primary_task')

        for compose_ansible_dict in self.composes_ansibles_matrix():

            compose = compose_ansible_dict['compose']
            ansible_version = compose_ansible_dict['ansible-version']
            ansible_path = compose_ansible_dict['ansible-path']

            pr_label = '{}/ansible-{}/(citool)'.format(
                compose, ansible_version
            )

            if not self.is_compose_supported(compose):
                self.shared(
                    'set_pr_status',
                    'success',
                    'The role does not support this platform. Skipping.',
                    context=pr_label
                )
                continue

            if primary_task.comment and primary_task.commit_statuses.get(pr_label):
                # if comment is [citest bad] comment, trigger only failure or error tests
                if '[citest bad]' in primary_task.comment.lower():
                    if primary_task.commit_statuses[pr_label]['state'] not in ['error', 'failure']:
                        self.info('skipping {}, not error nor failure'.format(pr_label))
                        continue
                # if comment is [citest pending] comment, trigger only pending tests
                if '[citest pending]' in primary_task.comment.lower():
                    if primary_task.commit_statuses[pr_label]['state'] != 'pending':
                        self.info('skipping {}, not pending'.format(pr_label))
                        continue

            self.build_params = common_build_params.copy()

            for substring, option in self.compose_sub_to_artemis_options().items():
                if substring in compose.lower():
                    self.build_params['artemis_options'] += option

            self.build_params['guess_environment_options'] += ' --compose-method=force --compose={}'.format(compose)

            self.build_params['test_scheduler_system_roles_options'] += ' --ansible-playbook-filepath={}'.format(
                ansible_path
            )

            if ansible_version != '2.9':
                self.build_params['test_scheduler_system_roles_options'] += ' --collection'

            self.build_params['pipeline_state_reporter_options'] += ' --pr-label={}'.format(pr_label)

            self.build_params['ansible_options'] += (
                ' --ansible-playbook-options=--extra-vars=ansible_playbook_filepath={}'.format(
                    ansible_path
                    ))

            self.shared('jenkins').invoke_job('ci-test-github-ts_sti-artemis-system-roles', self.build_params)
