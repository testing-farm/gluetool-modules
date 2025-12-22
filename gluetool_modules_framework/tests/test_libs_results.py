# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import textwrap

import gluetool_modules_framework.libs.results.xunit as xunit


def test_xunit_repr():
    """ Test that class printable representation, large fields should be redacted """
    testcase = xunit.XUnitTestCase(
        name='testcase',
        system_out=['output'],
        classname='class-name',
        failure=xunit.XUnitFailure(
            type='fail',
            message='message'
        ),
        time=1
    )

    assert str(testcase) == textwrap.dedent("""
        XUnitTestCase(name='testcase', classname='class-name', failure=XUnitFailure(type='fail'), error=None, time=1, start_time=None, end_time=None)
    """).strip()
