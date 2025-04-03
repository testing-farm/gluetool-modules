# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest

from gluetool_modules_framework.libs.test_schedule_tmt import get_test_contacts, \
    TMTDiscoveredTest

CONTACT_ONE = 'John Doe <jdoe@example.com>'
CONTACT_TWO = 'John Smith <jsmith@example.com>'
TEST_WITH_CONTACT_ONE = TMTDiscoveredTest(name='/test/with/contact', contact=[CONTACT_ONE],
                                          serial_number=1)
TEST_WITH_CONTACT_TWO = TMTDiscoveredTest(name='/test/with/contact', contact=[CONTACT_TWO],
                                          serial_number=2)
TEST_WITHOUT_CONTACT = TMTDiscoveredTest(name='/test/without/contact', contact=[],
                                         serial_number=3)
DISCOVERED_TESTS = [TEST_WITH_CONTACT_ONE, TEST_WITH_CONTACT_TWO, TEST_WITHOUT_CONTACT]


@pytest.mark.parametrize('test_name, test_serial_number, expected', [
    ('/non/existent/test', 1, []),
    ('/test/without/contact', 2, []),
    ('/test/with/contact', 2, [CONTACT_TWO]),
    ('/test/with/contact', 1, [CONTACT_ONE]),
])
def test_get_test_contact(test_name, test_serial_number, expected):
    assert get_test_contacts(test_name, test_serial_number, DISCOVERED_TESTS) == expected
