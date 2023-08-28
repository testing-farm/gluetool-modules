# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import re

# Type annotations
from typing import Optional, cast

import six

import gluetool
from gluetool.log import log_dict
from gluetool_modules_framework.libs.artifacts import splitFilename
from gluetool_modules_framework.libs.test_schedule import TestSchedule
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.testing.test_scheduler_sti import TestScheduleEntry


class TestSchedulerUpgrades(gluetool.Module):
    '''
    Module that prepares test schedule for upgrade testing.
    '''

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
        },
        'repos': {
            'help': 'Repos to look for binary rpms. If not specified, all repos will be searched.',
            'action': 'append',
            'type': str,
            'default': []
        },
        'exclude-repos': {
            'help': 'Repos to exclude from the binary rpms search. Mutually exclusive with repos option.',
            'action': 'append',
            'type': str,
            'default': []
        },
        'compose-url': {
            'help': 'Url of compose.',
            'type': str
        },
        'product-pattern': {
            'help': 'Regular expression used to extract major and minor version from the product name.',
            'type': str
        }
    }

    required_options = ('variant', 'compose-url', 'product-pattern')

    shared_functions = ['create_test_schedule']

    def sanity(self) -> None:
        '''
        Check correct combination of options.
        '''

        if self.option('variant') == 'from' and not self.option('destination'):
            msg = 'Option `destination` is required when `variant` is set to `from`.'
            raise gluetool.GlueError(msg)
        if self.option('repos') and self.option('exclude-repos'):
            msg = 'Options `repos` and `exclude-repos` are mutually exclusive.'
            raise gluetool.GlueError(msg)

    def product_version(self, product: str) -> tuple[str, str]:
        '''
        Get major and minor versions out of the product name using pattern provided by product-pattern option.
        '''

        product_pattern = self.option('product-pattern')
        match = re.match(product_pattern, product)

        if not match:
            msg = f'Unexpected product format: {product}'
            raise gluetool.GlueError(msg)

        versions = match.groupdict()
        return cast(str, versions['major']), cast(str, versions['minor'])

    def format_for_pes(self, product: str) -> str:
        '''
        Get release string suitable for use with Package Evolution Service.
        '''

        return f'RHEL {self.product_version(product)}'

    def format_for_leapp(self, product: str) -> str:
        '''
        Get version string suitable for use with Leapp.
        '''

        major, minor = self.product_version(product)
        return f'{major}.{minor}'

    def binary_rpms_list(self, compose_url: str, components: list[str]) -> list[str]:
        '''
        Return list of package names that belong to provided components (srpms).

        Package names are obtained from compose metadata (metadata/rpms.json).
        Only x86_64 builds are considered, upgrades for other arches are not yet supported.
        '''

        metadata_rpms_json_path = f'{compose_url}/metadata/rpms.json'

        with gluetool.utils.requests(logger=self.logger) as requests:
            try:
                response = requests.get(metadata_rpms_json_path)
                response.raise_for_status()
            except requests.exceptions.RequestException as requests_exception:
                message = f'Unable to fetch compose metadata from: {metadata_rpms_json_path}'
                raise gluetool.GlueError(message) from requests_exception

        metadata_rpms_json = response.json()

        binary_rpms_set = set()

        repos = metadata_rpms_json['payload']['rpms'].keys()
        if self.option('repos'):
            repos = gluetool.utils.normalize_multistring_option(self.option('repos'))
        elif self.option('exclude-repos'):
            repos = [
                repo for repo in repos
                if repo not in gluetool.utils.normalize_multistring_option(self.option('exclude-repos'))]

        for repo_name in repos:
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

    def create_test_schedule(
        self,
        testing_environment_constraints: Optional[list[TestingEnvironment]] = None
    ) -> TestSchedule:
        '''
        Modify STI test schedule with variables special to the upgrade test.

        Its expected that one of the schedule entries is for upgrade test, which requires special variables for
        successful run. These variables are added here and include:

          * compose_url - url of the compose used during the upgrade
          * binary_rpms_list - list of rpm names added to the upgrade transaction
          * target_release - target release for the upgrade test
        '''

        self.require_shared('primary_task', 'product')

        component = self.shared('primary_task').component
        compose_url = self.option('compose-url')
        product = self.shared('product')

        variant = self.option('variant')

        if variant == 'from':
            destination = self.option('destination')

            self.require_shared('successor_components')
            successor_components = self.shared(
                'successor_components',
                component,
                self.format_for_pes(product),
                self.format_for_pes(destination)
            )

            if successor_components:
                log_dict(self.info, f"Successor components of '{component}'", successor_components)
                components = successor_components
            else:
                self.info(f"No successors of components '{component}' found, assume successor's name is the same.")
                components = [component]

        elif variant == 'to':
            destination = product
            components = [component]

        new_variables = {
            'compose_url': compose_url,
            'binary_rpms_list': self.binary_rpms_list(compose_url, components),
            'target_release':  self.format_for_leapp(destination)
        }

        schedule: TestSchedule = self.overloaded_shared(
            'create_test_schedule',
            testing_environment_constraints=testing_environment_constraints
        )

        for schedule_entry in schedule:
            if not isinstance(schedule_entry, TestScheduleEntry):
                continue

            log_dict(self.debug, 'old variables', schedule_entry.variables)

            # `schedule_entry.variables` can contain variables given by user, we do not want to overwrite them
            new_variables.update(schedule_entry.variables)
            schedule_entry.variables = new_variables

            log_dict(self.debug, 'new variables', schedule_entry.variables)

        return schedule
