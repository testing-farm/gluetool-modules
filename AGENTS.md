# AGENTS.md

Gluetool modules for [Testing Farm](https://docs.testing-farm.io/) — Python 3.12, Poetry, Apache-2.0.

## Commands

```bash
# Install
poetry install

# Unit tests (parallel)
tox -e py312-unit-tests

# Single test
tox -e py312-unit-tests -- gluetool_modules_framework/tests/test_foo.py::test_bar

# Type checking (mypy --strict)
tox -e type-check

# Static analysis
tox -e py312-static-analysis

# Pre-commit (ruff, yamllint, ansible-lint, gitleaks, license headers)
pre-commit run --all-files

# Container
make build
make test-image
```

Tox automatically runs `ansible-playbook inject-extra-requirements.yml` before tests — this installs system deps and configures PycURL/SSL.
You do not need to run it manually.

## Non-Obvious Patterns

**Module system**: Each module extends `gluetool.Module`.
Modules communicate via *shared functions* — not imports, not events.
A module declares `shared_functions` it exposes and `required_shared_functions`/`shared_functions` it consumes.
Access at runtime via `self.shared('function_name')`.
Never import one module from another directly.

**Module options**: CLI args declared via `options` dict on the class:
```python
options = {
    'option-name': {
        'help': 'Description (default: %(default)s)',
        'action': 'store',
        'default': None,
        'type': str,
        'metavar': 'NAME',
    }
}
```

**Logging**: Always use `self.logger`, `self.debug()`, `self.warn()` from gluetool — never raw `logging` module.
Classes outside modules inherit from `gluetool.log.LoggerMixin`.

**Configuration**: Module options (CLI args via `options` dict), not env vars, not config files.

**HTTP requests**: Use `from gluetool.utils import requests` — not raw `requests`.
The gluetool wrapper adds error handling.

**Template rendering**: Use `from gluetool.utils import render_template` — not raw `jinja2`.

**Command execution**: Use `gluetool.utils.Command` — not `subprocess`.
Returns `ProcessOutput` with `.exit_code`, `.stdout`, `.stderr`.

**Retrying**: Use `gluetool.utils.wait` — not custom retry loops.
The check callback must return `Result.Ok(value)` on success or `Result.Error(reason)` to keep retrying.
```python
from gluetool.result import Result

def _check():
    try:
        result = do_something()
    except SomeError as exc:
        return Result.Error('failed: {}'.format(exc))
    return Result.Ok(result)

gluetool.utils.wait(
    'waiting for something',
    _check,
    timeout=120,
    tick=20
)
```

**Exceptions**: Custom errors subclass `gluetool.GlueError` or `gluetool.SoftGlueError`.
Errors related to artifacts should also inherit `ArtifactFingerprintsMixin` from `libs/sentry.py` for Sentry fingerprinting.

**Data classes**: Use `@attrs.define(kw_only=True)` for new data structures.
`@dataclass` (stdlib) also used in some places — match surrounding code.

**`six` still present**: Despite Python 3.12, some files use `six.iteritems()`, `six.ensure_binary()`, etc.
Match existing style in the file you're editing.

**`libs/` directory**: Module-agnostic reusable code only (data structures, utilities, base classes).
Module-specific logic goes in the module file itself.

**License header**: Every `.py` file must start with SPDX license header.
Pre-commit enforces this — if you create a new file, add:
```python
# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0
```

**Entry points**: New modules must be registered in `pyproject.toml` under `[tool.poetry.plugins."gluetool.modules"]`.
Format: `module-name = "gluetool_modules_framework.subpackage.file:ClassName"`

## Code Style

Max line length is 120 characters.

```python
# Docstrings: Sphinx/reST
def provision_guest(self, environment: TestingEnvironment) -> Guest:
    """
    Provision a guest machine.

    :param environment: testing environment specification.
    :returns: provisioned guest instance.
    """

# Type hints required (mypy --strict with --implicit-reexport)
# Use TYPE_CHECKING guard for circular imports:
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from gluetool_modules_framework.libs.guest import Guest
```

## Testing

Tests in `gluetool_modules_framework/tests/test_*.py`.
Assets in `gluetool_modules_framework/tests/assets/<module_name>/`.
Integration tests use `@pytest.mark.integration` (separate tox env, need real infrastructure).
Parallel execution via `pytest-xdist` with `-n auto --dist loadscope`.

**Every test file** must define a `module` fixture:
```python
@pytest.fixture(name='module')
def fixture_module():
    return create_module(MyModule)
```

**Test helpers** (in `gluetool_modules_framework/tests/__init__.py`):
- `create_module(ModuleClass)` — instantiates module for testing, returns `(glue, module)` tuple.
- `patch_shared(monkeypatch, module, {'fn_name': return_value}, callables={'fn_name': callable})` — mocks shared functions.
- `testing_asset('subdir', 'file.yaml')` — resolves path to `tests/assets/`.

**Mocking**: Uses standalone `mock` package (`import mock`), not `from unittest.mock import` directly.
Match existing test style.
Root `conftest.py` provides `mock_command`, `module_with_primary_task`, `root_action` fixtures.

**Test style**
- Use `module._config` to set module settings.
- Use `pytest.mark.parametrize` for similar tests.

## Boundaries

**Always**:
- Run `tox -e py312-unit-tests` and `tox -e type-check` before considering changes complete.
- Add tests for new functionality.
- Match existing patterns in the file you're editing.

**Ask first**:
- Adding new dependencies to `pyproject.toml`.
- Creating new modules (requires entry point registration).
- Modifying CI pipeline (`.gitlab-ci.yml`).

**Never**:
- Commit secrets, tokens, or credentials.
- Modify `poetry.lock` manually — use `poetry lock`.
- Force push to `main`.
- Remove or weaken existing tests.
