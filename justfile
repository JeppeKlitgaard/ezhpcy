set minimum-version := '1.55.0'
set default-list := true
set dotenv-load := true
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# Run all checks
all:
    just format
    just lint
    just test
    just pc-all

# Installs the project dependencies and pre-commit hooks
[group: 'dev']
install:
    uv sync
    uv run prek install

# Override EzHPCy config with default. Works best with a .dotenv containing EZHPCY_USER
override-config:
    uv run ezhpcy config load --yes DTU


# Formats the code using ruff
[group: 'format']
format:
    uv run ruff check . --fix
    uv run ruff format .

# Checks the code for linting errors
[group: 'format']
lint:
    uv run ruff check .

# Runs the pre-commit hooks on all files
[group: 'format']
pc-all:
    uv run prek run --all-files

# Runs the pre-commit hooks on the changed files
[group: 'format']
pc:
    uv run prek run

# Runs the test suite
[group: 'test']
test *args:
    uv run pytest {{args}}

# Runs the test suite and reports coverage; `just coverage --cov-report=html` for htmlcov/
[group: 'test']
coverage *args:
    uv run pytest --cov --cov-report=term-missing {{args}}
