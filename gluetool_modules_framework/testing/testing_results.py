# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import json
import sys

import gluetool
import gluetool_modules_framework.libs.results.test_result
from gluetool_modules_framework.libs.results import Results

# Type annotations
from typing import cast, Any, List, Tuple, Optional, Union  # noqa


class TestingResults(gluetool.Module):
    """
    Provides support for gathering and exporting testing results.

    Keeps internal ``list`` of produced results
    (instances of :py:class:`gluetool_modules_framework.libs.results.test_result.TestResult`),
    and provides it to callers via its shared function :py:meth:`results`. Users can then modify the
    list and results it carries.

    The module is able to store results in a file, or initialize the internal list from a file.
    Different formats are supported, namely JSON (``json`` format) and xUnit (``xunit`` format).
    """

    name = 'testing-results'
    description = 'Provides support for gathering and exporting testing results.'

    supported_dryrun_level = gluetool.glue.DryRunLevels.DRY

    options = {
        'results-file': {
            'help': 'Format and path to a file to store results into (default: none).',
            'metavar': 'FORMAT:PATH',
            'action': 'append',
            'default': []
        },
        'init-file': {
            'help': 'Format and path to initialize results from.',
            'metavar': 'FORMAT:PATH'
        }
    }

    shared_functions = ['results', 'serialize_results']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(TestingResults, self).__init__(*args, **kwargs)

        self._results: List[gluetool_modules_framework.libs.results.test_result.TestResult] = []

    def results(self) -> List[gluetool_modules_framework.libs.results.test_result.TestResult]:
        """
        Return list of gathered results.

        :rtype: list
        :returns: list of gathered results
                  (instances of :py:class:`gluetool_modules_framework.libs.results.test_result.TestResult`).
        """

        return self._results

    def _parse_formats(self, option: str) -> List[Tuple[str, ...]]:
        """
        Converts different forms on format:file specifications into a ``list``. These:

        * from a config file: ``format1:file1.ext, format2:file2.ext``
        * from an option (specified possibly multiple times): ``['format1:file1.ext', '  format2  : file2.ext  ']``

        will result into ``[('foo', 'bar'), ('bar', 'baz')]``.
        """

        specs: Union[str, List[str]] = self.option(option)

        if isinstance(specs, str):
            specs = [s.strip() for s in specs.split(',')]

        parsed: List[Tuple[str, ...]] = []

        for spec in specs:
            if ':' not in spec:
                raise gluetool.GlueError(
                    "Value '{}' of option '{}' does not specify format and filename".format(spec, option)
                )

            parsed.append(tuple([s.strip() for s in spec.split(':')]))

        return parsed

    @staticmethod
    def _serialize_to_results(results: List[gluetool_modules_framework.libs.results.test_result.TestResult]) -> Results:
        """
        Converts the legacy format (list of TestResult) into the new gluetool_modules_framework.libs.results.Results.
        """
        output_results = Results()

        for result in results:
            output_results.test_suites.append(result.convert_to_results())

        return output_results

    writers = {
        'xunit_testing_farm': lambda stream, results: stream.write(results.xunit_testing_farm.to_xml_string(
            pretty_print=True
        )),
        'xunit': lambda stream, results: stream.write(results.xunit.to_xml_string(pretty_print=True))
    }

    def serialize_results(
        self,
        output_format: str,
        results: Optional[List[gluetool_modules_framework.libs.results.test_result.TestResult]] = None
    ) -> Union[List[Any], Results]:
        if results is None:
            results = self._results

        # NOTE: "Serializing" is not a correct name for this action anymore. It does not consist of "serializing to
        # string" anymore, it's just a conversion to the new ``gluetool_modules_framework/libs/results.Results`` format.
        serializer = {
            'xunit_testing_farm': TestingResults._serialize_to_results,
            'xunit': TestingResults._serialize_to_results,
        }.get(output_format, None)

        if serializer is None:
            raise gluetool.GlueError("Output format '{}' is not supported".format(output_format))

        return cast(Union[List[Any], Results], serializer(results))

    def execute(self) -> None:
        initfile = self.option('init-file')

        if initfile is None:
            return

        input_format, input_file = cast(Tuple[str, Any], self._parse_formats('init-file')[0])

        self.info("loading results from '{}', in format '{}'".format(input_file, input_format))

        def _default_unserialize(result: Any) -> gluetool_modules_framework.libs.results.test_result.TestResult:
            return gluetool_modules_framework.libs.results.test_result.TestResult.unserialize(self.glue, 'json', result)

        # load results from init file
        try:
            with open(input_file, 'r') as f:
                if input_format == 'json':
                    try:
                        results = json.load(f)

                    except ValueError as exc:
                        raise gluetool.GlueError("Cannot load JSON data from file '{}': {}".format(input_file,
                                                                                                   str(exc)))

                    for result in results:
                        if 'result_class' in result:
                            klass_path = result['result_class'].split('.')
                            module_name, klass_name = '.'.join(klass_path[0:-1]), klass_path[-1]

                            if module_name not in sys.modules:
                                self.warn("Cannot find result module '{}'".format(module_name), sentry=True)
                                result = _default_unserialize(result)

                            elif not hasattr(sys.modules[module_name], klass_name):
                                self.warn("Cannot find result class '{}'".format(klass_name), sentry=True)
                                result = _default_unserialize(result)

                            else:
                                klass = getattr(sys.modules[module_name], klass_name)
                                result = klass.unserialize(self.glue, 'json', result)

                        else:
                            result = _default_unserialize(result)

                        gluetool.log.log_dict(self.debug, 'result', result.serialize('json'))
                        self._results.append(result)

                else:
                    raise gluetool.GlueError("Input format '{}' is not supported".format(input_format))

        except KeyError as e:
            raise gluetool.GlueError('init file is invalid, key {} not found'.format(e))
        except IOError as e:
            raise gluetool.GlueError(str(e))

    def destroy(self, failure: Optional[Any] = None) -> None:
        # the results-file option can be empty if parsing of arguments failed
        if not self.option('results-file'):
            self.warn('No results file set.', sentry=True)
            return

        outputs = self._parse_formats('results-file')

        if not self.dryrun_allows('Exporting results into a file'):
            return

        gluetool.log.log_dict(self.debug, 'outputs', outputs)

        for output_format, output_file in cast(Tuple[str, Any], outputs):
            serialized = self.serialize_results(output_format, self._results)

            with open(output_file, 'w') as f:
                self.writers[output_format](f, serialized)  # type: ignore
                f.flush()

            self.info("Results in format '{}' saved into '{}'".format(output_format, output_file))
