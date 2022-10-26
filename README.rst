Collection of gluetool modules used by Testing Farm Team
---------------------------------------------------------

Documentation
-------------

For more information see the generated documentation

https://gluetool-modules.readthedocs.io

Testing
-------

Install the test dependencies:

    sudo dnf install tox poetry libcurl-devel libpq-devel popt-devel

Run a particular `test scenario <./tox.ini>`_ with:

    tox -e py37-unit-tests

To run a concrete test, you can call tox this way.

    tox -e py27-unit-tests -- gluetool_modules_framework/tests/test_execute_command.py::test_sanity
