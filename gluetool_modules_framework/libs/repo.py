# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import configparser
import io
import shlex
import urllib.parse

from gluetool_modules_framework.libs.sut_installation import SUTInstallation
from gluetool_modules_framework.libs.artifacts import DEFAULT_PACKAGE_LIST, package_list_path


def create_repo(sut_installation: SUTInstallation, repo_name: str, repo_path: str, *,
                pkglist: str = DEFAULT_PACKAGE_LIST) -> None:
    """
    Create a repository from packages inside the directory.

    :param sut_installation: The SUT instance to add the command to.
    :param repo_name: Name of the repository to create.
    :param repo_path: Path of the directory containing the repo and packages.
    :param pkglist: Optional override for the list of packages. This is used to ensure only intended packages are
        indexed.
    """

    repo = configparser.ConfigParser(default_section='', interpolation=None)
    repo.add_section(repo_name)
    repo_section = repo[repo_name]
    repo_section['name'] = repo_name
    repo_section['description'] = 'Test artifacts repository'
    repo_section['baseurl'] = f'file://{urllib.parse.quote(repo_path)}'
    repo_section['priority'] = '1'
    repo_section['enabled'] = '1'
    repo_section['gpgcheck'] = '0'

    repo_str = io.StringIO()
    repo.write(repo_str, space_around_delimiters=False)

    pkglist_path = package_list_path(pkglist, basepath=repo_path)

    sut_installation.add_step('Create repository', (
        f'yum install -y createrepo && cd {shlex.quote(repo_path)} && touch {shlex.quote(str(pkglist_path))} && '
        f'createrepo --pkglist {shlex.quote(str(pkglist_path))} .'
    ))
    sut_installation.add_step(
        'Add repository', f'echo -e "{repo_str.getvalue()}" > /etc/yum.repos.d/{shlex.quote(repo_name)}.repo')
