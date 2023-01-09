Development
===========

Environment
-----------

Before moving on to the actual setup, there are few important notes:

- **The only supported and (sort of tested) way of using and developing these modules is using Poetry!**

-  The tested distributions (as in "we're using these") are the recent releases of Fedora and CentOS Stream 9.
   You could try other distributions but we didn't - please, let us know, and it'd be awesome if your first merge request
   could update this file :)


Requirements
------------

To begin digging into sources, there are few requirements to mention:

- ``poetry``, installed as described on the `installation page <https://python-poetry.org/docs/#installation>`__ - preferred version is **1.1.15**.

- ``ansible-playbook`` installed on your localhost

- system packages - it is either impossible or impractical to use their Python counterpart, or they are required to
  build a Python package. In some cases, on recent Fedora for example, it's been shown for some packages
  their ``compat-*`` variant might be needed. See the optional ``Bootstrap system environment`` step bellow.

Do not worry, the next sections will guide you through the whole installation process.


Installation
------------

0. Bootstrap system environment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Following steps are necessary to install requirements for different distributions:

.. warning::

    We support only CentoOS Stream 9 and Fedora.

**CentOS Stream 9**

Run all commands listed in the TMT test plan's ``prepare`` section. Test plan is located in the file `plans/centos9.fmf`.

**Fedora**

Run all commands listed in the TMT test plan's ``prepare`` section. Test plan is located in the file `plans/fedora.fmf`.


1. Install ``poetry``
~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

    curl -sSL https://install.python-poetry.org | python3 - --version 1.1.15

.. note::
    You might need to open a new shell to have ``poetry`` command available.


2. Clone ``gluetool-modules``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   git clone git@gitlab.com:testing-farm/gluetool-modules.git
   cd gluetool-modules


3. Install ``gluetool-modules``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   poetry install

.. note::
    If you want to install also useful development modules, use ``poetry install -E development``


4. Install extra requirements
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   poetry run /usr/bin/ansible-playbook inject-extra-requirements.yml

**Be warned:** read the messages reported by this step - ``inject-extra-requirements.yml`` playbook checks for
necessary system packages, and reports any missing pieces. **It does not install them!** - we don't want to
mess up your system setup, as we try to stay inside our little own virtualenv, but the playbook will try to
provide hints on what packages might solve the issue. Hopefully, you already have all required system packages.

.. _step_config:

5. Add configuration
~~~~~~~~~~~~~~~~~~~~

``citool`` looks for its configuration in ``~/.citool.d``. Add configuration for the modules according to your
preference by cloning the repository with your citool configuration. Note that this expects you know where
the repository is, if unsure, ask the maintainers of this project:

.. code-block:: bash

   git clone -b staging https://YOUR_CITOOL_CONFIG_REPOSITORY ~/.citool.d


6. Add local configuration (optional)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A configuration you cloned from remote repository in step :ref:`step_config` is often tailored for other task (e.g. CI) while
providing reasonable functionality when used locally. To tweak things for you, you can create a local configuration
``citool`` looks for configuration files in its working directory as well, i.e. when running from your
``gluetool-modules`` clone, it looks for ``.gluetool.d`` (or ``.citool.d`` directory).

.. code-block:: bash

   mkdir .citool.d
   cat << EOF > .citool.d/citool
   [default]
   output = citool-debug.txt
   colors = yes
   EOF


7. Test ``citool``
~~~~~~~~~~~~~~~~~~

To run ``citool`` you can use the ``poetry run`` command, which executes the given command in the virtualenv.

.. code-block:: bash

    poetry run citool -l
    ... pile of modules ...

You can also enter a shell with activated virtualenv by running the command:

.. code-block:: bash

   poetry shell

If you are in an poetry shell, you can run citool directly:

.. code-block:: bash

    citool -l
    ... pile of modules ...

Testing Farm how-tos
-------------------

Locally run a test pipeline
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Every testing farm artifact includes a ``pipeline.log`` with the ``gluetool`` invocation. This pipeline can be modified
to run against a local VM. First, check out the production configuration:

.. code-block:: bash

    git clone -b testing-farm https://gitlab.com/testing-farm/infrastructure/

Then pick a particular request, e.g one with a COPR installation, and run a pipeline with the `static-guest` provisioner.

.. code-block:: bash

    gluetool -c --module-config-path infrastructure/ranch/public/citool-config/config \
       rules-engine \
       ansible \
       static-guest --guest root@VMHOST:VMSSHPORT --ssh-key PATH/TO/identity \
       testing-farm-request --api-url https://internal.api.dev.testing-farm.io --api-key YOURAPIKEY --request-id REQUEST_ID \
       guest-setup \
       fedora-copr:copr \
       install-copr-build \
       guess-environment-testing-farm-request:guess-environment \
       dist-git-testing-farm:dist-git \
       test-schedule-tmt-connect:test-schedule-tmt \
       test-scheduler-noconfig:test-scheduler-baseosci \
       test-schedule-runner \
       test-schedule-report

If you need to run pre/post setup playbooks as well, add these parameters to
the ``guest-setup`` module (the production config assumes a container bind
mount, which doesn't work locally):

.. code-block:: bash

       guest-setup --playbooks 'pre-artifact-installation:infrastructure/ranch/public/citool-config/guest-setup/pre-artifact-installation/entrypoint.yml,post-artifact-installation:infrastructure/ranch/public/citool-config/guest-setup/post-artifact-installation/entrypoint.yml' --extra-vars pre-artifact-installation:IMAGE_NAME=<image-name>,post-artifact-installation:TESTING_FARM_REQUEST_ID=<request-id>

Replace ``<image-name>`` with e.g. ``Fedora-37``, and ``<request-id>`` with the
same ID as for the ``testing-farm-request`` module above.

Test suites
-----------

The test suite is governed by ``tox`` and ``py.test``. Tox can be easily executed by:

.. code-block:: bash

    tox

Tox also accepts additional options which are then passed to ``py.test``:

.. code-block:: bash

    tox -- --cov=gluetool_modules_framework --cov-report=html:coverage-report

Tox creates (and caches) virtualenv for its test runs, and uses them for running the tests. It integrates multiple
different types of test (you can see them by running ``tox -l``).


Installation tests
~~~~~~~~~~~~~~~~~~

A collection of installation tests written with the help of `Test Management Tool (tmt) <https://tmt.readthedocs.io/>` can be found at `plans/README.rst`.

Documentation
-------------

Auto-generated documentation is located in ``docs/`` directory. To update your local copy, run these commands:

.. code-block:: bash

    ansible-playbook ./generate-docs.yaml

Then you can read generated docs by opening ``docs/build/html/index.html``.


Troubleshooting
---------------

No known issues currently, if you run into issues please contact the maintainers.
