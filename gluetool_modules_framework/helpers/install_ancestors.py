# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import functools
import os
import re

import gluetool
from gluetool.log import log_dict
from gluetool.result import Ok, Error
from gluetool.utils import normalize_multistring_option, render_template
from gluetool_modules_framework.libs.guest_setup import guest_setup_log_dirpath, GuestSetupOutput, GuestSetupStage, \
    SetupGuestReturnType
from gluetool_modules_framework.libs.sut_installation import SUTInstallation

# Type annotations
from typing import cast, Any, Callable, List, Optional, Set  # noqa
from gluetool_modules_framework.libs.guest import NetworkedGuest


FuncType = Callable[..., List[str]]


class InstallAncestors(gluetool.Module):
    """
    Installs ancestors of the component defined by ``primary_task`` on given guest.

    The ancestor packages are resolved from ``primary_task``. In first step ``ancestor_components`` shared function
    is used to get ancestor components and then ``component_rpms`` shared function is used to get rpms built for each
    ancestor component.

    It's possible to override or exclude the ancestor components or rpms by providing corresponding options.
    """

    name = 'install-ancestors'
    description = 'Install ancestors of the component defined by primary_task on given guest.'

    options = {
        'ancestor-components': {
            'help': """
                Ancestor components to be installed on the guest (default: none).
                Overrides the ancestor resolved from primary_task. Mutually exclusive with ancestor-rpms option.
                """,
            'default': [],
            'action': 'append'
        },
        'ancestor-rpms': {
            'help': """
                Ancestor rpms to be installed on the guest (default: none).
                Overrides the ancestor resolved from primary_task. Mutually exclusive with ancestor-components option.
                """,
            'default': [],
            'action': 'append'
        },
        'exclude-components': {
            'help': """
                Name of components to be excluded from installation on the guest (default: none).
                The component to be excluded can by specified either by plain string or regular expression.
                """,
            'default': [],
            'action': 'append'
        },
        'exclude-rpms': {
            'help': """
                Name of rpms to be excluded from installation on the guest (default: none).
                The rpm to be excluded can by specified either by plain string or regular expression.
                """,
            'default': [],
            'action': 'append'
        },
        'major-version-pattern': {
            'help': """
                Regex with match group to extract major version from primary task's destination tag.
                """
        },
        'release-template': {
            'help': """
                Release string usable with Package Evolution Service.
                Variable MAJOR_VERSION is passed to the template when rendered.
                """
        },
        'rpms-arches': {
            'help': """
                Architectures of the ancestor rpms to be installed on the guest.
                """,
            'default': [],
            'action': 'append'

        },
        'source-release': {
            'help': 'Release for looking up ancestors.'
        },
        'log-dir-name': {
            'help': 'Name of directory where outputs of installation commands will be stored (default: %(default)s).',
            'type': str,
            'default': 'artifact-installation'
        }
    }

    shared_functions = ['setup_guest']

    @staticmethod
    def reduce_list_option(option_value: List[str]) -> List[str]:
        """
        Get rid of empty values from option list.
        """

        return sorted(opt for opt in normalize_multistring_option(option_value) if opt)

    def filter_list(self, lst: List[str], option_name: str) -> List[str]:
        """
        Filter out list items using provided option.

        :param List[str] lst: List to be filtered.
        :param str option_name: Option name from which filters are retrieved.
        """

        # do not filter anything when the option is not set
        if not self.option(option_name):
            return lst

        excluded_items: Set[str] = set()
        for exclude in normalize_multistring_option(self.option(option_name)):
            regex = re.compile(exclude)
            excluded_items.update(filter(regex.match, lst))

        if excluded_items:
            log_dict(self.info, "Following items were filtered out by '{}' filter".format(option_name),
                     sorted(excluded_items))
        else:
            self.info("No items were filtered out by '{}' filter".format(option_name))

        return sorted(list(set(lst) - excluded_items))

    def filter_list_decorator(option_name: str) -> Callable[[FuncType], FuncType]:  # type: ignore[misc]  # noqa: F821
        # This decorator needs to be a class memeber as it only makes sense to use it for decorating instance methods.
        # mypy then complains about missing self argument, which needs to be ignored because of
        # https://github.com/python/mypy/issues/7778
        """
        Decorator for filtering results (list) of instance methods.

        :param str option_name: Option name from which filters are retrieved.
        """

        def decorator(func: FuncType) -> FuncType:
            @functools.wraps(func)
            def wrapper(self: 'InstallAncestors') -> List[str]:
                lst = func(self)
                lst = self.filter_list(lst, option_name)
                return lst
            return wrapper
        return decorator

    @filter_list_decorator(option_name='exclude-components')
    def get_ancestor_components(self) -> List[str]:
        """
        Return ancestor components of the component defined by ``primary_task``.
        """

        self.require_shared('primary_task')
        component = self.shared('primary_task').component

        ancestor_components = self.option('ancestor-components')
        if ancestor_components:
            ancestor_components = self.reduce_list_option(ancestor_components)
            log_dict(self.info, "Ancestor components of component '{}' set by option".format(component),
                     ancestor_components)
            return cast(List[str], ancestor_components)

        target_major_version = self.shared('primary_task').rhel
        target_release = render_template(
            self.option('release-template'),
            logger=self.logger,
            **{
                'MAJOR_VERSION': target_major_version
            }
        )

        self.require_shared('ancestor_components')
        ancestor_components = self.shared('ancestor_components', component, target_release)

        # If no ancestor component was found, assume it's the same
        if not ancestor_components:
            self.info("No ancestors of component '{}' found, assume ancestor's name is the same.".format(component))
            ancestor_components = [component]

        log_dict(self.info, "Ancestor components of component '{}' set by shared function".format(component),
                 ancestor_components)
        return cast(List[str], ancestor_components)

    @filter_list_decorator(option_name='exclude-rpms')
    def get_ancestor_rpms(self) -> List[str]:
        """
        Return ancestor rpms of the component defined by ``primary_task``.
        """

        self.require_shared('primary_task')
        component = self.shared('primary_task').component

        ancestor_rpms = self.option('ancestor-rpms')
        if ancestor_rpms:
            ancestor_rpms = self.reduce_list_option(ancestor_rpms)
            log_dict(self.info, 'Ancestor rpms set by option', ancestor_rpms)
            return cast(List[str], ancestor_rpms)

        ancestor_components = self.get_ancestor_components()

        # The source-release option usually contains space but Jenkins somehow messes it up when passing it to
        # gluetool pipeline. The workaround is to use hashes instead of spaces as in test-scheduler-sti module.
        source_release = self.option('source-release').replace('#', ' ')

        major_version_pattern = self.option('major-version-pattern')
        source_version_match = re.match(major_version_pattern, source_release)
        if not source_version_match:
            raise gluetool.GlueError('Unexpected format of source release: {}'.format(source_release))
        source_major_version = int(source_version_match.group(1))
        target_major_version = int(self.shared('primary_task').rhel)
        if source_major_version != target_major_version - 1:
            raise gluetool.GlueError("Target '{}' and source '{}' major version mismatch!".format(target_major_version,
                                                                                                  source_major_version))

        architectures = normalize_multistring_option(self.option('rpms-arches'))
        self.require_shared('component_rpms')
        for ancestor_component in ancestor_components:
            rpms = self.shared('component_rpms', ancestor_component, source_release, architectures)
            ancestor_rpms.extend(rpms)
        ancestor_rpms.sort()
        if not ancestor_rpms:
            self.warning("No binary rpms for ancestors of component '{}' in release '{}' were built "
                         "for architectures '{}'.".format(component, source_release, ', '.join(architectures)))
        else:
            log_dict(self.info, "Binary rpms for ancestors of component '{}' in release '{}' built for "
                     "architectures '{}'".format(component, source_release, ', '.join(architectures)), ancestor_rpms)

        return cast(List[str], ancestor_rpms)

    def setup_guest(self,
                    guest: NetworkedGuest,
                    stage: GuestSetupStage = GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
                    log_dirpath: Optional[str] = None,
                    **kwargs: Any) -> SetupGuestReturnType:

        self.require_shared('primary_task')

        log_dirpath = guest_setup_log_dirpath(guest, log_dirpath)

        r_overloaded_guest_setup_output: SetupGuestReturnType = self.overloaded_shared(
            'setup_guest',
            guest,
            stage=stage,
            log_dirpath=log_dirpath,
            **kwargs
        )

        if r_overloaded_guest_setup_output is None:
            r_overloaded_guest_setup_output = Ok([])

        if r_overloaded_guest_setup_output.is_error:
            return r_overloaded_guest_setup_output

        if stage != GuestSetupStage.ARTIFACT_INSTALLATION:
            return r_overloaded_guest_setup_output

        # no rpms to install
        ancestor_rpms = self.get_ancestor_rpms()
        if not ancestor_rpms:
            self.info('No ancestor rpms to install.')
            return r_overloaded_guest_setup_output

        guest_setup_output = r_overloaded_guest_setup_output.unwrap() or []

        installation_log_dirpath = os.path.join(
            log_dirpath,
            '{}-{}'.format(self.option('log-dir-name'), guest.name)
        )

        primary_task = self.shared('primary_task')

        sut_installation = SUTInstallation(self, installation_log_dirpath, primary_task, logger=guest.logger)

        joined_rpms = ' '.join(ancestor_rpms)

        # SUTInstallation takes care of substituting yum with dnf if available
        sut_installation.add_step('Installing ancestor rpms', 'yum -y install {}'.format(joined_rpms))

        sut_result = sut_installation.run(guest)

        guest_setup_output += [
            GuestSetupOutput(
                stage=stage,
                label='Ancestor installation',
                log_path=installation_log_dirpath,
                additional_data=sut_installation
            )
        ]

        if sut_result.is_error:
            assert sut_result.error is not None

            return Error((
                guest_setup_output,
                sut_result.error
            ))

        return Ok(guest_setup_output)

    def sanity(self) -> None:

        # combination of options:
        # ancestor-components
        #     mutually exclusive with ancestor-rpms
        #     if present, option release-template is not needed
        # ancestor-rpms
        #     mutually exclusive with ancestor-components
        #     if present, options source-release, major-version-pattern and rpms-arches are not needed

        components_option = self.option('ancestor-components')
        rpms_option = self.option('ancestor-rpms')

        if components_option and rpms_option:
            raise gluetool.utils.IncompatibleOptionsError("Options '--ancestor-components' and '--ancestor-rpms' "
                                                          "are mutually exclusive")

        required_options = list()
        if not components_option:
            required_options.append('release-template')
        if not rpms_option:
            required_options.append('major-version-pattern')
            required_options.append('source-release')
            required_options.append('rpms-arches')

        missing_options = list()
        for option_name in required_options:
            if not self.option(option_name):
                missing_options.append(option_name)

        if missing_options:
            raise gluetool.GlueError("Missing required option(s): {}".format(', '.join(missing_options)))
