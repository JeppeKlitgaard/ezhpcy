set minimum-version := '1.55.0'
set default-list := true
set dotenv-load := true
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# Run all checks
all:
    just format
    just lint
    just lint-imports
    just check
    just test
    just pc-all

# Installs the project dependencies and pre-commit hooks
[group: 'dev']
install:
    uv sync
    uv run --group pre-commit prek install

# Override EzHPCy config with default. Works best with a .dotenv containing EZHPCY_USER
override-config:
    uv run ezhpcy config load --yes DTU


# Formats the code using ruff
[group: 'lint']
format:
    uv run --group lint ruff check . --fix
    uv run --group lint ruff format .

# Checks the code for linting errors
[group: 'lint']
lint:
    uv run --group lint ruff check .

# Checks the import contracts
[group: 'lint']
lint-imports:
    uv run --group lint lint-imports

# Runs the pre-commit hooks on all files
[group: 'lint']
pc-all:
    uv run --group pre-commit prek run --all-files

# Runs the pre-commit hooks on the changed files
[group: 'lint']
pc:
    uv run --group pre-commit prek run

# Runs the test suite in parallel; `just test -n0` for one process
[group: 'test']
test *args:
    uv run --group test pytest -n auto {{args}}

# Runs the test suite and reports coverage; `just coverage --cov-report=html` for htmlcov/
[group: 'test']
coverage *args:
    uv run --group test pytest -n auto --cov --cov-report=term-missing {{args}}

# Runs the type checker
[group: 'typecheck']
check:
    uv run --group typecheck pyrefly check

# Counts the lines of code
[group: 'dev']
sloc:
    @echo "src/ezhpcy"
    @uvx pygount --format=summary src/ezhpcy
    @echo "tests"
    @uvx pygount --format=summary tests

# Bumps the version (major, minor, patch, stable, alpha, beta, rc, post or dev; several combine) and commits it
[group: 'release']
bump +bumps:
    git diff --exit-code HEAD -- pyproject.toml uv.lock
    uv version {{ prepend("--bump=", bumps) }}
    git commit --message "chore(version): release $(uv version --short)" -- pyproject.toml uv.lock

# Tags HEAD on master as v<version in pyproject.toml> and pushes the tag, which publishes it to PyPI
[group: 'release']
[confirm("Tag HEAD as a release and push the tag? This publishes it to PyPI. (N/y): ")]
tag-release:
    {{ if trim(`git branch --show-current`) != "master" { error("Releases are tagged on master.") } else { "" } }}
    uv lock --check
    git diff --exit-code HEAD -- uv.lock
    just pc-all
    git tag --annotate v{{ trim(`uv version --short`) }} --message "v{{ trim(`uv version --short`) }}"
    git push origin v{{ trim(`uv version --short`) }}
