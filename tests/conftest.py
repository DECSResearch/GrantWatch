"""Shared pytest configuration."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest
from hypothesis import settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Property tests run 300 simulated scenarios by default; for a deeper sweep
# run `HYPOTHESIS_PROFILE=long pytest tests/test_planner_properties.py`.
settings.register_profile("default", max_examples=300)
settings.register_profile("long", max_examples=2000)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture(scope="session", autouse=True)
def isolate_pipeline_log(tmp_path_factory):
    """Keep test output out of logs/grantwatch.log.

    The pipeline logger attaches a file handler to the real log at import
    time, so without this every pytest run appends its fixtures' warnings and
    deliberate error paths to the production log.
    """
    pipeline_logger = logging.getLogger("grantwatch")
    original = [h for h in pipeline_logger.handlers if isinstance(h, logging.FileHandler)]
    for handler in original:
        pipeline_logger.removeHandler(handler)
        handler.close()

    test_log = tmp_path_factory.mktemp("logs") / "grantwatch.log"
    handler = logging.FileHandler(test_log, encoding="utf-8")
    if original:
        handler.setFormatter(original[0].formatter)
    pipeline_logger.addHandler(handler)

    yield

    pipeline_logger.removeHandler(handler)
    handler.close()
