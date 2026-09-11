"""Score grants against the research profile with an OpenAI-compatible model.

Any server that speaks the chat-completions protocol works: Ollama
(``http://localhost:11434/v1``), vLLM (``http://host:8000/v1``), LM Studio,
or a hosted endpoint. Configuration comes from the environment:

    GRANTS_LLM_BASE_URL          default http://localhost:11434/v1
    GRANTS_LLM_MODEL             required; scoring is skipped when empty
    GRANTS_LLM_API_KEY           default "ollama" (vLLM with --api-key needs the real one)
    GRANTS_LLM_BATCH_SIZE        grants per request, default 4
    GRANTS_LLM_WORKERS           concurrent requests, default 2
    GRANTS_LLM_TIMEOUT           seconds per request, default 180
    GRANTS_LLM_MAX_CHARS         description characters sent per grant, default 4000
    GRANTS_RELEVANCE_MAX_PER_RUN grants scored per run, default 400 (soonest deadlines first)

Local models are not always strict about output formats, so the parser
tolerates reasoning tags, code fences, and partially answered batches; a
grant that comes back unscored is simply tried again on the next run.
"""
from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

from grants.profile import Profile
from grants_data.normalize import strip_html
from logs.status_logger import logger

SCORE_FIELD = "RELEVANCE_SCORE"
REASON_FIELD = "RELEVANCE_REASON"
MODEL_FIELD = "RELEVANCE_MODEL"
PROFILE_FIELD = "RELEVANCE_PROFILE"
SCORED_AT_FIELD = "RELEVANCE_SCORED_AT"

_DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"]

_SYSTEM_PROMPT = """You screen federal funding opportunities for a university research group.

For each opportunity decide how well it fits the group's research profile and
whether the group is an eligible applicant. Use this scale:

5  Squarely in scope. The group could lead a competitive proposal.
4  Strong fit for a major component of the group's work. Worth reading in full.
3  Partial fit. Plausible as a collaborator or with a stretch of the current work.
2  Tangential. Only a minor aspect overlaps.
1  Unrelated topic, although the group would be eligible.
0  Not applicable: wrong applicant type (for example only federal agencies,
   tribes, states, individuals, or foreign governments may apply) or off-topic.

Judge fit from the substance of the description, not from keyword overlap.
Read the eligibility text carefully; a good topic with the wrong eligibility
is a 0.

Respond with JSON only, no prose, in this exact shape:
{"scores": [{"opportunity_number": "...", "score": 0, "summary": "...", "reason": "..."}]}

"summary" is one sentence on what the program funds. "reason" is one sentence
on why it does or does not fit this group. Include every opportunity number
you were given, exactly as written."""

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "opportunity_number": {"type": "string"},
                    "score": {"type": "integer"},
                    "summary": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["opportunity_number", "score", "summary", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}

# Tried in order; a server that rejects one falls through to the next.
_RESPONSE_FORMATS: List[Optional[Dict[str, Any]]] = [
    {"type": "json_schema", "json_schema": {"name": "grant_scores", "schema": _JSON_SCHEMA, "strict": True}},
    {"type": "json_object"},
    None,
]

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(_env(name, str(default))))
    except ValueError:
        return default


@dataclass
class LLMSettings:
    base_url: str = "http://localhost:11434/v1"
    model: str = ""
    api_key: str = "ollama"
    batch_size: int = 4
    workers: int = 2
    timeout: float = 180.0
    max_chars: int = 4000
    max_per_run: int = 400

    @classmethod
    def from_env(cls) -> "LLMSettings":
        return cls(
            base_url=_env("GRANTS_LLM_BASE_URL") or _env("OPENAI_BASE_URL") or cls.base_url,
            model=_env("GRANTS_LLM_MODEL") or _env("OPENAI_MODEL"),
            api_key=_env("GRANTS_LLM_API_KEY") or _env("OPENAI_API_KEY") or cls.api_key,
            batch_size=_env_int("GRANTS_LLM_BATCH_SIZE", cls.batch_size),
            workers=_env_int("GRANTS_LLM_WORKERS", cls.workers),
            timeout=float(_env_int("GRANTS_LLM_TIMEOUT", int(cls.timeout))),
            max_chars=_env_int("GRANTS_LLM_MAX_CHARS", cls.max_chars, minimum=200),
            max_per_run=_env_int("GRANTS_RELEVANCE_MAX_PER_RUN", cls.max_per_run),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.model)


@dataclass
class ScoringOutcome:
    records: List[Dict[str, Any]]
    enabled: bool = False
    scored: int = 0
    cached: int = 0
    failed: int = 0
    deferred: int = 0
    new_entries: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def _parse_close(value: Any) -> datetime:
    if value in (None, ""):
        return datetime.max
    text = str(value)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.max


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [truncated]"


def _record_block(record: Dict[str, Any], max_chars: int) -> str:
    status = str(record.get("OPPORTUNITY_STATUS") or "Posted")
    if status.lower() == "forecasted":
        status = "Forecasted (dates are estimates; no final synopsis yet)"
    lines = [
        f"Opportunity number: {record.get('OPPORTUNITY_NUMBER') or 'unknown'}",
        f"Title: {record.get('OPPORTUNITY_TITLE') or 'untitled'}",
        f"Agency: {record.get('AGENCY') or 'unknown'} ({record.get('AGENCY_CODE') or 'n/a'})",
        f"Status: {status}",
        f"Posted: {record.get('POSTED_DATE') or 'n/a'}   Closes: {record.get('CLOSE_DATE') or 'n/a'}",
    ]
    categories = record.get("FUNDING_CATEGORIES")
    if isinstance(categories, (list, tuple)):
        categories = "; ".join(str(item) for item in categories)
    if categories:
        lines.append(f"Funding categories: {categories}")
    if record.get("AWARD_CEILING"):
        lines.append(f"Award ceiling: {record.get('AWARD_CEILING')}")
    eligibility = strip_html(record.get("ADDITIONAL_INFORMATION_ON_ELIGIBILITY"))
    if eligibility:
        lines.append(f"Eligibility: {_truncate(eligibility, 700)}")
    description = strip_html(record.get("FUNDING_DESCRIPTION")) or "(no description)"
    lines.append(f"Description: {_truncate(description, max_chars)}")
    return "\n".join(lines)


def _extract_json(text: str) -> Any:
    cleaned = _THINK_RE.sub("", text or "").strip()
    fenced = _FENCE_RE.search(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(cleaned[start : end + 1])


def _coerce_score(value: Any) -> Optional[int]:
    try:
        score = int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None
    return max(0, min(5, score))


def parse_scores(text: str, expected: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Map opportunity number -> {score, summary, reason} from model output."""
    data = _extract_json(text)
    items = data.get("scores") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("model output has no 'scores' list")

    lookup = {str(number).strip().upper(): str(number) for number in expected}
    parsed: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("opportunity_number") or "").strip().upper()
        number = lookup.get(key)
        score = _coerce_score(item.get("score"))
        if number is None or score is None:
            continue
        parsed[number] = {
            "score": score,
            "summary": " ".join(str(item.get("summary") or "").split()),
            "reason": " ".join(str(item.get("reason") or "").split()),
        }
    return parsed


class RelevanceScorer:
    """Scores batches of records; safe to call from several threads."""

    def __init__(self, profile: Profile, settings: LLMSettings, client: Any = None):
        self.profile = profile
        self.settings = settings
        self._client = client
        self._format_index = 0
        self._lock = threading.Lock()
        self.abort_reason: Optional[str] = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = OpenAI(
                base_url=self.settings.base_url,
                api_key=self.settings.api_key,
                timeout=self.settings.timeout,
                max_retries=2,
            )
        return self._client

    def _system_prompt(self) -> str:
        return f"{_SYSTEM_PROMPT}\n\nResearch profile:\n{self.profile.as_prompt_text()}"

    def _user_prompt(self, batch: Sequence[Dict[str, Any]]) -> str:
        blocks = "\n\n---\n\n".join(_record_block(record, self.settings.max_chars) for record in batch)
        return f"Score these {len(batch)} opportunities. Return JSON only.\n\n{blocks}"

    def _complete(self, batch: Sequence[Dict[str, Any]]) -> str:
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._user_prompt(batch)},
        ]
        while True:
            with self._lock:
                index = self._format_index
            response_format = _RESPONSE_FORMATS[index]
            kwargs: Dict[str, Any] = {
                "model": self.settings.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": 300 * len(batch) + 200,
            }
            if response_format is not None:
                kwargs["response_format"] = response_format
            try:
                response = self.client.chat.completions.create(**kwargs)
            except BadRequestError as exc:
                if index + 1 >= len(_RESPONSE_FORMATS):
                    raise
                with self._lock:
                    if self._format_index == index:
                        self._format_index = index + 1
                        logger("warning", f"Model rejected response_format {response_format!r}; falling back ({exc.message[:120]})")
                continue
            return response.choices[0].message.content or ""

    def score_batch(self, batch: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        expected = [str(record.get("OPPORTUNITY_NUMBER")) for record in batch]
        text = self._complete(batch)
        return parse_scores(text, expected)


def score_records(
    records: Sequence[Dict[str, Any]],
    profile: Profile,
    *,
    already_scored: Optional[Dict[str, Dict[str, Any]]] = None,
    settings: Optional[LLMSettings] = None,
    client: Any = None,
) -> ScoringOutcome:
    """Attach relevance fields to ``records``; returns copies, never mutates."""
    settings = settings or LLMSettings.from_env()
    output = [dict(record) for record in records]
    outcome = ScoringOutcome(records=output)

    if not profile.is_configured:
        logger("warning", "Research profile summary is not filled in; relevance scoring skipped")
        return outcome
    if not settings.enabled:
        logger("warning", "GRANTS_LLM_MODEL is not set; relevance scoring skipped")
        return outcome
    outcome.enabled = True

    fingerprint = profile.fingerprint()
    cached = already_scored or {}
    pending: List[Dict[str, Any]] = []
    for record in output:
        number = str(record.get("OPPORTUNITY_NUMBER") or "")
        hit = cached.get(number)
        if hit and hit.get("score") is not None:
            _apply(record, hit, fingerprint, hit.get("model") or settings.model, hit.get("scored_at"))
            outcome.cached += 1
        elif number:
            pending.append(record)

    pending.sort(key=lambda record: (_parse_close(record.get("CLOSE_DATE")), str(record.get("OPPORTUNITY_NUMBER"))))
    if len(pending) > settings.max_per_run:
        outcome.deferred = len(pending) - settings.max_per_run
        pending = pending[: settings.max_per_run]
        logger("warning", f"Scoring {settings.max_per_run} of {len(pending) + outcome.deferred} unscored grants this run; the rest wait for the next run")

    if not pending:
        logger("info", f"Relevance scoring: nothing new to score ({outcome.cached} cached)")
        return outcome

    scorer = RelevanceScorer(profile, settings, client=client)
    batches = [pending[i : i + settings.batch_size] for i in range(0, len(pending), settings.batch_size)]
    by_number = {str(record.get("OPPORTUNITY_NUMBER")): record for record in pending}
    scored_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def run(batch: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        if scorer.abort_reason:
            return {}
        try:
            return scorer.score_batch(batch)
        except AuthenticationError as exc:
            scorer.abort_reason = f"authentication failed: {exc.message}"
        except APIConnectionError as exc:
            scorer.abort_reason = f"cannot reach {settings.base_url}: {exc}"
        except RateLimitError as exc:
            logger("warning", f"Rate limited while scoring a batch; skipping it ({exc.message[:120]})")
        except APIStatusError as exc:
            logger("warning", f"Model server returned {exc.status_code} for a batch; skipping it")
        except (ValueError, json.JSONDecodeError) as exc:
            logger("warning", f"Could not parse model output for a batch; skipping it ({exc})")
        return {}

    with ThreadPoolExecutor(max_workers=max(1, settings.workers)) as pool:
        futures = [pool.submit(run, batch) for batch in batches]
        for future in as_completed(futures):
            for number, result in future.result().items():
                record = by_number.get(number)
                if record is None:
                    continue
                _apply(record, result, fingerprint, settings.model, scored_at)
                outcome.new_entries[number] = {**result, "model": settings.model, "scored_at": scored_at}
                outcome.scored += 1

    if scorer.abort_reason:
        logger("error", f"Relevance scoring stopped: {scorer.abort_reason}")
    outcome.failed = len(pending) - outcome.scored
    logger(
        "info",
        f"Relevance scoring with {settings.model}: scored {outcome.scored}, cached {outcome.cached}, "
        f"unscored {outcome.failed}, deferred {outcome.deferred}",
    )
    return outcome


def _apply(record: Dict[str, Any], result: Dict[str, Any], fingerprint: str, model: str, scored_at: Any) -> None:
    record[SCORE_FIELD] = result.get("score")
    record[REASON_FIELD] = result.get("reason") or ""
    record[MODEL_FIELD] = model
    record[PROFILE_FIELD] = fingerprint
    record[SCORED_AT_FIELD] = scored_at
    summary = (result.get("summary") or "").strip()
    if summary:
        record["SUMMARY"] = summary
