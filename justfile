set minimum-version := '1.55.0'
set default-list := true

# Installs the project dependencies
[group: 'dev']
install:
    uv sync --all-extras

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
test:
    uv run pytest
