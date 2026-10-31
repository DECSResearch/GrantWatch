"""What the end-to-end tests are built from: the dated fixture extract and a fake model server.

No pytest and no database in here, so ``tests/test_e2e_harness.py`` can check these
parts on every run, with or without ``GRANTS_TEST_POSTGRES_URL``.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from datetime import date, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXTRACT = FIXTURES / "extract.xml"
PROFILE = FIXTURES / "profile.yml"

# Every date in the extract is written {{+N}} or {{-N}}: that many days from the day the test runs,
# as Grants.gov writes them (MMDDYYYY). The one literal year, 2076, is the placeholder close date.
_DAY_MARK = re.compile(r"\{\{([+-]\d+)\}\}")
_EXTRA_SLOT = "{{extra}}"

# What the model "thinks" of each fixture grant, by opportunity number. A grant that is not here
# (a number a test adds) gets DEFAULT_SCORE unless the test puts it in FakeLLM.scores.
SCORES: Dict[str, int] = {
    "E2E-TRUSTED": 4,
    "E2E-NOKW": 5,
    "E2E-BULLETS": 5,
    "E2E-ENTITY": 5,
    "E2E-PLACEHOLDER": 5,
    "DURIP-ARMY-26": 3,
    "DURIP-ONR-26": 4,
    "DURIP-AFOSR-26": 4,
    "EXP-2020": 3,
    "EXP-2026": 3,
    "E2E-BROAD": 2,
    "E2E-NOCODES": 3,
    "E2E-FORECAST": 4,
}
DEFAULT_SCORE = 1
MODEL = "fake-model"

# Test-only settings of the environment the suite runs in (see conftest.py).
ADMIN_USER = "e2eadmin"
ADMIN_PASSWORD = "e2e-admin-password"
SECRET_KEY = "e2e-secret-key-0123456789abcdef"
DASHBOARD_URL = "https://grants.example.test"

# The numbers the pipeline must never send to the model: the pre-check scores them 0 itself,
# and a closed grant is dropped before scoring.
NOT_SENT = ("E2E-STATES", "E2E-CLOSED")

_NUMBER_LINE = re.compile(r"^Opportunity number: (.+)$", re.M)


def render_extract(today: date, extra: str = "") -> str:
    """The fixture extract with its day marks worked out from ``today``; ``extra`` goes in before ``</Grants>``.

    ``extra`` may use day marks too.
    """
    text = EXTRACT.read_text(encoding="utf-8").replace(_EXTRA_SLOT, extra)
    return _DAY_MARK.sub(lambda m: (today + timedelta(days=int(m.group(1)))).strftime("%m%d%Y"), text)


def synopsis(number: str, title: str, *, oid: int, close: int, posted: int = -10, codes: str = "06", agency: str = "NSF") -> str:
    """One posted opportunity, in the extract's format, for a test that adds a grant to the fixture."""
    applicants = "".join(f"<EligibleApplicants>{code}</EligibleApplicants>" for code in codes.split(",") if code)
    return (
        "<OpportunitySynopsisDetail_1_0>"
        f"<OpportunityID>{oid}</OpportunityID><OpportunityTitle>{title}</OpportunityTitle>"
        f"<OpportunityNumber>{number}</OpportunityNumber><OpportunityCategory>D</OpportunityCategory>"
        f"<AgencyCode>{agency}</AgencyCode><AgencyName>National Science Foundation</AgencyName>{applicants}"
        f"<PostDate>{{{{{posted:+d}}}}}</PostDate><CloseDate>{{{{{close:+d}}}}}</CloseDate>"
        "<Description>A new program for campus network authentication research.</Description>"
        "</OpportunitySynopsisDetail_1_0>"
    )


def closed_port() -> int:
    """A port nothing is listening on, so a connection to it is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeLLM:
    """An OpenAI-compatible model server on a thread, with scripted scores.

    Answers ``/models`` and ``/chat/completions`` (the SDK adds ``/v1`` itself) and keeps every chat
    request. ``mode`` is ``"ok"``, or ``"truncated"`` for an answer cut off at the token limit
    (``finish_reason`` "length"). A server that is down is just ``stop()``: the port refuses connections.
    Every reply says ``x-should-retry: false`` so a test never waits on the SDK's retries.
    """

    def __init__(self, scores: Optional[Mapping[str, int]] = None, model: str = MODEL):
        self.scores: Dict[str, int] = dict(SCORES if scores is None else scores)
        self.model = model
        self.mode = "ok"
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # keep the test output quiet
                pass

            def _send(self, status: int, body: Dict[str, Any]) -> None:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("x-should-retry", "false")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                if self.path.split("?")[0].endswith("/models"):
                    self._send(200, {"object": "list", "data": [{"id": fake.model, "object": "model", "owned_by": "fake"}]})
                else:
                    self._send(404, {"error": {"message": f"no such route {self.path}"}})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if not self.path.split("?")[0].endswith("/chat/completions"):
                    self._send(404, {"error": {"message": f"no such route {self.path}"}})
                    return
                self._send(200, fake._complete(body))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-llm", daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
        self.mode = "ok"

    def _complete(self, body: Dict[str, Any]) -> Dict[str, Any]:
        messages = {m["role"]: str(m["content"]) for m in body.get("messages", [])}
        user = messages.get("user", "")
        numbers = [n.strip() for n in _NUMBER_LINE.findall(user)]
        with self._lock:
            self.requests.append({"body": body, "system": messages.get("system", ""), "user": user, "numbers": numbers})
            mode = self.mode
        if mode == "truncated":
            content, finish = '{"scores": [{"opportunity_number": "', "length"
        else:
            answers = [
                {
                    "opportunity_number": number,
                    "score": self.scores.get(number, DEFAULT_SCORE),
                    "summary": f"{number} funds the group's research.",
                    "reason": f"{number} fits the group's profile as scripted.",
                }
                for number in numbers
            ]
            content, finish = json.dumps({"scores": answers}), "stop"
        return {
            "id": f"chatcmpl-{len(self.requests)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", self.model),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def sent(self) -> List[str]:
        """Every opportunity number asked about so far, in the order asked."""
        with self._lock:
            return [number for request in self.requests for number in request["numbers"]]

    def request_for(self, number: str) -> Dict[str, Any]:
        """The one chat request that asked about ``number``."""
        with self._lock:
            found = [request for request in self.requests if number in request["numbers"]]
        assert len(found) == 1, f"{number} was in {len(found)} requests, expected 1"
        return found[0]


def read_email(path: Path) -> EmailMessage:
    """An .eml file the outbox wrote, parsed with the modern policy (so ``get_body`` works)."""
    return BytesParser(policy=policy.default).parsebytes(Path(path).read_bytes())


def plain_text(message: EmailMessage) -> str:
    """The decoded plain-text part. Assert on this, never on the raw bytes: they are quoted-printable."""
    return message.get_body(("plain",)).get_content()


_ENTRY = re.compile(r"^- \[([^\]]+)\] (.*)$")


def new_section(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """The email's "New grants that fit" section: its count text ("6 of 7") and its (score, title) lines in order."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("New grants that fit ("))
    count = lines[start].split("(", 1)[1].rstrip(")")
    entries: List[Tuple[str, str]] = []
    for line in lines[start + 1:]:
        found = _ENTRY.match(line)
        if found:
            entries.append((found.group(1), found.group(2)))
        elif line and not line.startswith(" "):
            break  # the next heading or the footer
    return count, entries
