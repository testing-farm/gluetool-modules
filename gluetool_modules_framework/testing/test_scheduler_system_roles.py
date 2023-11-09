# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import tempfile
import tarfile

import gluetool
from gluetool.utils import normalize_path, load_yaml
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.test_schedule import TestSchedule
from gluetool_modules_framework.testing.test_scheduler_sti import TestScheduleEntry

# Type annotations
from typing import Optional, List, Any, Tuple, Set, cast, Dict  # noqa


class TestSchedulerSystemRoles(gluetool.Module):

    name = 'test-scheduler-system-roles'
    description = 'Prepare schedule for system roles testing. Modify entries provided by previous (STI) provider.'

    options = {
        'ansible-playbook-filepath': {
            'help': """
                    Provide different ansible-playbook executable to the call
                    of a `run_playbook` shared function. (default: %(default)s)
                    """,
            'metavar': 'PATH',
            'type': str,
            'default': ''
        },
        'collection': {
            'help': 'Run test using converted collection',
            'action': 'store_true'
        },
        'collection-namespace': {
            'help': 'Namespace to use for converted collection (default: %(default)s)',
            'type': str,
            'default': 'fedora'
        },
        'collection-name': {
            'help': 'Name to use for converted collection (default: %(default)s)',
            'type': str,
            'default': 'linux_system_roles'
        },
        'collection-script-url':  {
            'help': 'Location of conversion script (default: %(default)s)',
            'type': str,
            'default': 'https://raw.githubusercontent.com/linux-system-roles/auto-maintenance/master'
        },
        'vault-pwd-file': {
            'help': """
                    Name of vault password file (if not absolute path, relative to tests directory)
                    (default: %(default)s)
                    """,
            'type': str,
            'default': 'vault_pwd'
        },
        'vault-variables-file': {
            'help': """
                    Name of vault variables file (if not absolute path, relative to tests directory)
                    (default: %(default)s)
                    """,
            'type': str,
            'default': 'vars/vault-variables.yml'
        },
        'vault-no-variables-file': {
            'help': """
                    Name of file listing tests not to use vault (if not absolute path, relative to tests directory)
                    (default: %(default)s)
                    """,
            'type': str,
            'default': 'no-vault-variables.txt'
        }
    }

    shared_functions = ['create_test_schedule']

    def _convert_to_collection(self, ansible_environment: Dict[str, str]) -> None:
        """
        Convert the role to collection format.
        """
        repo_path = self.shared('dist_git_repository').path
        role = self.shared('primary_task').component
        base_url = self.option('collection-script-url')
        lsr_coll_tmp = None
        coll_namespace = self.option('collection-namespace')
        coll_name = self.option('collection-name')
        lsr_role2coll_path = os.path.join(repo_path, 'lsr_role2collection.py')
        lsr_runtime_path = os.path.join(repo_path, 'runtime.yml')
        with gluetool.utils.requests() as request:
            response = request.get(base_url + '/lsr_role2collection.py')
            with open(lsr_role2coll_path, 'w') as writer:
                writer.write(response.text)
            response = request.get(base_url + '/lsr_role2collection/runtime.yml')
            with open(lsr_runtime_path, 'w') as writer:
                writer.write(response.text)
        try:
            lsr_coll_tmp = tempfile.mkdtemp(prefix='lsr_', suffix='_coll')
            cmd = ['python3', lsr_role2coll_path, '--src-owner', 'linux-system-roles',
                   '--role', role, '--src-path', repo_path, '--dest-path', lsr_coll_tmp,
                   '--namespace', coll_namespace, '--collection', coll_name,
                   '--subrole-prefix', 'private_' + role + '_subrole_',
                   '--meta-runtime', lsr_runtime_path]
            gluetool.utils.Command(cmd).run()
            # remove the old tests directory
            shutil.rmtree(os.path.join(repo_path, 'tests'))
            # Move the converted collection
            coll_path = os.path.join(repo_path, '.collection')
            if not os.path.isdir(coll_path):
                shutil.move(lsr_coll_tmp, coll_path)
            else:
                tar_file = os.path.join(repo_path, '.collection.tar')
                _cwd = os.getcwd()
                with tarfile.open(tar_file, "w") as _tar:
                    os.chdir(lsr_coll_tmp)
                    dlist = os.listdir(lsr_coll_tmp)

                    exclude_files = ["ansible_collections/{}/{}/.collection".format(coll_namespace, coll_name)]

                    def exclude_function(tarinfo: Any) -> Optional[Any]:
                        filename = tarinfo.name
                        if filename in exclude_files or os.path.splitext(filename)[1] in exclude_files:
                            return None
                        else:
                            return tarinfo

                    for _item in dlist:
                        _tar.add(_item, filter=exclude_function)
                    os.chdir(_cwd)
                with tarfile.open(tar_file, "r") as _tar:
                    os.chdir(coll_path)

                    # Workaround for CVE-2007-4559
                    # See https://github.com/testing-farm/gluetool-modules/pull/1

                    def is_within_directory(directory: str, target: str) -> bool:

                        abs_directory = os.path.abspath(directory)
                        abs_target = os.path.abspath(target)

                        prefix = os.path.commonprefix([abs_directory, abs_target])

                        result = prefix == abs_directory
                        return result

                    def safe_extract(tar: tarfile.TarFile,
                                     path: str = ".",
                                     members: Optional[List[tarfile.TarInfo]] = None) -> None:

                        for member in tar.getmembers():
                            member_path = os.path.join(path, member.name)
                            if not is_within_directory(path, member_path):
                                raise gluetool.GlueError("Attempted Path Traversal in Tar File")

                        tar.extractall(path, members)

                    safe_extract(_tar)

                    os.chdir(_cwd)
            # Move the converted tests
            os.rename(os.path.join(coll_path, 'ansible_collections', coll_namespace, coll_name, 'tests', role),
                      os.path.join(repo_path, 'tests'))
            ansible_environment['ANSIBLE_COLLECTIONS_PATHS'] = coll_path
        except Exception as exc:
            if lsr_coll_tmp:
                shutil.rmtree(lsr_coll_tmp)
            raise gluetool.GlueError('Converting of role to collection failed with {}'.format(exc))

    def _install_requirements(self, ansible_environment: Dict[str, str]) -> None:
        """
        If collection-requirements.yml contains the collections, install reqs
        from meta/collection-requirements.yml at repo_path/.collection.
        Also install test requirements from tests/collection-requirements.yml
        """
        self.info('Trying to install requirements.')

        repo_path = self.shared('dist_git_repository').path
        ansible_path = os.path.dirname(self.option('ansible-playbook-filepath'))

        collection_path = os.path.join(repo_path, '.collection')
        if not os.path.isdir(collection_path):
            os.mkdir(collection_path)

        requirements_filepath = os.path.join(repo_path, "meta", "collection-requirements.yml")
        test_requirements_filepath = os.path.join(repo_path, "tests", "collection-requirements.yml")

        # see if reqfile is in legacy role format
        for req_file in [requirements_filepath, test_requirements_filepath]:
            if os.path.isfile(req_file):
                self.info('The {} requirements file was found'.format(req_file))
                cmd = [
                    "{}/ansible-galaxy".format(ansible_path),
                    "collection",
                    "install",
                    "--force",
                    "-p",
                    collection_path,
                    "-vv",
                    "-r",
                    req_file
                ]
                try:
                    gluetool.utils.Command(cmd).run()
                    self.info('Requirements were successfully installed from {}'.format(req_file))
                except gluetool.GlueCommandError as exc:
                    raise gluetool.GlueError("ansible-galaxy failed with: {}".format(exc))

                # Check if the collection(s) are installed or not.
                requirements_yaml = load_yaml(req_file)

                for collection in requirements_yaml['collections']:
                    if isinstance(collection, dict):
                        collection_name = collection['name']
                    else:
                        collection_name = collection

                    collection_dir = os.path.join(
                        collection_path,
                        "ansible_collections",
                        collection_name.replace('.', '/')
                    )

                    if not os.path.isdir(collection_dir):
                        raise gluetool.GlueError("{} is not installed at {}".format(collection_name, collection_dir))

                # Set collection_path to ANSIBLE_COLLECTIONS_PATHS
                ansible_environment['ANSIBLE_COLLECTIONS_PATHS'] = collection_path

    # Returns a Set of the basenames of test playbooks that we do not want
    # to provide vault variables for
    def get_no_vault_tests(self, repo_path: str) -> Set[str]:
        no_vault_file = self.option('vault-no-variables-file')
        if not os.path.isabs(no_vault_file):
            no_vault_file = os.path.join(repo_path, 'tests', no_vault_file)
        if os.path.exists(no_vault_file):
            no_vault_tests = set([playbook.strip() for playbook in open(no_vault_file).readlines()])
        else:
            no_vault_tests = set()
        return no_vault_tests

    def _setup_for_vault(self, ansible_environment: Dict[str, str]) -> Tuple[bool, Set[str], str]:
        repo_path = self.shared('dist_git_repository').path
        vault_pwd_file = self.option('vault-pwd-file')
        if not os.path.isabs(vault_pwd_file):
            vault_pwd_file = os.path.join(repo_path, 'tests', vault_pwd_file)
        vault_variables_file = self.option('vault-variables-file')
        if not os.path.isabs(vault_variables_file):
            vault_variables_file = os.path.join(repo_path, 'tests', vault_variables_file)
        if os.path.exists(vault_pwd_file) and os.path.exists(vault_variables_file):
            ansible_environment['ANSIBLE_VAULT_PASSWORD_FILE'] = vault_pwd_file
            uses_vault = True
            no_vault_tests = self.get_no_vault_tests(repo_path)
        else:
            uses_vault = False
            no_vault_tests = set()  # empty
        return (uses_vault, no_vault_tests, vault_variables_file)

    def _fix_playbook_for_vault(self, playbook_filepath: str, vault_variables_file: str) -> None:
        with open(playbook_filepath) as pbf:
            playbook = pbf.read()
            playbook = playbook.replace('---\n', '')
        with open(playbook_filepath, 'w') as pbf:
            pbf.write('''- hosts: all
  gather_facts: false
  tasks:
    - name: Include vault variables
      include_vars:
        file: {}

{}
'''.format(vault_variables_file, playbook))

    def create_test_schedule(
        self,
        testing_environment_constraints: Optional[List[TestingEnvironment]] = None
    ) -> TestSchedule:
        """
        This module modifies STI test schedule provided by other module. It adds provided ansible playbook filepath
        to schedule entries.
        """

        schedule: TestSchedule = self.overloaded_shared(
            'create_test_schedule', testing_environment_constraints=testing_environment_constraints
        )

        ansible_environment: Dict[str, str] = {}
        uses_vault, no_vault_tests, vault_variables_file = self._setup_for_vault(ansible_environment)
        if self.option('ansible-playbook-filepath') or uses_vault:
            for entry in schedule:

                if entry.runner_capability != 'sti':
                    continue

                assert isinstance(entry, TestScheduleEntry)
                if self.option('ansible-playbook-filepath'):
                    entry.ansible_playbook_filepath = normalize_path(self.option('ansible-playbook-filepath'))
                if uses_vault:
                    test_pb_file = os.path.basename(entry.playbook_filepath)
                    if test_pb_file not in no_vault_tests:
                        self._fix_playbook_for_vault(entry.playbook_filepath, vault_variables_file)

            # Install collections from ansible-galaxy if specified in collection-requirements.yml
            if self.option('ansible-playbook-filepath'):
                self._install_requirements(ansible_environment)

        # If linux_system_roles collection is already installed from ansible-galaxy,
        # the being-tested role is overwritten by this conversion.
        if self.option('collection'):
            self._convert_to_collection(ansible_environment)

        # update schedule entry with ansible_environment
        if ansible_environment:
            for entry in schedule:

                if entry.runner_capability != 'sti':
                    continue

                assert isinstance(entry, TestScheduleEntry)
                entry.ansible_environment = ansible_environment

        return schedule
