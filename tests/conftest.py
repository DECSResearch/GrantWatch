"""Shared pytest configuration."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
