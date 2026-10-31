"""The end-to-end suite's world: a real Postgres schema, a fake model server and an isolated environment.

Skipped unless GRANTS_TEST_POSTGRES_URL is set. It must be a URI
(``postgresql://user:password@host:5432/dbname``), because each test module's schema is chosen with
``?options=-csearch_path=<schema>``. Every module gets a schema of its own and drops it afterwards, so
the suite leaves nothing in the database it is pointed at.

Nothing in the run reaches past this machine: the Grants.gov download is replaced by the fixture
extract, the model is a server on 127.0.0.1, mail goes to a folder, and a guard refuses any other
outbound connection.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import main  # noqa: E402
from grants.sql_utils import _DSN_ENV_VARS, db_connection  # noqa: E402
from grants_data import pipeline  # noqa: E402
from harness import ADMIN_PASSWORD, ADMIN_USER, DASHBOARD_URL, MODEL, PROFILE, SECRET_KEY, FakeLLM, render_extract  # noqa: E402
from llm_utils import relevance_cache  # noqa: E402
from web import admin_auth  # noqa: E402
from web import app as webapp  # noqa: E402

# Everything the code reads from the environment that a developer's shell or .env could set. Cleared
# first, so what a test sees is only what this file sets.
_CLEARED = _DSN_ENV_VARS + (
    "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE", "PGOPTIONS",
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
    "POSTGRES_SSLMODE", "POSTGRES_SSL_DOMAIN", "POSTGRES_CONNECT_TIMEOUT",
    "GRANTS_LLM_BASE_URL", "GRANTS_LLM_MODEL", "GRANTS_LLM_API_KEY", "GRANTS_LLM_BATCH_SIZE", "GRANTS_LLM_WORKERS",
    "GRANTS_LLM_TIMEOUT", "GRANTS_LLM_MAX_CHARS", "GRANTS_LLM_MAX_TOKENS", "GRANTS_LLM_EXTRA_BODY",
    "GRANTS_RELEVANCE_MAX_PER_RUN", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY",
    "GRANTS_PROFILE_SUMMARY", "GRANTS_PROFILE_INSTITUTION", "GRANTS_KEYWORDS", "GRANTS_KEYWORD_THRESHOLD",
    "GRANTS_INCLUDE_FORECAST", "GRANTS_MIN_SCORE", "GRANTS_OVERVIEW_DAY",
    "GRANTS_DATA_SOURCE", "GRANTS_GOV_LOOKBACK_DAYS", "GRANTS_KEEP_ARTIFACTS",
    "GRANTS_SMTP_HOST", "GRANTS_SMTP_PORT", "GRANTS_SMTP_SECURITY", "GRANTS_SMTP_USER", "GRANTS_SMTP_PASSWORD",
    "GMAIL_TOKEN_FILE", "GMAIL_TOKEN_JSON", "GMAIL_SENDER_EMAIL", "GMAIL_NOTIFY_RECIPIENTS",
    "GRANTS_NOTIFY_RECIPIENTS", "GRANTS_EMAIL_MAX_NEW", "GRANTS_TIMEZONE",
    # A proxy would send the calls to the fake server somewhere else.
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
)
_LOCAL = {"127.0.0.1", "::1", "localhost"}


def with_schema(url: str, schema: str) -> str:
    """``url`` with every connection's search_path set to ``schema``."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _only_local_sockets(mp: pytest.MonkeyPatch) -> None:
    """Refuse any connect() to an address that is not this machine. Postgres is reached by libpq, not here."""

    def guarded(real: Any) -> Any:
        def connect(self: socket.socket, address: Any) -> Any:
            host = address[0] if isinstance(address, tuple) else None  # a unix socket path is a str or bytes
            if host is not None and host not in _LOCAL:
                raise OSError(f"the end-to-end tests do not connect outside this machine (tried {host})")
            return real(self, address)

        return connect

    mp.setattr(socket.socket, "connect", guarded(socket.socket.connect))
    mp.setattr(socket.socket, "connect_ex", guarded(socket.socket.connect_ex))


class World:
    """One test module's schema, scripted model, folders and the pieces of the real program to drive."""

    def __init__(self, tmp: Path, llm: FakeLLM, schema: str, mp: pytest.MonkeyPatch):
        self.tmp, self.llm, self.schema, self.mp = tmp, llm, schema, mp
        self.today = date.today()
        self.extra = ""  # XML that gen_extract adds to the fixture extract (see harness.render_extract)
        self.outbox = tmp / "outbox"

    def ingest(self) -> int:
        return main.main(["ingest"])

    def notify(self, *flags: str) -> int:
        code = main.main(["notify", *flags])
        # The outbox names a file by the second and the message's number in its run, so two runs in one
        # second would overwrite each other's mail. Number what this run wrote before the next one.
        for path in sorted(self.outbox.glob("2*.eml")):
            path.rename(path.with_name(f"mail-{len(list(self.outbox.glob('mail-*.eml'))) + 1:03d}-{path.name}"))
        return code

    def query(self, sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
        with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()] if cur.description else []

    def emails(self) -> List[Path]:
        return sorted(self.outbox.glob("*.eml"))

    def reset(self) -> None:
        """Back to an empty database, cache, outbox and folder of CSVs, and a model that answers."""
        tables = [row["tablename"] for row in self.query("SELECT tablename FROM pg_tables WHERE schemaname = %s", (self.schema,))]
        if tables:
            self.query(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        for folder in (self.outbox, self.tmp / "csv"):
            shutil.rmtree(folder, ignore_errors=True)
        (self.tmp / "relevance_cache.json").unlink(missing_ok=True)
        self.extra = ""
        self.llm.reset()


@pytest.fixture(scope="session")
def postgres_url() -> str:
    url = os.getenv("GRANTS_TEST_POSTGRES_URL", "").strip()
    if not url:
        pytest.skip("GRANTS_TEST_POSTGRES_URL is not set; the end-to-end tests need a real Postgres")
    return url


@pytest.fixture(scope="module")
def fake_llm() -> Iterator[FakeLLM]:
    llm = FakeLLM()
    try:
        yield llm
    finally:
        llm.stop()


@pytest.fixture(scope="module")
def world(postgres_url: str, fake_llm: FakeLLM, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    tmp = tmp_path_factory.mktemp("e2e")
    schema = f"e2e_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(postgres_url)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
    try:
        with pytest.MonkeyPatch.context() as mp:
            _only_local_sockets(mp)
            for name in _CLEARED:
                mp.delenv(name, raising=False)
            for name, value in {
                "POSTGRES_URL": with_schema(postgres_url, schema),
                "GRANTS_PROFILE_FILE": str(PROFILE),
                "GRANTS_EMAIL_TRANSPORT": "outbox",
                "GRANTS_EMAIL_OUTBOX": str(tmp / "outbox"),
                "GRANTS_EMAIL_FROM": "GrantWatch <grantwatch@example.test>",
                "GRANTS_DASHBOARD_URL": DASHBOARD_URL,
                "GRANTS_SECRET_KEY": SECRET_KEY,
                "GRANTS_LLM_BASE_URL": fake_llm.base_url,
                "GRANTS_LLM_MODEL": MODEL,
                "GRANTS_LLM_API_KEY": "e2e-test-key",
                "GRANTS_LLM_WORKERS": "1",  # one request at a time, so the order of requests is the order of batches
                "GRANTS_LLM_TIMEOUT": "5",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }.items():
                mp.setenv(name, value)
            mp.setattr(admin_auth, "SCRYPT_N", 2**4)  # the real cost is for the server, not the tests
            mp.setenv("GRANTS_ADMIN_USERS", f"{ADMIN_USER}:{admin_auth.hash_password(ADMIN_PASSWORD)}")

            this = World(tmp, fake_llm, schema, mp)

            def gen_extract(keep_zip: bool = False) -> bool:
                path = tmp / "GrantsDBExtract_e2e.xml"
                path.write_text(render_extract(this.today, this.extra), encoding="utf-8")
                gen_extract.last_extract_path = path  # type: ignore[attr-defined]
                return True

            gen_extract.last_extract_path = None  # type: ignore[attr-defined]
            mp.setattr(pipeline, "gen_extract", gen_extract)
            mp.setattr(relevance_cache, "_CACHE_PATH", tmp / "relevance_cache.json")
            mp.setattr(pipeline, "_CSV_DIR", tmp / "csv")
            webapp._profile.cache_clear()  # the web app reads the profile once; this one is the fixture's
            try:
                yield this
            finally:
                webapp._profile.cache_clear()  # later modules read their own
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.close()


@pytest.fixture
def fresh(world: World) -> World:
    """``world`` emptied first, for a test that starts from nothing."""
    world.reset()
    return world
