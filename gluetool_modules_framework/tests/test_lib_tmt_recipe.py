# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import base64
import contextlib
import gzip
import os

import pytest
from mock import MagicMock

import gluetool
from gluetool import SoftGlueError

import gluetool_modules_framework.libs.tmt_recipe
from gluetool_modules_framework.libs.test_schedule_tmt import TMTPlan, TMTPlanPrepare, TMTPlanProvision
from gluetool_modules_framework.libs.tmt_recipe import check_recipe_request, load_recipe, recipe_kind, \
    recipe_plan_names, recipe_to_tmt_plan, write_recipe
from gluetool_modules_framework.testing_farm.testing_farm_request import TestingFarmRequestTMT

ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets', 'tmt_recipe')
RECIPE_FILEPATH = os.path.join(ASSETS_DIR, 'recipe.yaml')


@pytest.fixture(name='recipe_text')
def fixture_recipe_text():
    with open(RECIPE_FILEPATH) as f:
        return f.read()


def _mock_download(monkeypatch, content, status_code=200):
    response = MagicMock(status_code=status_code, iter_content=MagicMock(return_value=[content]))
    req = MagicMock(get=MagicMock(return_value=response))

    @contextlib.contextmanager
    def requests(logger=None):
        yield req

    monkeypatch.setattr(gluetool.utils, 'requests', requests)

    return req


@pytest.mark.parametrize('value, expected', [
    ('https://example.com/recipe.yaml', 'url'),
    ('http://example.com/recipe.yaml', 'url'),
    ('plans:\n- name: /plan\n', 'yaml'),
    (base64.b64encode(b'plans: []').decode(), 'base64'),
    # line wrapping added by the `base64` command
    ('cGxhbnM6\nIFtd\n', 'base64'),
])
def test_recipe_kind(value, expected):
    assert recipe_kind(value) == expected


def test_load_recipe_yaml(recipe_text):
    recipe = load_recipe(recipe_text)

    assert recipe_plan_names(recipe) == ['/plans/smoke']
    assert [test['name'] for test in recipe['plans'][0]['discover']['tests']] == ['/tests/a', '/tests/b']


def test_load_recipe_base64(recipe_text):
    assert load_recipe(base64.b64encode(recipe_text.encode()).decode()) == load_recipe(recipe_text)


def test_load_recipe_base64_gzip(recipe_text):
    value = base64.b64encode(gzip.compress(recipe_text.encode())).decode()

    assert load_recipe(value) == load_recipe(recipe_text)


@pytest.mark.parametrize('compress', [False, True])
def test_load_recipe_url(monkeypatch, recipe_text, compress):
    content = recipe_text.encode()
    req = _mock_download(monkeypatch, gzip.compress(content) if compress else content)

    assert load_recipe('https://example.com/recipe.yaml') == load_recipe(recipe_text)

    req.get.assert_called_once_with('https://example.com/recipe.yaml', timeout=60, stream=True)


def test_load_recipe_url_error(monkeypatch):
    _mock_download(monkeypatch, b'', status_code=404)

    with pytest.raises(SoftGlueError, match=r"Failed to download recipe from 'https://example.com/recipe.yaml', HTTP status 404."):  # noqa
        load_recipe('https://example.com/recipe.yaml')


def test_load_recipe_too_large(monkeypatch):
    monkeypatch.setattr(gluetool_modules_framework.libs.tmt_recipe, 'RECIPE_MAX_SIZE', 1)

    # compresses well, but decompressed exceeds the limit
    value = base64.b64encode(gzip.compress(b'plans: []\n' + b'#' * 2 * 1024 * 1024)).decode()

    with pytest.raises(SoftGlueError, match=r'Recipe is larger than 1 MiB, refusing to continue.'):
        load_recipe(value)


@pytest.mark.parametrize('value, error', [
    ('plans: [', r'Failed to parse recipe'),
    ('- foo\n- bar\n', r'Recipe must be a mapping.'),
    ('run:\n  root: .\n', r'Recipe does not contain any plans.'),
    ('plans: []\n', r'Recipe does not contain any plans.'),
    ('plans:\n- summary: no name\n', r'Recipe contains a plan without a name.'),
    (base64.b64encode(b'\x1f\x8bnot gzip').decode(), r'Failed to decompress recipe'),
])
def test_load_recipe_invalid(value, error):
    with pytest.raises(SoftGlueError, match=error):
        load_recipe(value)


@pytest.mark.parametrize('options, error', [
    ({}, None),
    ({'name': '/plan'}, r'Recipe cannot be combined with name.'),
    ({'plan_filter': 'tag:foo', 'test_filter': 'tag:bar'}, r'Recipe cannot be combined with plan_filter, test_filter.'),
    ({'test_name': '/test'}, r'Recipe cannot be combined with test_name.'),
])
def test_check_recipe_request(options, error):
    tmt_request = TestingFarmRequestTMT(url='https://example.com/repo', ref='main', recipe='plans: []', **options)

    if error is None:
        check_recipe_request(tmt_request)
        return

    with pytest.raises(SoftGlueError, match=error):
        check_recipe_request(tmt_request)


def test_recipe_to_tmt_plan(recipe_text):
    assert recipe_to_tmt_plan(load_recipe(recipe_text), '/plans/smoke') == TMTPlan(
        name='/plans/smoke',
        provision=[TMTPlanProvision(how='virtual', hardware={'memory': '>= 4 GB'})],
        prepare=[TMTPlanPrepare(how='install', exclude=['bar'])]
    )


def test_recipe_to_tmt_plan_missing(recipe_text):
    with pytest.raises(SoftGlueError, match=r"Plan '/plans/missing' not found in the recipe."):
        recipe_to_tmt_plan(load_recipe(recipe_text), '/plans/missing')


@pytest.mark.parametrize('root, expected_root', [
    (None, '.'),
    ('some/path', 'some/path'),
])
def test_write_recipe(recipe_text, tmpdir, root, expected_root):
    recipe = load_recipe(recipe_text)
    filepath = str(tmpdir.join('recipe.yaml'))

    write_recipe(recipe, root, filepath)

    written = gluetool.utils.load_yaml(filepath)

    assert written['run']['root'] == expected_root
    assert 'environment' not in written['run']
    assert written['plans'][0]['provision']['phases'] == [{
        'become': False,
        'hardware': {'memory': '>= 4 GB'},
        'how': 'virtual',
        'name': 'default-0',
        'order': 50,
        'user': 'root',
    }]
    assert written['plans'][0]['discover'] == recipe['plans'][0]['discover']

    # the original recipe is not modified
    assert recipe['run']['environment'] == {'FOO': 'bar'}
