Collection of gluetool modules used by Testing Farm Team
---------------------------------------------------------

Documentation
-------------

For more information see the generated documentation:

https://gluetool-modules.readthedocs.io

Testing
-------

Install the test dependencies::

    sudo dnf install tox poetry libcurl-devel libpq-devel popt-devel

Run a particular `test scenario <./tox.ini>`_ with::

    tox -e py312-unit-tests

To run a concrete test, you can call tox this way::

    tox -e py312-unit-tests -- gluetool_modules_framework/tests/test_execute_command.py::test_sanity


Container Image
---------------

The project provides a `Dockerfile <./container/Dockerfile>`_ to bundle all the modules into a container image.

To build the image::

    make build

The container image is tested via `goss <https://github.com/aelsabbahy/goss>`_.
First install it according to the `official instructions <https://github.com/aelsabbahy/goss#installation>`_.

To run the image tests::

    make test-image

To edit the image tests::

    make edit-image-test
    <edit goss.yaml file>
    goss validate
    exit

Integration tests
-----------------

Testing Farm worker integration tests defined in the `infrastructure repository <https://gitlab.com/testing-farm/infrastructure>`_ are used to validate the changes against Testing Farm.
The tests run automatically in GitLab CI for all code changes.

To force a different infrastructure repository url or branch, you can use the following strings in the merge request description::

    !infra-repo: REPOSITORY_URL
    !infra-branch: REPOSITORY_BRANCH
