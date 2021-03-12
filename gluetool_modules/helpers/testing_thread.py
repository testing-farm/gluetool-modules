import hashlib
import json
import time

import bs4
import gluetool
from gluetool.utils import render_template

import gluetool_modules.libs


DEFAULT_ID_FILE = 'testing-thread-id.json'


class TestingThread(gluetool.Module):
    """
    A `testing thread` property of ``citool`` pipeline signals a dependency between different
    ``citool`` pipelines. In CI, one job usualy starts other jobs, often a lot of them, and
    one might be interested in tracking which job belongs to which "family" of jobs.

    The first thread ID is generated by a "parent" pipeline (e.g. in jenkins build ``J1``), as
    a SHA1 hash of string, which is rendered from template choose by `id-template` option. Template can
    contain variables from `eval_context` and `STAMP` representing timestamp. eg. Jenkins-related properties
    (master hostname, job name, etc.) or artifact related properties (build ID).

    If the ``J1`` pipeline wishes to dispatch another pipelines (by dispatching other Jenkins build,
    ``K1`` and ``K2``) and mark them as "kids" of ``J1``, the thread ID of ``J1`` is extended for each
    of the children, and each child gets its own unique thread ID, in a form ``<thread-id>-<child index>``.
    In our example, if ``J1`` pipeline created a thread-id ``foo79bar`` and dispatched its 2 children,
    they will be given thread IDs ``foo79bar-1`` and ``foo79bar-2``, respectively.
    """

    name = 'testing-thread'
    description = 'Simple testing-thread tagging.'
    supported_dryrun_level = gluetool.glue.DryRunLevels.DRY

    shared_functions = ('thread_id',)
    required_options = ['id-template']

    options = {
        'id': {
            'help': 'Current testing thread ID.',
            'metavar': 'ID'
        },
        'id-template': {
            'help': """
                    Template for string, which is used to generate testing-thread id. It has access to all
                    variables available in the eval context, with ``STAMP`` representing timestamp.
                    """,
            'type': str
        },
        'id-length': {
            'help': 'Number of hash characters used as a thread ID (default: %(default)s).',
            'metavar': 'NUMBER',
            'type': int,
            'default': 12
        },
        'id-file': {
            'help': 'If set, module will store the ID in this file (default: %(default)s).',
            'metavar': 'PATH',
            'default': DEFAULT_ID_FILE
        }
    }

    def __init__(self, *args, **kwargs):
        super(TestingThread, self).__init__(*args, **kwargs)

        self._thread_id = None

    @property
    def eval_context(self):
        if gluetool_modules.libs.is_recursion(__file__, 'eval_context'):
            return {}

        __content__ = {  # noqa
            'THREAD_ID': """
                         ID of the current testing thread.
                         """
        }

        return {
            'THREAD_ID': self._thread_id
        }

    def thread_id(self):
        """
        Returns current testing thread ID.

        :rtype: str
        """

        return self._thread_id

    def _create_thread_id(self, template):
        self.debug("creating a thread ID from template: '{}'".format(template))

        context = gluetool.utils.dict_update(self.shared('eval_context'),
                                             {'STAMP': int(time.time())})

        s = render_template(template, **context)

        self.debug("creating a thread ID from string: '{}'".format(s))

        sha = hashlib.sha1()
        sha.update(s)

        return sha.hexdigest()[0:self.option('id-length')]

    def sanity(self):
        if self.option('id'):
            self._thread_id = self.option('id')

            self.info('testing thread ID set to {}'.format(self._thread_id))

    def execute(self):
        if self._thread_id is not None:
            return

        self._thread_id = self._create_thread_id(self.option('id-template'))
        self.info('testing thread ID set to {}'.format(self._thread_id))

    def destroy(self, failure=None):
        if self._thread_id is None:
            self.warn('Testing thread ID is not set')
            return

        if self.option('id-file'):
            with open(self.option('id-file'), 'w') as f:
                f.write(json.dumps(self._thread_id))
                f.flush()

        results = self.shared('results') or []

        if results:
            if isinstance(results, bs4.element.Tag):
                # Already serialized in test-scheduler workflow.
                pass

            else:
                for result in results:
                    self.debug('result:\n{}'.format(result))

                    if 'testing-thread-id' in result.ids:
                        continue

                    self.debug('adding a testing thread ID')
                    result.ids['testing-thread-id'] = self._thread_id
