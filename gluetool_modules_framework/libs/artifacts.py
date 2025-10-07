# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from os import PathLike
from pathlib import Path

import gluetool

# Type annotations
from typing import TYPE_CHECKING, cast, Any, List, Optional, Tuple, Union  # noqa

if TYPE_CHECKING:
    from gluetool.log import ContextAdapter  # noqa


DEFAULT_DOWNLOAD_PATH = '/var/share/test-artifacts'
DEFAULT_PACKAGE_LIST = 'pkglist'


class NoArtifactsError(gluetool.glue.SoftGlueError):
    """
    Raised when the artifact (e.g. Brew task or MBS build) contain no artifacts anymore.
    This can - and does - happen in case of scratch builds: only the record the build
    was performed stays in a build system database, and its artifacts (RPMs, logs, etc.)
    are removed to save the space.

    :param task_id: ID of the task without artifacts.
    """

    def __init__(self, task_id: Any) -> None:

        super(NoArtifactsError, self).__init__('No artifacts found for task')

        self.task_id = task_id


def has_artifacts(*tasks: Any) -> None:
    """
    Check whether tasks have artifacts, any artifacts at all - no constraints like architecture are imposed,
    we're not trying to check whether the artifacts are testable with environments we have at our disposal.

    :param tasks: list of tasks to check.
    :raises: :py:class:`NoArtifactsError` if any task has no artifacts.
    """

    for task in tasks:
        if not task.has_artifacts:
            raise NoArtifactsError(task.id)


def artifacts_location(module: gluetool.Module, local_path: str, logger: Optional['ContextAdapter'] = None) -> str:
    """
    If we have access to ``artifacts_location`` shared function, return its output. Otherwise, return
    the input string.

    The goal si to simplify the code when``artifacts_location`` shared function is not available.
    """

    if module.has_shared('artifacts_location'):
        return cast(
            str,
            module.shared('artifacts_location', local_path, logger=logger)
        )

    return local_path


def package_list_path(pkglist: Union[str, PathLike[str]] = DEFAULT_PACKAGE_LIST, *,
                      basepath: Optional[Union[str, PathLike[str]]] = None) -> PathLike[str]:
    """
    Helper to resolve package list path.

    :param pkglist: Custom package list path.
    :param basepath: Optional path to prepend to the package list path if it is relative.
    :returns: Path object of the package list.
    """

    filepath = Path(pkglist)

    if not filepath.is_absolute() and basepath is not None:
        filepath = Path(basepath) / filepath

    return filepath


def packages_download_cmd(download_path: str, rpm_urls: Optional[List[str]] = None,
                          rpm_urls_file: Optional[str] = None, *, pkglist: str = DEFAULT_PACKAGE_LIST) -> str:
    """
    Helper to generate a command to download package files to a directory.

    :param download_path: Path of the target directory.
    :param rpm_urls: List of RPM URLs to fetch.
    :param rpm_urls_file: Path to a file on the target containing the list of URLs to download.
    :patam pkglist: Optional override for the location of the list of downloaded packages.
    :raises :py:class:`ValueError` if incorrect arguments are passed in.
    """

    if (rpm_urls is None) is (rpm_urls_file is None):
        raise ValueError("Exactly one of 'rpm_urls' or 'rpm_urls_file' is required.")

    # Base curl command, which outputs the downloaded filename to a file
    curl_cmd = 'curl -sL --retry 5 --remote-name-all -w "%{http_code} %{url_effective} %{filename_effective}\\n"'

    if rpm_urls is not None:
        curl_cmd = '{} {}'.format(curl_cmd, ' '.join(rpm_urls))
    else:
        curl_cmd = 'cat {} | xargs -n1 {}'.format(rpm_urls_file, curl_cmd)

    return (
        'cd {} && {} | awk -v pkglist="{}" \'{{if ($1 == "200") {{print "Downloaded:", $2; print $3 >> pkglist}}}}\''
    ).format(download_path, curl_cmd, package_list_path(pkglist))


# With python3 we can use `Subject` from `dnf` package
# see https://bugzilla.redhat.com/show_bug.cgi?id=1452801#c7

def splitFilename(filename: str) -> Tuple[str, ...]:
    """
    Split N(E)VRA to its pieces

    :param nevra to split
    :returns: a name, version, release, epoch, arch

    Code taken from rpmUtils.miscutils.splitFilename,
    which is unavailable in Fedora 31.
    Original code modified to accept N(E)VRA instead (E)NVRA
    """
    if filename[-4:] == '.rpm':
        filename = filename[:-4]

    archIndex = filename.rfind('.')
    arch = filename[archIndex+1:]

    relIndex = filename[:archIndex].rfind('-')
    rel = filename[relIndex+1:archIndex]

    verIndex = filename[:relIndex].rfind('-')
    ver = filename[verIndex+1:relIndex]

    epochIndex = ver.find(':')
    if epochIndex == -1:
        epoch = ''
    else:
        epoch = ver[:epochIndex]
        ver = ver[epochIndex+1:]

    name = filename[:verIndex]

    return name, ver, rel, epoch, arch
