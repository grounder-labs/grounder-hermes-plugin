# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The tests run in a plain clone of the public repo (GRO-609).

A default ``git clone`` puts the plugin in ``grounder-hermes-plugin``. The folder holds an ``__init__.py``, which pytest imports
as a package before any test, and a hyphenated name cannot be imported: every test errored at setup. ``tests/pytest.ini`` makes the
tests folder pytest's root, so ``pytest tests`` collects the same in either folder name. This copies the plugin into a folder named
as a clone's is and runs a test file there.
"""

import shutil
import subprocess
import sys

from conftest import PLUGIN_DIR


def test_the_tests_run_from_a_folder_named_as_a_clone_is(tmp_path):
    clone = tmp_path / "grounder-hermes-plugin"
    shutil.copytree(PLUGIN_DIR, clone, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    run = subprocess.run([sys.executable, "-I", "-m", "pytest", "tests/test_names.py", "-q", "-p", "no:cacheprovider"],
                         cwd=clone, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-1000:]
    assert " passed" in run.stdout and "error" not in run.stdout.lower()
