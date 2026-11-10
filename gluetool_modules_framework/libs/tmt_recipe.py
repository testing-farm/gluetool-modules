# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0
#
# Support for tmt recipes, see https://tmt.readthedocs.io/en/stable/guide.html#recipes
#
# This file contains common code used in:
# * gluetool_modules_framework/testing/test_schedule_tmt.py
# * gluetool_modules_framework/testing/test_schedule_tmt_multihost.py

import base64
import binascii
import copy
import re
import zlib

import gluetool
import ruamel.yaml
from gluetool import SoftGlueError
from gluetool.log import ContextAdapter, Logging
from gluetool.utils import create_cattrs_unserializer, dump_yaml, from_yaml

from gluetool_modules_framework.libs.test_schedule_tmt import TMTPlan
from gluetool_modules_framework.testing_farm.testing_farm_request import TestingFarmRequestTMT

# Type annotations
from typing import cast, Any, Dict, List, Optional  # noqa

# Recipe file passed to `tmt run --recipe`, stored in the plan's work directory
TMT_RECIPE = 'tmt-recipe.yaml'

# Maximum size of a recipe in MiB, applies to the downloaded and the decompressed content
RECIPE_MAX_SIZE = 50

# Timeout in seconds for downloading a recipe
RECIPE_DOWNLOAD_TIMEOUT = 60

GZIP_MAGIC = b'\x1f\x8b'

URL_PATTERN = re.compile(r'^https?://')

# Keys of provision phases kept in the recipe. Everything else, e.g. image, arch or plugin specific options filled in
# by tmt during the original run, is dropped so it does not override what Testing Farm passes on the command line.
PROVISION_PHASE_KEYS = (
    'name',
    'order',
    'how',
    'role',
    'hardware',
    'kickstart',
    'pool',
    'user',
    'become',
    'ssh-option',
    'watchdog-dispatch-delay',
    'watchdog-period-delay',
)


def _b64decode(value: str) -> bytes:
    # Ignore whitespace, e.g. line wrapping added by `base64` command
    return base64.b64decode(''.join(value.split()), validate=True)


def recipe_kind(value: str) -> str:
    """
    Return the form of the recipe as given in the request: ``url``, ``base64`` or ``yaml``.
    """

    if URL_PATTERN.match(value):
        return 'url'

    try:
        _b64decode(value)

    except (binascii.Error, ValueError):
        return 'yaml'

    return 'base64'


def _check_size(content: bytes) -> None:
    if len(content) > RECIPE_MAX_SIZE * 1024 * 1024:
        raise SoftGlueError('Recipe is larger than {} MiB, refusing to continue.'.format(RECIPE_MAX_SIZE))


def _download(url: str, logger: ContextAdapter) -> bytes:
    logger.info("downloading recipe from '{}'".format(url))

    try:
        with gluetool.utils.requests(logger=logger) as req:
            response = req.get(url, timeout=RECIPE_DOWNLOAD_TIMEOUT, stream=True)

            if response.status_code != 200:
                raise SoftGlueError("Failed to download recipe from '{}', HTTP status {}.".format(
                    url, response.status_code
                ))

            content = b''
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                content += chunk
                _check_size(content)

    except SoftGlueError:
        raise

    except Exception as exc:
        raise SoftGlueError("Failed to download recipe from '{}': {}".format(url, exc)) from exc

    return content


def _decompress(content: bytes) -> bytes:
    # Decompress in a bounded way, to protect against decompression bombs
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    max_size = RECIPE_MAX_SIZE * 1024 * 1024

    try:
        decompressed = decompressor.decompress(content, max_size + 1)

    except zlib.error as exc:
        raise SoftGlueError('Failed to decompress recipe: {}'.format(exc)) from exc

    _check_size(decompressed)

    return decompressed


def load_recipe(value: str, logger: Optional[ContextAdapter] = None) -> Dict[str, Any]:
    """
    Load a tmt recipe from a value given in the Testing Farm request.

    The value can be one of:

    * a plain YAML text of the recipe,
    * a base64 encoded recipe, optionally gzip compressed,
    * an URL to the recipe, its content optionally gzip compressed.

    :raises gluetool.SoftGlueError: if the recipe cannot be loaded or is not valid.
    :returns: the recipe as a dictionary.
    """

    logger = logger or Logging.get_logger()

    kind = recipe_kind(value)

    if kind == 'url':
        content = _download(value, logger)

    elif kind == 'base64':
        content = _b64decode(value)

    else:
        content = value.encode('utf-8')

    _check_size(content)

    if content.startswith(GZIP_MAGIC):
        content = _decompress(content)

    try:
        recipe = from_yaml(content.decode('utf-8'), loader_type='safe')

    except (gluetool.GlueError, ruamel.yaml.YAMLError, UnicodeDecodeError) as exc:
        raise SoftGlueError('Failed to parse recipe: {}'.format(exc)) from exc

    if not isinstance(recipe, dict):
        raise SoftGlueError('Recipe must be a mapping.')

    plans = recipe.get('plans')

    if not plans or not isinstance(plans, list):
        raise SoftGlueError('Recipe does not contain any plans.')

    for plan in plans:
        if not isinstance(plan, dict) or not isinstance(plan.get('name'), str):
            raise SoftGlueError('Recipe contains a plan without a name.')

    return recipe


def check_recipe_request(tmt_request: TestingFarmRequestTMT) -> None:
    """
    Refuse plan and test selectors combined with a recipe. Plans and tests to run are defined by the recipe, and tmt
    ignores test selectors when running a recipe.

    :raises gluetool.SoftGlueError: if a selector is used together with a recipe.
    """

    selectors = [
        name for name, value in (
            ('name', tmt_request.name),
            ('plan_filter', tmt_request.plan_filter),
            ('test_name', tmt_request.test_name),
            ('test_filter', tmt_request.test_filter),
        )
        if value
    ]

    if selectors:
        raise SoftGlueError('Recipe cannot be combined with {}.'.format(', '.join(selectors)))


def recipe_plan_names(recipe: Dict[str, Any]) -> List[str]:
    """
    Return names of plans in the recipe.
    """

    return [plan['name'] for plan in recipe['plans']]


def _recipe_plan(recipe: Dict[str, Any], plan: str) -> Dict[str, Any]:
    for recipe_plan in recipe['plans']:
        if recipe_plan['name'] == plan:
            return cast(Dict[str, Any], recipe_plan)

    raise SoftGlueError("Plan '{}' not found in the recipe.".format(plan))


def _step_phases(recipe_plan: Dict[str, Any], step: str) -> List[Dict[str, Any]]:
    step_data = recipe_plan.get(step)

    if not isinstance(step_data, dict):
        return []

    return step_data.get('phases') or []


def recipe_to_tmt_plan(recipe: Dict[str, Any], plan: str) -> TMTPlan:
    """
    Construct :py:class:`TMTPlan` from a plan in the recipe. Replaces ``tmt plan export`` used for plans from git.
    """

    recipe_plan = _recipe_plan(recipe, plan)

    return create_cattrs_unserializer(TMTPlan)({
        'name': plan,
        'provision': _step_phases(recipe_plan, 'provision'),
        'prepare': _step_phases(recipe_plan, 'prepare'),
    })


def write_recipe(recipe: Dict[str, Any], root: Optional[str], filepath: str) -> None:
    """
    Write the recipe for use with ``tmt run --recipe``.

    The recipe is adjusted for running in Testing Farm:

    * ``run.root`` is set to the metadata root, relative to the repository clone, the same way as ``--root`` option.
      The recipe then works both in the worker and in the reproducer.
    * ``run.environment`` is removed. If present, tmt uses it instead of the ``-e`` option, and it would hide
      the variables and secrets from the request.
    * provision phases are reduced to :py:data:`PROVISION_PHASE_KEYS`.

    :param dict recipe: recipe to write.
    :param str root: metadata root, ``test.fmf.path`` from the request.
    :param str filepath: path to the recipe file.
    """

    recipe = copy.deepcopy(recipe)

    run = recipe.get('run')
    if not isinstance(run, dict):
        run = recipe['run'] = {}

    run['root'] = root or '.'
    run.pop('environment', None)

    for recipe_plan in recipe['plans']:
        provision = recipe_plan.get('provision')

        if not isinstance(provision, dict) or not isinstance(provision.get('phases'), list):
            continue

        provision['phases'] = [
            {key: value for key, value in phase.items() if key in PROVISION_PHASE_KEYS}
            for phase in provision['phases']
            if isinstance(phase, dict)
        ]

    dump_yaml(recipe, filepath)
