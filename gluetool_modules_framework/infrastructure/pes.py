# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from six.moves.urllib.parse import urljoin
import simplejson.errors  # type: ignore  # no stubfile for simplejson

from requests.exceptions import ConnectionError, HTTPError, Timeout

import gluetool
from gluetool.utils import cached_property, requests
from gluetool.log import LoggerMixin, log_dict
from gluetool.result import Result

from jq import jq

# Type annotations
from typing import TYPE_CHECKING, cast, Any, Dict, List, Optional, Tuple, Union  # noqa
import requests as orig_requests  # noqa


DEFAULT_RETRY_TIMEOUT = 30
DEFAULT_RETRY_TICK = 10


class PESApi(LoggerMixin, object):
    """
    API to Package Evolution Service
    """

    def __init__(self, module: gluetool.Module) -> None:

        super(PESApi, self).__init__(module.logger)

        self.api_url: str = module.option('api-url')
        self.module: gluetool.Module = module

    def _request_with_payload(self, method: str, location: str, payload: Dict[str, Any]) -> orig_requests.Response:
        url = urljoin(self.api_url, location)

        self.debug('[PES API]: {}'.format(url))

        def _request_response() -> Result[orig_requests.Response, Exception]:
            try:
                with requests() as req:
                    if method == 'post':
                        response = req.post(url, json=payload, verify=False)
                    elif method == 'get':
                        response = req.get(url, params=payload, verify=False)
                    else:
                        raise gluetool.GlueError("Unsupported method '{}'".format(method))

                # 404 is expected if no events were found for a component
                if response.status_code not in [200, 404]:
                    raise gluetool.GlueError(
                        "{} with payload '{}' to '{}' returned {}: {}".format(
                            method,
                            payload,
                            url,
                            response.status_code,
                            response.content)
                    )

                # show nice parsed output
                try:
                    log_dict(self.debug,
                             "[PES API] returned '{}' and following output".format(response.status_code),
                             response.json())

                # in case json decoding fails for the response, something is really wrong (e.g. wrong api-url)
                except simplejson.errors.JSONDecodeError:
                    raise gluetool.GlueError("Pes returned unexpected non-json output, needs investigation")

                return Result.Ok(response)

            except (ConnectionError, HTTPError, Timeout) as error:
                return Result.Error(error)

            return Result.Error('unknown error')

        # Wait until we get a valid response. For 200 or 404, we get valid result, for anything else _request_response
        # returns invalid result, forcing another attempt.
        return gluetool.utils.wait('getting {} response from {}'.format(method, url),
                                   _request_response,
                                   timeout=self.module.option('retry-timeout'),
                                   tick=self.module.option('retry-tick'))

    def get_ancestor_components(self, component: str, release: str) -> List[str]:
        """
        Get ancestor components of the given component from given major release by querying Package Evolution Service.
        This can be used for testing upgrades from the ancestor package(s) to the given component.

        :returns: List of ancestor components of the component.
        """

        # Note: srpm-events endpoint MUST end with /
        response = self._request_with_payload('post', 'srpm-events/', {'name': component, 'release': release})

        # When no entries are found empty list is returned.
        # We can assume component has not changed between releases, but rather no guessing in this step.
        # Consumers of this function can guess the ancestor, or try to find them some other way.
        if response.status_code == 404:
            return []

        # Note state presence actually means two thing:
        #
        # 1. package was present in previous release
        # 2. it is a new package, previously not present in previous release
        #
        # The case 2. needs to be handled later in the pipeline, i.e. not existing ancestor build

        query = '.[] | .in_packageset.srpm | .[]'

        ancestors = jq(query).transform(response.json(), multiple_output=True)

        # remove duplicate ancestors and sort them, so their list is predictable
        return sorted(set(ancestors))

    def get_component_rpms(self, component: str, release: str, architectures: List[str]) -> List[str]:
        """
        Get binary rpms built from component by querying Package Evolution Service.

        :param str component: Component to find rpms for.
        :param str release: Version of targeted system in a RHEL X.Y format.
        :param List[str] architectures: Architectures of the rpms.
        """

        response = self._request_with_payload('post', 'rpmmap/', {'name': component,
                                                                  'release': release,
                                                                  'architecture': architectures})

        # Return empty list when no rpms are found.
        if response.status_code == 404:
            return list()

        return sorted(rpm['name'] for rpm in response.json()['rpms'])

    def get_successor_components(self, component: str, initial_release: str, release: str) -> List[str]:
        """
        Get successor components of the given component by querying Package Evolution Service. This can be used
        for testing upgrades from given component to the the successor component(s).

        :returns: List of successor components of the component.
        """

        payload = {'srpm': component, 'initial_release': initial_release, 'release': release}
        response = self._request_with_payload('get', 'successors/', payload)

        # When no entries are found empty list is returned.
        # We can assume component has not changed between releases, but rather no guessing in this step.
        # Consumers of this function can guess the ancestor, or try to find them some other way.
        if response.status_code == 404:
            return []

        successors = response.json().keys()

        # remove duplicate ancestors and sort them, so their list is predictable
        return sorted(set(successors))


class PES(gluetool.Module):
    """
    Provides API to Package Evolution Service via `pes_api` shared function.
    Provides functions to find ancestors and successors of a given component. Used for upgrades testing.
    """
    name = 'pes'
    description = 'Provides API to Package Evolution Service (PES)'

    options = [
        ('General options', {
            'api-url': {
                'help': 'PES API server URL',
                'type': str
            },
        }),
        ('Query options', {
            'retry-timeout': {
                'help': 'Wait timeout in seconds. (default: %(default)s)',
                'type': int,
                'default': DEFAULT_RETRY_TIMEOUT
            },
            'retry-tick': {
                'help': 'Number of times to retry the query. (default: %(default)s)',
                'type': int,
                'default': DEFAULT_RETRY_TICK
            },
        }),
    ]

    required_options = ('api-url',)

    shared_functions = ['ancestor_components', 'successor_components', 'pes_api', 'component_rpms']

    def __init__(self, *args: Any, **kwargs: Any) -> None:

        super(PES, self).__init__(*args, **kwargs)

        self._components: List[str] = []

    @cached_property
    def _pes_api(self) -> PESApi:
        return PESApi(self)

    def pes_api(self) -> PESApi:
        """
        Returns PESApi instance.
        """
        return cast(PESApi, self._pes_api)

    def ancestor_components(self, component: str, target_release: str) -> List[str]:
        """
        Return list of ancestor components of a specified component from specified major target release.

        :param str component: Component to find ancestors for.
        :param str target_release: Target release in a 'RHEL X' format. Anything after that substring is ignored.
        """
        ancestors = cast(List[str], self._pes_api.get_ancestor_components(component, target_release))
        ancestors.sort()

        log_dict(self.info,
                 "Ancestors of component '{}' from target release '{}'".format(component, target_release),
                 ancestors)

        return ancestors

    def component_rpms(self, component: str, release: str, architectures: List[str]) -> List[str]:
        """
        Return list of binary rpms built from component in specified release and architectures.

        :param str component: Component to find rpms for.
        :param str release: Release in a 'RHEL X[.Y]' format (Y is assumend to be 0 if missing) where to look form rpms.
        :param List[str] architectures: Allowed architectures of the rpms.
        """

        rpms = cast(List[str], self._pes_api.get_component_rpms(component, release, architectures))
        rpms.sort()

        log_dict(self.info,
                 "Binary rpms of component '{}' built in release '{}' for architectures '{}'".format(
                     component, release, ', '.join(architectures)),
                 rpms)

        return rpms

    def successor_components(self, component: str, initial_release: str, release: str) -> List[str]:
        """
        Returns list of successor components from a next major release.

        Note that this currently expects PES only holds successors for a next major release.

        :param str component: Component to find successors for.
        :param str initial_release: Version of source system in a RHEL-X.Y format.
        :param str release: Version of targeted system in a RHEL-X.Y format.
        """
        successors = cast(List[str], self._pes_api.get_successor_components(component, initial_release, release))
        successors.sort()

        log_dict(self.info,
                 "Successors of component '{}' ('{}') in release '{}'".format(component, initial_release, release),
                 successors)

        return successors
