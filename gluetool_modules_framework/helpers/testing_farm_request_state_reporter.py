# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import argparse
import gluetool

from typing import Any, Optional, cast  # noqa
from gluetool import Failure
from gluetool.log import log_dict

STATE_QUEUED = 'queued'
STATE_RUNNING = 'running'
STATE_COMPLETE = 'complete'
STATE_ERROR = 'error'

ALLOWED_OVERALL_RESULTS = ['passed', 'failed', 'skipped', 'unknown', 'error']


class TestingFarmRequestStateReporter(gluetool.Module):
    name = 'testing-farm-request-state-reporter'
    description = 'Updates request according to the pipeline state.'

    options = [
        ('Mapping options', {
            'overall-result-map': {
                'help': 'Instructions to decide the overall result of the pipeline.'
            },
            'state-map': {
                'help': 'Instructions to decide the state of the pipeline.'
            },
            'summary-map': {
                'help': 'Instructions to decide the summary message. By default the error message from the pipline.'
            }
        })
    ]

    @gluetool.utils.cached_property
    def state_map(self) -> Any:
        if not self.option('state-map'):
            return []

        return gluetool.utils.load_yaml(self.option('state-map'), logger=self.logger)

    @gluetool.utils.cached_property
    def overall_result_map(self) -> Any:
        if not self.option('overall-result-map'):
            return []

        return gluetool.utils.load_yaml(self.option('overall-result-map'), logger=self.logger)

    @gluetool.utils.cached_property
    def summary_map(self) -> Any:
        if not self.option('summary-map'):
            return []

        return gluetool.utils.load_yaml(self.option('summary-map'), logger=self.logger)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(TestingFarmRequestStateReporter, self).__init__(*args, **kwargs)

    def execute(self) -> None:
        self.require_shared('testing_farm_request')

        request = self.shared('testing_farm_request')
        request.update(state=STATE_RUNNING, artifacts_url=self.shared('coldstore_url'))

    def destroy(self, failure: Optional[Failure] = None) -> None:
        if failure is not None and isinstance(failure.exc_info[1], SystemExit):
            return

        request = self.shared('testing_farm_request')

        if not request:
            self.warn('no request found in pipeline, refusing to report state', sentry=True)
            return

        test_results = self.shared('results')

        if failure:
            self.info('pipeline failure')
        elif not test_results:
            self.info('pipeline no results')
        else:
            self.info('pipeline complete')

        request.update(
            state=self._get_state(failure),
            overall_result=self._get_overall_result(
                test_results.overall_result
                if test_results and test_results.overall_result in ALLOWED_OVERALL_RESULTS
                else 'unknown',
                failure
            ),
            summary=self._get_summary(failure),
            artifacts_url=self.shared('coldstore_url'),
            # We are indicating that we are updating the state during destroying to deal with
            # some corner cases for request cancellation.
            destroying=True
        )

    def _get_state(self, failure: Any) -> str:
        """
        If failure, determine state from mapping file, defaulting to error.
        """

        if not failure:
            return STATE_COMPLETE

        assert failure.exc_info[1] is not None
        error_message = str(failure.exc_info[1])

        context = gluetool.utils.dict_update(self.shared('eval_context'), {
            'ERROR_MESSAGE': error_message,
            'FAILURE': failure
        })

        for instr in self.state_map:
            log_dict(self.debug, 'state instruction', instr)

            if not self.shared('evaluate_rules', instr.get('rule', 'True'), context=context):
                self.debug('denied by rules')
                continue

            if 'state' not in instr:
                self.warn('State map matched but did not yield any state', sentry=True)
                continue

            self.debug("state set to '{}'".format(instr['state']))

            return cast(str, instr['state'])

        return STATE_ERROR

    def _get_overall_result(self, result: str, failure: Optional[gluetool.Failure]) -> str:
        """
        Determine result.overall from mapping file, defaulting to result content and finally error.
        """

        self.require_shared('evaluate_instructions')

        if not failure:
            return result

        assert failure.exc_info[1] is not None
        error_message = str(failure.exc_info[1])

        context = gluetool.utils.dict_update(self.shared('eval_context'), {
            'OVERALL_RESULT': result,
            'ERROR_MESSAGE': error_message,
            'FAILURE': failure
        })

        overall_result = argparse.Namespace(result=None)

        # Callback for 'result' command
        def _result_callback(instruction: str, command: str, argument: str, context: str) -> None:
            overall_result.result = argument.strip()

            self.debug("overall result set to '{}'".format(overall_result.result))

        self.shared('evaluate_instructions', self.overall_result_map, {
            'result': _result_callback
        }, context=context, default_rule='False')

        if overall_result.result is not None:
            return cast(str, overall_result.result)

        return result

    def _get_summary(self, failure: Optional[gluetool.Failure] = None) -> Optional[str]:
        """
        Map failure error message. By default return the error message.
        """
        if failure is None:
            return None

        assert failure.exc_info[1] is not None
        error_message = str(failure.exc_info[1])

        context = gluetool.utils.dict_update(self.shared('eval_context'), {
            'ERROR_MESSAGE': error_message,
            'FAILURE': failure
        })

        for instr in self.summary_map:
            log_dict(self.debug, 'summary instruction', instr)

            if not self.shared('evaluate_rules', instr.get('rule', 'True'), context=context):
                self.debug('denied by rules')
                continue

            if 'reason' not in instr:
                self.warn('Summary rules matched but did not yield any error reason', sentry=True)
                continue

            reason = gluetool.utils.render_template(instr['reason'], logger=self.logger, **context)
            self.debug("error reason set to '{}'".format(reason))

            return reason

        return error_message
