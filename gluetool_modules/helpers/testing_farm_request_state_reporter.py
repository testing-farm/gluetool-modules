# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool

from typing import Any, Optional  # noqa
from gluetool import Failure

STATE_QUEUED = 'queued'
STATE_RUNNING = 'running'
STATE_COMPLETE = 'complete'
STATE_ERROR = 'error'


class TestingFarmRequestStateReporter(gluetool.Module):
    name = 'testing-farm-request-state-reporter'
    description = 'Updates request according to the pipeline state.'

    def __init__(self, *args, **kwargs):
        # type: (*Any, **Any) -> None
        super(TestingFarmRequestStateReporter, self).__init__(*args, **kwargs)

    def execute(self):
        # type: () -> None
        self.require_shared('testing_farm_request')

        request = self.shared('testing_farm_request')
        request.update(state=STATE_RUNNING, artifacts_url=self.shared('coldstore_url'))

    def destroy(self, failure=None):
        # type: (Optional[Failure]) -> None
        if failure is not None and isinstance(failure.exc_info[1], SystemExit):
            return

        request = self.shared('testing_farm_request')

        if not request:
            self.warn('no request found in pipeline, refusing to report state', sentry=True)
            return

        if failure:
            self.info('reporting pipeline state - error')
            assert failure.exc_info[1] is not None
            request.update(
                state=STATE_ERROR,
                summary=str(failure.exc_info[1].message),
                overall_result='error',
                artifacts_url=self.shared('coldstore_url')
            )
            return

        test_results = self.shared('results')

        if not test_results:
            self.info('reporting pipeline state - error - no results')
            request.update(state=STATE_ERROR, overall_result='error', artifacts_url=self.shared('coldstore_url'))
            return

        self.info('reporting pipeline state - complete')
        request.update(
            state=STATE_COMPLETE,
            overall_result=test_results['overall-result'],
            xunit=str(test_results),
            artifacts_url=self.shared('coldstore_url')
        )
