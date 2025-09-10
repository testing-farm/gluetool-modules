# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import configparser
import io
import shlex
import urllib.parse

from gluetool_modules_framework.libs.sut_installation import SUTInstallation


def create_repo(sut_installation: SUTInstallation, repo_name: str, repo_path: str) -> None:
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

    sut_installation.add_step('Create repository', f'yum install -y createrepo; createrepo {shlex.quote(repo_path)}')
    sut_installation.add_step(
        'Add repository', f'echo -e "{repo_str.getvalue()}" > /etc/yum.repos.d/{shlex.quote(repo_name)}.repo')
