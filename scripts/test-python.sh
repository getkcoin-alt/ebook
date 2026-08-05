#!/usr/bin/env bash
#
# Run every Python test suite in the monorepo.
#
# **One pytest process per service, deliberately.** A single run from the repository
# root cannot work with this layout and never could: every service defines top-level
# `models`, `schemas`, `settings`, `services`, `routers` and `tests` modules. Python
# caches imports by name, so the first service to import `models` wins and every
# service collected after it silently gets somebody else's tables — when pytest gets
# that far at all, which it does not: two `tests/conftest.py` files under the same
# rootdir raise `ImportPathMismatchError` during collection.
#
# That is not a flaw in the layout. Each service is an independent deployable with its
# own `PYTHONPATH`, which is exactly how it runs in its container — so running its
# tests the same way is the honest thing to do, and it also means one service's
# failures cannot be caused by another's imports.
#
# Coverage is reported **per service**, not combined. Combining is tempting and wrong
# here: every service has a file called `models.py`, so a merged report would fold
# seven unrelated modules into one line and produce a number that describes nothing.
#
# Usage:
#   scripts/test-python.sh              # everything
#   scripts/test-python.sh books ai     # named services only
#   COVERAGE=0 scripts/test-python.sh   # skip coverage (much faster locally)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"
COVERAGE="${COVERAGE:-1}"

if [ "$#" -gt 0 ]; then
    TARGETS=("$@")
else
    TARGETS=(packages/core-py)
    for dir in apps/*/; do
        [ -d "${dir}tests" ] && TARGETS+=("${dir%/}")
    done
fi

rm -f "$ROOT"/.coverage "$ROOT"/.coverage.* "$ROOT"/junit-*.xml "$ROOT"/coverage-*.xml

failed=()
passed=0

for target in "${TARGETS[@]}"; do
    # Accept a bare service name as well as a path, so `test-python.sh books` works.
    if [ ! -d "$target" ]; then
        if [ -d "apps/$target" ]; then
            target="apps/$target"
        elif [ -d "packages/$target" ]; then
            target="packages/$target"
        else
            echo "::error::No such service or package: $target"
            failed+=("$target")
            continue
        fi
    fi

    name="$(basename "$target")"
    echo ""
    echo "──────────────────────────────────────────────────────────────"
    echo "  $name"
    echo "──────────────────────────────────────────────────────────────"

    args=(-p no:cacheprovider "--junitxml=$ROOT/junit-$name.xml" -o junit_family=legacy)
    if [ "$COVERAGE" = "1" ]; then
        # Measured from the service directory, so `models.py` in the report is
        # unambiguously *this* service's, and written to its own file.
        args+=(--cov=. --cov-branch "--cov-report=xml:$ROOT/coverage-$name.xml")
        args+=(--cov-report=term:skip-covered)
    fi

    # `rootdir` pinned to the service so its `tests/conftest.py` is the only one in
    # scope, and `PYTHONPATH` set to the service so `import models` resolves the way
    # it does inside that service's container.
    if (
        cd "$target" &&
        PYTHONPATH="$ROOT/$target" \
        COVERAGE_FILE="$ROOT/.coverage.$name" \
        KOS_SERVICE_TEST_RUN=1 \
        "$PYTHON" -m pytest tests/ "${args[@]}"
    ); then
        passed=$((passed + 1))
    else
        failed+=("$name")
    fi
done

echo ""
if [ "${#failed[@]}" -gt 0 ]; then
    echo "FAILED: ${failed[*]}"
    echo "$passed suite(s) passed, ${#failed[@]} failed."
    exit 1
fi

echo "All $passed suite(s) passed."
