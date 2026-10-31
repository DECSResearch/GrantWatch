"""The end-to-end environment keeps a run on this machine, in its own schema, with the fixture's settings."""
from __future__ import annotations

import os
import socket

import pytest

from grants.sql_utils import _DSN_ENV_VARS
from notifications.transport import get_transport
from web import app as webapp


def test_a_connection_beyond_this_machine_is_refused(world):
    with pytest.raises(OSError, match="do not connect outside this machine"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)  # a documentation address: nothing would answer


def test_the_database_is_the_modules_own_schema(world):
    assert [name for name in _DSN_ENV_VARS if os.getenv(name)] == ["POSTGRES_URL"]
    assert f"csearch_path%3D{world.schema}" in os.environ["POSTGRES_URL"]
    assert world.query("SELECT current_schema() AS schema") == [{"schema": world.schema}]


def test_mail_goes_to_the_outbox_and_the_profile_is_the_fixtures(world):
    assert get_transport().name == "outbox"
    assert not any(os.getenv(name) for name in ("GRANTS_SMTP_HOST", "GMAIL_TOKEN_FILE", "GMAIL_TOKEN_JSON"))
    assert webapp._profile().applicant_codes == ["06"] and webapp._profile().institution == "University of Testing"
