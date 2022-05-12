# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
from gluetool.utils import cached_property, dict_update
import gluetool_modules_framework.libs.dispatch_job

# Type annotations
from typing import Dict, Optional  # noqa


DEFAULT_WOW_OPTIONS_SEPARATOR = '#-#-#-#-#'

PROXIED_MODULE_NAMES = (
    'ansible',
    'artemis',
    'brew',
    'brew-build-task-params',
    'build-dependencies',
    'coldstore',
    'compose-url',
    'dashboard',
    'dist-git',
    'github',
    'guess-environment',
    'guest-setup',
    'install-brew-build-options',
    'install-mbs-build-options',
    'openstack',
    'pipeline-install-ancestors',
    'test-scheduler',
    'test-scheduler-sti',
    'test-scheduler-upgrades',
    'test-schedule-report',
    'test-schedule-runner',
    'test-schedule-runner-restraint',
    'test-schedule-tmt',
)


class CIJob(gluetool_modules_framework.libs.dispatch_job.DispatchJenkinsJobMixin, gluetool.Module):
    """
    Jenkins job module dispatching a given CI testing pipeline.

    .. note::

       Value of the ``--id`` option is, by default, first searched in the environment, and it is expected
       to be set by Jenkins' machinery, e.g. by the ``redhat-ci-plugin``.

    .. note::

       This module dispatches a Jenkins job, therefore it requires another module to provide connection
       to a Jenkins instance via the shared function ``jenkins``.
    """

    name = 'ci-job'
    description = 'Dispatch CI job'

    job_name = 'ci-job'

    # DispatchJenkinsJobMixin.options contain hard defaults
    # pylint: disable=gluetool-option-no-default-in-help,gluetool-option-hard-default
    options = dict_update(
        {},
        gluetool_modules_framework.libs.dispatch_job.DispatchJenkinsJobMixin.options,
        {
            '{}-options'.format(module_name): {
                'help': 'Additional options for ``{}`` module.'.format(module_name),
                'default': ''
            }
            for module_name in PROXIED_MODULE_NAMES
        },
        {
            'wow-options': {
                'help': 'Additional options for workflow-tomorrow.',
                'action': 'append',
                'default': []
            },
            'wow-options-separator': {
                'help': """
                        Due to technical limitations of Jenkins, when jobs want to pass multiple ``--wow-options``
                        instances to this module, it is necessary to encode them into a single string. To tell them
                        apart, this SEPARATOR string is used (default: %(default)s).
                        """,
                'metavar': 'SEPARATOR',
                'type': str,
                'action': 'store',
                'default': DEFAULT_WOW_OPTIONS_SEPARATOR
            },

            # following options are passed to brew-build-task-params module
            'install-rpms-blacklist': {
                'help': """
                        Regexp pattern (compatible with ``egrep``) - when installing build, matching packages will
                        **not** be installed (default: %(default)s).
                        """,
                'type': str,
                'default': ''
            },
            'install-method': {
                'help': 'Yum method to use for installation (default: %(default)s).',
                'type': str,
                'default': 'multi'
            }
        }
    )

    @cached_property
    def build_params(self):
        # type: () -> Dict[str, Optional[str]]
        brew_build_task_params_options = self.option('brew-build-task-params-options')
        install_rpms_blacklist = self.option('install-rpms-blacklist')
        install_method = self.option('install-method')

        if install_rpms_blacklist:
            brew_build_task_params_options = '{} --install-rpms-blacklist={}'.format(brew_build_task_params_options,
                                                                                     install_rpms_blacklist)

        if install_method:
            brew_build_task_params_options = '{} --install-method={}'.format(brew_build_task_params_options,
                                                                             install_method)

        wow_options = self.option('wow-options-separator').join(self.option('wow-options'))

        return dict_update(
            super(CIJob, self).build_params,
            {
                '{}_options'.format(module_name.replace('-', '_')): self.option('{}-options'.format(module_name))
                for module_name in PROXIED_MODULE_NAMES
            },
            {
                'brew_build_task_params_options': brew_build_task_params_options,
                'wow_options': wow_options
            }
        )
