# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
import re
import six

from gluetool.log import log_dict
from gluetool_modules_framework.libs.artifacts import splitFilename
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.test_schedule import TestSchedule
from gluetool_modules_framework.testing.test_scheduler_sti import TestScheduleEntry

# Type annotations
from typing import List, Optional, cast  # noqa


def product_version(product):
    # type: (str) -> str
    matched_product = re.match(r'(?i).*rhel-(\d.\d)', product)

    if not matched_product:
        raise gluetool.GlueError('Unexpected product format: {}'.format(product))

    return matched_product.group(1)


def format_for_pes(product):
    # type: (str) -> str
    return 'RHEL {}'.format(product_version(product))


def format_for_test(product):
    # type: (str) -> str
    return product_version(product)


class TestSchedulerUpgrades(gluetool.Module):

    name = 'test-scheduler-upgrades'
    description = 'Prepare schedule for upgrade testing. Modify schedule entries provided by previous (STI) provider.'

    options = {
        'variant': {
            'help': 'Determine, if we test upgrade *from* the package or upgrade *to* the package.',
            'choices': ('from', 'to'),
        },
        'destination': {
            'help': 'Version of targeted system in a RHEL-X.Y format.',
            'type': str
        }
    }

    required_options = ('variant',)

    shared_functions = ['create_test_schedule']

    def sanity(self):
        # type: () -> None
        if self.option('variant') == 'from' and not self.option('destination'):
            raise gluetool.GlueError('Option `destination` is required when `variant` is set to `from`.')

    def binary_rpms_list(self, compose_url, components):
        # type: (str, List[str]) -> List[str]

        # List of binary package names is obtained from compose metadata (metadata/rpms.json).
        # Only x86_64 builds are considered, upgrades for other arches are not yet supported.
        metadata_rpms_json_path = '{}/metadata/rpms.json'.format(compose_url)

        with gluetool.utils.requests(logger=self.logger) as requests:
            try:
                response = requests.get(metadata_rpms_json_path)
                response.raise_for_status()
            except requests.exceptions.RequestException:
                raise gluetool.GlueError('Unable to fetch compose metadata from: {}'.format(metadata_rpms_json_path))

        metadata_rpms_json = response.json()

        binary_rpms_set = set()

        for repo_name in metadata_rpms_json['payload']['rpms']:
            for srpm_name in metadata_rpms_json['payload']['rpms'][repo_name]['x86_64']:
                if splitFilename(srpm_name)[0] in components:
                    binary_rpms_set.update(
                        metadata_rpms_json['payload']['rpms'][repo_name]['x86_64'][srpm_name].keys()
                    )

        binary_rpms_set = {
            six.ensure_str(package) for package in binary_rpms_set if not package.endswith('.src')
        }
        log_dict(self.debug, 'binary rpm nevrs found in compose', sorted(binary_rpms_set))

        binary_rpms_list = sorted({splitFilename(package)[0] for package in binary_rpms_set})
        log_dict(self.info, 'binary rpm names found in compose', binary_rpms_list)

        if not binary_rpms_list:
            log_dict(self.warn, 'No x86_64 binary rpm names found for packages', components)

        return binary_rpms_list

    def create_test_schedule(self, testing_environment_constraints=None):
        # type: (Optional[List[TestingEnvironment]]) -> TestSchedule
        """
        This module modifies STI test schedule provided by other module. It expects one of the test is testing upgrade
        and require special variables for successful run. Namely url of composes, made by OSCI guys based on tested
        artifact and list of binary package names, which belongs to the artifact.
        """

        self.require_shared('primary_task', 'get_compose_url', 'product')

        component = self.shared('primary_task').component
        compose_url = self.shared('get_compose_url')
        product = self.shared('product')

        variant = self.option('variant')

        if variant == 'from':
            destination = self.option('destination')

            self.require_shared('successor_components')
            successor_components = self.shared(
                'successor_components',
                component,
                format_for_pes(product),
                format_for_pes(destination)
            )

            if successor_components:
                log_dict(self.info, "Successor components of '{}'".format(component), successor_components)
                components = successor_components
            else:
                self.info("No successors of components '{}' found, assume successor's name is the same.".format(
                    component))
                components = [component]

        elif variant == 'to':
            destination = product
            components = [component]

        new_variables = {
            'compose_url': compose_url,
            'binary_rpms_list': self.binary_rpms_list(compose_url, components),
            'target_release':  format_for_test(destination)
        }

        schedule = self.overloaded_shared(
            'create_test_schedule',
            testing_environment_constraints=testing_environment_constraints
        )  # type: TestSchedule

        for schedule_entry in schedule:
            if not isinstance(schedule_entry, TestScheduleEntry):
                continue

            log_dict(self.debug, 'old variables', schedule_entry.variables)

            # `schedule_entry.variables` can contain variables given by user, we do not want to overwrite them
            new_variables.update(schedule_entry.variables)
            schedule_entry.variables = new_variables

            log_dict(self.debug, 'new variables', schedule_entry.variables)

        return schedule
