# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
from datetime import datetime

import gluetool
from gluetool import Failure
from gluetool import GlueCommandError
from gluetool import GlueError
from gluetool.utils import Command
from gluetool_modules_framework.libs.test_schedule import TestScheduleEntryState

from typing import AnyStr, List, Optional, Dict, Any, cast # noqa


SUMMARY_PAGE_TEMPLATE = "<html>\n<head>\n</head>\n<body>{}\n</body>\n</html>"


class UploadResults(gluetool.Module):
    """
    This module is for uploading test results in linux-system-roles BaseOS CI use-case.
    It does not provide generic functionality for gluetool-module.

    It is used at the end of the citool pipeline.

    It uses entries in ``test_schedule`` as a source of artifacts.
    It provides ``PR_TESTING_ARTIFACTS_URL`` as the target of uploaded results on the web.
    """

    name = 'upload-results'
    description = 'Upload result using scp'

    supported_dryrun_level = gluetool.glue.DryRunLevels.DRY

    options = {
        'artifact-src-filenames': {
            'help': 'The filenames of source artifacts we want to upload',
            'metavar': 'path',
            'type': str
        },
        'artifact-dest-file-postfix': {
            'help': 'The postfix in the end of the uploaded test results filename.',
            'metavar': 'path',
            'type': str
        },
        'artifact-target-dir-name': {
            'help': 'The name of a directory for artifacts in `target-dir`',
            'metavar': 'path',
            'type': str
        },
        'artifact-target-subdirs': {
            'help': 'The subdirectories in `target-dir`/`artifact_target-dir-name` where to upload results. Optional',
            'metavar': 'path',
            'type': str
        },
        'key-path': {
            'help': 'the path to the key which will be used to upload',
            'metavar': 'path',
            'type': str
        },
        'upload-to-public': {
            'help': 'Uploads results to public space if set',
            'action': 'store_true'
        },
        'user': {
            'help': 'The user which will be used by scp to log in target host',
            'metavar': 'USER',
            'type': str
        },
        'domain': {
            'help': 'The domain to which results will be uploaded',
            'metavar': 'URL',
            'type': str
        },
        'download-domain': {
            'help': 'The domain from which results will be downloaded',
            'metavar': 'DOWNLOADURL',
            'type': str,
        },
        'target-url': {
            'help': 'The URL to which results will be uploaded',
            'metavar': 'URL',
            'type': str
        },
        'target-dir': {
            'help': 'The directory in target host where artifacts will be uploaded',
            'metavar': 'PATH',
            'type': str
        },
        'create-summary-page': {
            'help': 'Create summary page with tests summary if set',
            'action': 'store_true'
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(UploadResults, self).__init__(*args, **kwargs)
        self.full_target_url: Optional[str] = None

    def _get_pull_request_info(self) -> str:
        """
        It generates a string from pull request information.

        :rtype: str
        :returns: Formated pull request info.
        """
        task = self.shared('primary_task')
        return "{}-{}-{}".format(task.repo, task.pull_number, task.commit_sha[0:7])

    def _get_artifact_dir_name(self) -> str:
        """
        It generates a name for the results folder.

        :rtype: str
        :returns: The name of the folder where the results will be uploaded
        """
        compose = self.shared('compose')
        if isinstance(compose, List):
            compose = compose[0]

        artifact_folder_name = self.option('artifact-target-dir-name').format(
            self._get_pull_request_info(),
            compose,
            datetime.now().strftime('%Y%m%d-%H%M%S')
        )
        return cast(str, artifact_folder_name)

    def _create_subdir_for_artifacts(self, destination_sub_path: str, user_and_domain: str) -> Optional[str]:
        """
        This will create a folder for the results on the target file hosting.

        :param str destination_sub_path: Main destination path in filesystem for results.
        :param str user_and_domain: User login to the server.
        """
        target_subdirectory = self.option('artifact-target-subdirs')
        if target_subdirectory:
            destination_sub_path = "{}/{}".format(destination_sub_path, target_subdirectory)
            target_dir = self.option('target-dir')
            cmd_init_remote_dir = [
                'ssh', '-i', self.option('key-path'),
                user_and_domain,
                "mkdir -p {}".format(os.path.join(target_dir, destination_sub_path))
            ]
            try:
                Command(cmd_init_remote_dir).run()
                return destination_sub_path
            except GlueCommandError as exc:
                assert exc.output.stderr is not None
                raise GlueError('Creating remote folder failed: {} cmd: {}'.format(exc, cmd_init_remote_dir))

        return None

    def _get_files_to_upload(self) -> List[Dict[str, str]]:
        """
        Get the results to be uploaded to the server.

        :returns: The source paths to the test results and destination filenames.
        """
        schedule = self.shared('test_schedule')
        dest_file_postfix = self.option('artifact-dest-file-postfix')

        files = []
        for entry in schedule:

            # If entry errored, there is no files to upload
            if entry.state == TestScheduleEntryState.ERROR:
                continue

            dest_filename = "{}-{}{}".format(
                os.path.splitext(
                    entry.playbook_filepath.split('/')[-1]
                )[0],
                entry.result,
                dest_file_postfix
            )

            files.append({
                'src-file-path': os.path.join(entry.work_dirpath, self.option('artifact-src-filenames')),
                'dest-filename': dest_filename,
                'dest-url': '{}/{}'.format(self.full_target_url, dest_filename),
                'result': entry.result
            })

        return files

    def _upload_results(self,
                        destination_path: str,
                        user_and_domain: str,
                        results_files: List[Dict[str, str]]) -> None:
        """
        It uploads the artifacts to the server.

        :param str destination_path: Where to upload results. Example: ``/data/logs/result1/``
        :param str user_and_domain: User login to the server. Example: ``root@domain.com``
        :param dict results_files: Full paths to the source artifacts and destination filenames.
        """
        for results_file in results_files:
            cmd_upload: Optional[List[str]] = ['scp', '-i', cast(str, self.option('key-path'))]
            assert cmd_upload is not None

            cmd_upload.append(results_file['src-file-path'])

            cmd_upload.append('{}:{}'.format(
                user_and_domain,
                os.path.join(destination_path, results_file['dest-filename'])
            ))

            try:
                Command(cmd_upload).run()
                cmd_upload = None
            except GlueCommandError as exc:
                assert exc.output.stderr is not None
                raise GlueError('Uploading results failed: {} cmd: {}'.format(exc, cmd_upload))

    def _create_summary_file(self, results_files: List[Dict[str, str]]) -> Dict[str, str]:
        """
        Creates summary html with results summary
        """
        summary_page = SUMMARY_PAGE_TEMPLATE
        body = ""

        primary_task = self.shared('primary_task')
        if primary_task:
            body += '<h>Pull request - {}</h>\n<h>Tested on {}</h>'.format(
                primary_task.html_url,
                self.shared('compose')[0]
            )

        for results_file in results_files:
            line = '<p> Testing: <a href={}>{}</a> - {}</p>\n'.format(
                results_file['dest-url'],
                results_file['dest-filename'],
                results_file['result']
            )
            body += line

        # Check for errored entries and mention them in summary page
        schedule = self.shared('test_schedule')
        for entry in schedule:
            if entry.state == TestScheduleEntryState.ERROR:
                line = '<p> Testing error: {}</p>\n'.format(
                    os.path.splitext(entry.playbook_filepath.split('/')[-1])[0]
                )
                body += line

        body += '\n<a href={}>INDEX</a>\n'.format(self.full_target_url)
        summary_page = summary_page.format(body)

        with open('summary.html', 'w') as f:
            f.write(summary_page)

        return {
            'src-file-path': 'summary.html',
            'dest-filename': 'summary.html',
        }

    @property
    def _full_target_url(self) -> Optional[str]:
        return self.full_target_url

    @property
    def eval_context(self) -> Dict[str, Optional[str]]:
        __content__ = { # noqa
            'PR_TESTING_ARTIFACTS_URL': """
                          The URL with results of testing
                          """
        }
        return {
            'PR_TESTING_ARTIFACTS_URL': '{}/summary.html'.format(self._full_target_url)
        }

    def destroy(self, failure: Optional[Failure] = None) -> None:
        """
        It creates a directory for results in destination and then it uploads test results.
        At the end ``PR_TESTING_ARTIFACTS_URL`` contains the URL with the uploaded results.

        :param gluetool.glue.Failure failure: if set, carries information about failure that made
          ``gluetool`` to destroy the whole session. Modules might want to take actions based
          on provided information, e.g. send different notifications.
        """
        self.require_shared('test_schedule', 'compose', 'primary_task')

        # If failure and no test schedule, there is nothing to upload
        if failure and not self.shared('test_schedule'):
            return

        if not self.shared('test_schedule'):
            # Probably cloning failed
            self.warn('Nothing to upload')
            return

        if not self.option('upload-to-public'):
            return

        domain = self.option('domain')
        user = self.option('user')
        user_and_domain = "{}@{}".format(user, domain)

        destination_sub_path = self._get_artifact_dir_name()

        subdir = self._create_subdir_for_artifacts(destination_sub_path, user_and_domain)
        assert subdir is not None
        destination_sub_path = subdir

        target_url = self.option('target-url')
        self.destination_url = os.path.join(target_url, destination_sub_path)

        target_dir = self.option('target-dir')
        self.destination_dir = os.path.join(target_dir, destination_sub_path)

        # Return artifacts URL
        download_domain = self.option('download-domain') or domain
        self.full_target_url = "https://{}/{}".format(download_domain, self.destination_url)

        results_files = self._get_files_to_upload()

        if self.option('create-summary-page'):
            self.info('creating summary page')
            summary_file = self._create_summary_file(results_files)

            results_files.append(summary_file)
        self._upload_results(self.destination_dir, user_and_domain, results_files)
