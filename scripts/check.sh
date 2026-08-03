#!/bin/sh
set -eu

# Keep the production compatibility checks on the default Python (3.9+), while
# allowing tests to run from a project venv or another interpreter that already
# has the development-only pytest dependency installed.
CHECK_PYTHON=${CAMERA_DEBUG_CHECK_PYTHON:-python3}
"$CHECK_PYTHON" -c 'import sys; assert sys.version_info >= (3, 9), "Camera Debug Studio requires Python 3.9+"'
PYTHON_FILES=$(find . -type f -name '*.py' \
    ! -path './.git/*' \
    ! -path './.venv/*' \
    ! -path './test/.venv/*' \
    ! -path '*/__pycache__/*' | sort)
# Intentional word splitting: py_compile accepts the newline-separated list as
# individual paths, and project paths must not contain whitespace.
# shellcheck disable=SC2086
"$CHECK_PYTHON" -m py_compile $PYTHON_FILES
"$CHECK_PYTHON" -c 'import json,pathlib; [json.loads(p.read_text(encoding="utf-8")) for p in pathlib.Path("configs/profiles").glob("*/*.json")]'
"$CHECK_PYTHON" -c 'import camera_debug as app; [app.Runtime.validate_config(app.load_profile(path)) for path in sorted(app.PROFILE_DIR.iterdir()) if path.is_dir()]'

if command -v node >/dev/null 2>&1; then
    find web -type f -name '*.js' -print | sort | while IFS= read -r javascript; do
        node --check "$javascript"
    done
else
    echo "node is not installed; JavaScript syntax check skipped" >&2
fi

find_pytest_python() {
    if [ -n "${CAMERA_DEBUG_PYTEST_PYTHON:-}" ]; then
        printf '%s\n' "$CAMERA_DEBUG_PYTEST_PYTHON"
        return
    fi
    for candidate in \
        "./test/.venv/bin/python" \
        "./.venv/bin/python" \
        "python3" \
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
    do
        if "$candidate" -c 'import pytest' >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return
        fi
    done
    return 1
}

if PYTEST_PYTHON=$(find_pytest_python); then
    echo "Running tests with $PYTEST_PYTHON"
    "$PYTEST_PYTHON" -m pytest -q
else
    echo "pytest is not installed. Run: python3 -m pip install -r requirements-dev.txt" >&2
    exit 1
fi
