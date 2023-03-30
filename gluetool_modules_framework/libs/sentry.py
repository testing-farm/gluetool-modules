# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

# Type annotations
# pylint: disable=unused-import,wrong-import-order
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union  # noqa


class ArtifactFingerprintsMixin(object):
    """
    The goal of this mixin class is to allow custom "soft" exceptions to implement
    per-component fingerprints. To aggregate soft errors on per-component basis
    is a common demand that it makes sense to provide simple mixin class.

    Simple add it as mixin class to your exception class, and don't forget to accept
    ``artifact`` parameter:

    .. code-block:: python

       class FooError(ArtifactFingerprintsMixin, SoftGlueError):
           def __init__(self, task):
               super(FooError, self).__init__(task, 'Some weird foo happened')

    :param artifact: Artifacts in whose context the error happenend. Can be a primary task
        or a Testing Farm request.
    """

    def __init__(self, artifact: Any, *args: Any, **kwargs: Any) -> None:

        super(ArtifactFingerprintsMixin, self).__init__(*args, **kwargs)  # type: ignore  # multiple inheritance

        self.artifact = artifact

        assert artifact.ARTIFACT_NAMESPACE

        if artifact.ARTIFACT_NAMESPACE == 'testing-farm-request':
            self.sentry_fingerpint = self._request_sentry_fingerprint
            self.sentry_tags = self._request_sentry_tags
            return

        self.sentry_fingerpint = self._task_sentry_fingerprint
        self.sentry_tags = self._task_sentry_tags

    def _request_sentry_fingerprint(self, current: List[Any]) -> List[Any]:
        # pylint: disable=unused-argument
        """
        Sets Sentry fingerprints to class name and ``task``'s component and ID,
        to force aggregation of errors on a per-component basis.
        """

        # Not calling super - this mixin wants to fully override any possible
        # fingerprints. If you want these fingerprints to coexist with what this
        # mixin provides, do it on your own.

        return [
            self.__class__.__name__,
            self.artifact.id,
            self.artifact.type,
            self.artifact.url,
            self.artifact.ref
        ]

    def _request_sentry_tags(self, current: Dict[str, Any]) -> Dict[str, Any]:
        """
        Adds task namespace and ID as Sentry tags.
        """

        if 'artifact-namespace' not in current:
            current.update({
                'artifact-namespace': self.artifact.ARTIFACT_NAMESPACE
            })

        if 'request-id' not in current:
            current.update({
                'request-id': self.artifact.id,
                'request-type': self.artifact.type,
                'request-url': self.artifact.url,
                'request-ref': self.artifact.ref
            })

        return current

    def _task_sentry_fingerprint(self, current: List[Any]) -> List[Any]:
        # pylint: disable=unused-argument
        """
        Sets Sentry fingerprints to class name and ``task``'s component and ID,
        to force aggregation of errors on a per-component basis.
        """

        # Not calling super - this mixin wants to fully override any possible
        # fingerprints. If you want these fingerprints to coexist with what this
        # mixin provides, do it on your own.

        return [
            self.__class__.__name__,
            self.artifact.component,
            self.artifact.id
        ]

    def _task_sentry_tags(self, current: Dict[str, Any]) -> Dict[str, Any]:
        """
        Adds task namespace and ID as Sentry tags.
        """

        if 'component' not in current:
            current['component'] = self.artifact.component

        if 'artifact-id' not in current:
            current.update({
                'artifact-namespace': self.artifact.ARTIFACT_NAMESPACE,
                'artifact-id': self.artifact.id,
            })

        return current
