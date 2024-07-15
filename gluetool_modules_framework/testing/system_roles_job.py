# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0
import re

import gluetool
import gluetool_modules_framework.libs.dispatch_job

from gluetool.utils import render_template, from_yaml, cached_property

# Type annotations
from typing import cast, Any, Callable, Dict, List, Optional, Tuple, Union, NamedTuple, Set  # noqa


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
            'test-scheduler-baseosci-options': {
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

    def composes_ansibles_matrix(self) -> List[Dict[str, Any]]:
        matrix = []
        for line in self.option('composes-ansibles-matrix').split(',\n'):
            splitted_line = line.split(':')
            matrix.append({
                'compose': render_template(splitted_line[0], **self.shared('eval_context')),
                'ansible-version': splitted_line[1],
                'ansible-path': splitted_line[2]
            })
        return matrix

    def compose_sub_to_artemis_options(self) -> Dict[str, str]:
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

    def test_pull_request(self) -> bool:
        # Check if 'citest' comment exists and author is collaborator
        primary_task = self.shared('primary_task')
        if primary_task.comment:
            return '[citest' in primary_task.comment \
                and primary_task.comment_author_is_collaborator

        # Check if '[citest skip]' is not present and author is collaborator
        if primary_task.pull_head_branch_owner_is_collaborator:
            return '[citest skip]' not in primary_task.title

        return False

    @cached_property
    def parse_platforms_from_meta(self) -> Set[str]:
        """
        Implement https://issues.redhat.com/browse/SYSROLES-38
        'Fedora', 'EL', etc. without a version means all versions
        """
        meta_file = from_yaml(
            self.shared('primary_task').get_file_from_pull_request(self.option('metadata-path'))
        )

        rv = set()
        have_specific_version = set()
        for platform in meta_file['galaxy_info'].get('platforms', []):
            lower_name = platform['name'].lower()
            rv.add(lower_name)
            for version in platform.get('versions', []):
                if version == 'all':
                    continue
                rv.add(lower_name + '-' + version)
                # we have a specific version of something e.g. 'fedora40'
                # instead of just 'fedora'
                have_specific_version.add(lower_name)
        for tag in meta_file['galaxy_info'].get('galaxy_tags', []):
            match = re.match(r'([a-z]+)(\d+)$', tag)
            if match:
                # we have a specific version of something e.g. 'fedora40'
                # instead of just 'fedora'
                rv.add(match.group(1) + '-' + match.group(2))
                have_specific_version.add(match.group(1))
            else:
                rv.add(tag)
        for tag in have_specific_version:
            # if we have both 'fedora' and 'fedora40' in the list, remove
            # 'fedora' - that is - the role doesn't support all versions of
            # 'fedora', just the specific versions listed
            if tag in rv:
                rv.remove(tag)

        self.debug('SystemRolesJob:platforms supported by role {}'.format(rv))
        return rv  # e.g. fedora, el-8, el-9, el-10

    def get_short_rhel_compose(self, compose: str) -> str:
        """
        Transform compose name to the short version RHEL-X.Y[.Z]
        """
        match = re.match(r'RHEL-\d+(?:\.\d+)*', compose)
        if match:
            return match.group(0)

        # If failed, return original string
        return compose

    def is_compose_supported(self, compose: str) -> bool:
        """
        Check if compose is supported. The method first gets a list of
        role_platforms (including versions), then normalizes the given
        compose string to the Ansible meta format, then sees if that
        compose is in the list of supported role platforms.
        role_platforms is normalized to lower case - do the same with
        compose_platform
        """
        role_platforms = self.parse_platforms_from_meta
        # If no platform is specified, the role supports all platforms
        if not role_platforms:
            return True

        # convert compose platform, version to ansible meta format
        match = re.match(r'CentOS-Stream-(\d+)', compose)
        if match:
            compose_platform = 'el'
            compose_version = match.group(1)
        else:
            # should be of the form PLATFORMNAME-MAJORVERSION
            match = re.match(r'([^-]+)-(\d+)', compose)
            if match:
                compose_platform = match.group(1).lower()
                compose_version = match.group(2)
                if compose_platform in ['rhel', 'centos']:
                    compose_platform = 'el'
            elif compose == 'Fedora-Rawhide':
                compose_platform = 'fedora'
                compose_version = ''
            else:
                compose_platform = compose.lower()  # not sure what this could be
                compose_version = ''

        return compose_platform in role_platforms or compose_platform + '-' + compose_version in role_platforms

    def execute(self) -> None:

        common_build_params = {
            'ansible_options': self.option('ansible-options'),
            'dist_git_options': self.option('dist-git-options'),
            'guess_environment_options': self.option('guess-environment-options'),
            'artemis_options': self.option('artemis-options'),
            'github_options': self.option('github-options'),
            'pipeline_state_reporter_options': self.option('pipeline-state-reporter-options'),
            'test_scheduler_baseosci_options': self.option('test-scheduler-baseosci-options'),
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

            pr_label = '{}/ansible-{}'.format(
                self.get_short_rhel_compose(compose), ansible_version
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

            if ansible_version == '2.9':
                self.build_params['ansible_options'] += (
                    ' --ansible-playbook-environment-variables ANSIBLE_CALLBACK_WHITELIST=profile_tasks'
                )

            else:
                self.build_params['test_scheduler_system_roles_options'] += ' --collection'
                # callback_whitelist is deprecated in ansible >2.9
                self.build_params['ansible_options'] += (
                    ' --ansible-playbook-environment-variables ANSIBLE_CALLBACKS_ENABLED=profile_tasks'
                )

            self.build_params['pipeline_state_reporter_options'] += ' --pr-label={}'.format(pr_label)

            self.build_params['ansible_options'] += (
                ' --ansible-playbook-options=--extra-vars=ansible_playbook_filepath={}'.format(
                    ansible_path
                    ))

            self.shared('jenkins').invoke_job('ci-test-github-ts_sti-artemis-system-roles', self.build_params)
