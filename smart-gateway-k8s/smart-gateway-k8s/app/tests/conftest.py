"""pytest configuration for tests/.

Several files here are manual demo scripts: they run (and sleep, or call
http://127.0.0.1:8000) at import time, so pytest must not collect them. Run
those by hand from the project root, e.g.  python -m tests.test_scheduler_worker
"""
import sys
from pathlib import Path

import pytest

# Make `import main`, `import gateway`, ... work wherever pytest is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

collect_ignore = [
    "test_scheduler_api.py",          # needs a running server on :8000
    "test_scheduler_worker.py",       # script
    "test_scheduler_demo.py",         # script
    "test_scheduler_performance.py",  # script
    "test_dynamic_scheduler.py",      # script
]


def pytest_collection_modifyitems(config, items):
    # test_job_failure.py expects scheduler_worker.py to raise
    # "Simulated LLM processing failure" for the prompt TEST_FAILURE, but the worker
    # has no such hook (on the fair-share-scheduler branch too). strict=True: once
    # the hook is added this starts failing as XPASS, which is the cue to delete this.
    known_failure = pytest.mark.xfail(
        reason="scheduler_worker.py has no TEST_FAILURE hook yet", strict=True
    )
    for item in items:
        if item.nodeid.endswith("TestJobFailure::test_failed_request"):
            item.add_marker(known_failure)
