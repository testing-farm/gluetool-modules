# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
import gluetool_modules_framework.libs.dispatch_job


class OSHJob(gluetool_modules_framework.libs.dispatch_job.DispatchJenkinsJobMixin, gluetool.Module):
    """
    Jenkins job module dispatching OSH testing, as defined in ``ci-osh.yaml`` file.

    .. note::

       Value of the ``--id`` option is, by default, first searched in the environment, at it is expected
       to be set by Jenkins' machinery, e.g. by the ``redhat-ci-plugin``.

    .. note::

       This module dispatches a Jenkins job, therefore it requires other module to provide connection
       to a Jenkins instance via the shared function ``jenkins``.
    """

    name = 'osh-job'
    description = 'Create and run osh job'

    job_name = 'ci-osh'
