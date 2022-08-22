# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import tempfile
import tarfile

import gluetool
from gluetool.utils import normalize_path, Command
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.test_schedule import TestSchedule
from gluetool_modules_framework.testing.test_schedule_runner_sti import TestScheduleEntry

# Type annotations
from typing import Optional, List, cast  # noqa


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
        'collection_namespace': {
            'help': 'Namespace to use for converted collection (default: %(default)s)',
            'type': str,
            'default': 'fedora'
        },
        'collection_name': {
            'help': 'Name to use for converted collection (default: %(default)s)',
            'type': str,
            'default': 'linux_system_roles'
        },
        'collection_script_url':  {
            'help': 'Location of conversion script (default: %(default)s)',
            'type': str,
            'default': 'https://raw.githubusercontent.com/linux-system-roles/auto-maintenance/master'
        }
    }

    shared_functions = ['create_test_schedule']

    def _convert_to_collection(self):
        # type: () -> None
        """
        Convert the role to collection format.
        """
        repo_path = self.shared('dist_git_repository').path
        role = self.shared('primary_task').component
        base_url = self.option('collection_script_url')
        lsr_coll_tmp = None
        coll_namespace = self.option('collection_namespace')
        coll_name = self.option('collection_name')
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

                    def exclude_function(tarinfo):
                        # type: (Any) -> Optional[Any]
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
                    _tar.extractall()
                    os.chdir(_cwd)
            # Move the converted tests
            os.rename(os.path.join(coll_path, 'ansible_collections', coll_namespace, coll_name, 'tests', role),
                      os.path.join(repo_path, 'tests'))
            os.environ['ANSIBLE_COLLECTIONS_PATHS'] = coll_path
        except Exception as exc:
            if lsr_coll_tmp:
                shutil.rmtree(lsr_coll_tmp)
            raise gluetool.GlueError('Converting of role to collection failed with {}'.format(exc))

    def _install_requirements(self, ansible_path, logger):
        # type: (str, gluetool.log.ContextAdapter) -> None
        """
        If collection-requirements.yml contains the collections, install reqs
        from meta/collection-requirements.yml at repo_path/.collection.
        """
        repo_path = self.shared('dist_git_repository').path
        collection_path = os.path.join(repo_path, '.collection')
        if not os.path.isdir(collection_path):
            os.mkdir(collection_path)
        reqfile = os.path.join(repo_path, "meta", "collection-requirements.yml")
        # see if reqfile is in legacy role format
        if os.path.isfile(reqfile):
            cmd = [
                "{}/ansible-galaxy".format(ansible_path),
                "collection",
                "install",
                "-p",
                collection_path,
                "-vv",
                "-r",
                reqfile
            ]
            try:
                Command(cmd, logger=logger).run()
            except gluetool.GlueCommandError as exc:
                raise gluetool.GlueError("ansible-galaxy failed with {}".format(exc))

            # Check if the collection(s) are installed or not.
            import yaml
            with open(reqfile) as rqff:
                obj = yaml.safe_load(rqff)
                for coll in obj['collections']:
                    if isinstance(coll, dict):
                        name_coll = coll['name']
                    else:
                        name_coll = coll
                    colldir = os.path.join(
                        collection_path,
                        "ansible_collections",
                        name_coll.replace('.', '/')
                    )
                    if not os.path.isdir(colldir):
                        raise gluetool.GlueError("{} is not installed at {}".format(name_coll, colldir))

            # Set collection_path to ANSIBLE_COLLECTIONS_PATHS
            os.environ['ANSIBLE_COLLECTIONS_PATHS'] = collection_path

    def create_test_schedule(self, testing_environment_constraints=None):
        # type: (Optional[List[TestingEnvironment]]) -> TestSchedule
        """
        This module modifies STI test schedule provided by other module. It adds provided ansible playbook filepath
        to schedule entries.
        """

        schedule = self.overloaded_shared(
            'create_test_schedule', testing_environment_constraints=testing_environment_constraints
        )  # type: TestSchedule

        _logger = None
        _ansible_path = None
        if self.option('ansible-playbook-filepath'):
            for entry in schedule:

                if entry.runner_capability != 'sti':
                    continue

                assert isinstance(entry, TestScheduleEntry)
                entry.ansible_playbook_filepath = normalize_path(self.option('ansible-playbook-filepath'))
                _logger = entry.logger
                _ansible_path = os.path.dirname(entry.ansible_playbook_filepath)

        if not _ansible_path:
            raise gluetool.GlueError("ansible-playbook-filepath is not specified.")
        # Install collections from ansible-galaxy if specified in collection-requirements.yml
        self._install_requirements(_ansible_path, _logger)

        # If linux_system_roles collection is already installed from ansible-galaxy,
        # the being-tested role is overwritten by this conversion.
        if self.option('collection'):
            self._convert_to_collection()

        return schedule
