"""Render a plan (see planner.py) as an email in plain text and HTML.

Pure functions, like the planner. The email is trimmed until it fits under
``settings.max_bytes`` so Gmail never clips it; whatever does not fit is left
out of ``shown`` and is therefore not recorded, so it goes out on the next run.
"""
from __future__ import annotations

import html
import quopri
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from grants.display import COUNTDOWN_DAYS, firm_deadline
from notifications.planner import (
    CHANGED,
    OVERVIEW,
    REMINDER,
    REMOVED,
    URGENT_DAYS,
    GrantState,
    Notice,
    NotifySettings,
    Plan,
    SentRecord,
)

_FONT = "Public Sans,Helvetica,Arial,sans-serif"
_MAX_TITLE = 200
_MAX_DETAIL = 300
_MAX_AGENCY = 120
_MAX_PROFILE_LINE = 200
_UNTRIMMED = 1000  # starting cap for sections that are small in practice


@dataclass
class RenderedEmail:
    subject: str
    text: str
    html: str
    shown: List[Notice] = field(default_factory=list)
    overview_shown: bool = False
    unsubscribe_url: str = ""

    @property
    def size(self) -> int:
        """Bytes on the wire for both parts, quoted-printable encoded as transport.build_message sends them."""
        return sum(len(quopri.encodestring(part.encode("utf-8"))) for part in (self.text, self.html))

    def records(self, recipient_id: int, today: date) -> List[SentRecord]:
        """Rows to write once this email has been sent."""
        rows = [row for notice in self.shown for row in notice.records(recipient_id)]
        if self.overview_shown:
            rows.append(SentRecord(recipient_id, "", OVERVIEW, -1, today))
        return rows


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fmt_date(value: Optional[date]) -> str:
    return value.strftime("%b %d, %Y") if value else "no date"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _score_text(score: Optional[int]) -> str:
    return f"{score}/5" if score is not None else "unscored"


def _deadline_text(g: GrantState, today: date) -> str:
    if g.close_date is None:
        return "no deadline listed"
    if not g.firm_deadline(today):
        return "no firm deadline"
    days = g.days_left(today)
    when = f"closes {_fmt_date(g.close_date)}"
    if days is not None and 0 <= days <= COUNTDOWN_DAYS:
        when += f" ({_plural(days, 'day')} left{', estimated' if g.forecasted else ''})"
    elif g.forecasted:
        when += " (estimated)"
    return when


def _meta(notice_kind: str, g: GrantState, today: date, previous: Optional[date] = None) -> str:
    parts = [_clip(g.agency, _MAX_AGENCY) or "Unknown agency"]
    if notice_kind == CHANGED:
        # A placeholder date is not a fact on either side of the move.
        was_firm, is_firm = firm_deadline(previous, today), g.firm_deadline(today)
        if was_firm and is_firm:
            parts.append(f"deadline moved from {_fmt_date(previous)} to {_fmt_date(g.close_date)}")
        elif is_firm:
            parts.append(f"now closes {_fmt_date(g.close_date)}")
        elif was_firm:
            parts.extend([f"deadline moved from {_fmt_date(previous)}", "no firm deadline now"])
        else:
            parts.append("still no firm deadline")
        days = g.days_left(today)
        if days is not None and 0 <= days <= COUNTDOWN_DAYS:
            parts.append(f"{_plural(days, 'day')} left")
    elif notice_kind == REMOVED:
        if g.archived(today):
            before = f", before its {_fmt_date(g.close_date)} deadline" if g.firm_deadline(today) else ""
            parts.append(f"archived on {_fmt_date(g.archive_date)}{before}")
        else:
            parts.append("no longer listed on Grants.gov")
    else:
        parts.append(_deadline_text(g, today))
    if g.watched and notice_kind not in (REMINDER, CHANGED, REMOVED):
        parts.append("tracked")
    return "; ".join(parts)


@dataclass
class _Entry:
    grant: GrantState
    meta: str


@dataclass
class _Section:
    key: str
    title: str
    entries: List[_Entry]
    total: int
    more: str = ""

    @property
    def count(self) -> str:
        """"25 of 35" when some are held back, else the number listed."""
        listed = len(self.entries)
        return f"{listed} of {self.total}" if self.total > listed else str(listed)


def _also_posted(g: GrantState) -> List[Tuple[str, str]]:
    """(agency, link) of the other services' postings of this program."""
    return [(_clip(other.agency, _MAX_AGENCY) or "another agency", other.url) for other in g.also]


def _text_entry(entry: _Entry) -> List[str]:
    g = entry.grant
    lines = [f"- [{_score_text(g.score)}] {_clip(g.title, _MAX_TITLE)}", f"  {entry.meta}"]
    if g.also:
        lines.append("  Also posted by: " + ", ".join(f"{name} ({url})" if url else name for name, url in _also_posted(g)))
    for detail in (_clip(g.summary, _MAX_DETAIL), _clip(g.reason, _MAX_DETAIL)):
        if detail:
            lines.append(f"  {detail}")
    if g.url:
        lines.append(f"  {g.url}")
    lines.append("")
    return lines


def _urgency(g: GrantState, today: date) -> str:
    days = g.days_left(today)
    if days is not None and 0 <= days <= 7:
        return "#8a1f1a"
    if days is not None and 0 <= days <= 30:
        return "#7a4e00"
    return "#5b6660"


def _html_entry(entry: _Entry, today: date) -> str:
    g = entry.grant
    title = html.escape(_clip(g.title, _MAX_TITLE))
    if g.url:
        title = f'<a href="{html.escape(g.url, quote=True)}" style="color:#1e5a44;text-decoration:none">{title}</a>'
    summary = html.escape(_clip(g.summary, _MAX_DETAIL))
    reason = html.escape(_clip(g.reason, _MAX_DETAIL))
    also = ", ".join(_link(url, name) if url else html.escape(name) for name, url in _also_posted(g))
    return (
        '<tr><td style="padding:9px 0;border-top:1px solid #d6dbd7">'
        f'<div style="font-weight:600">{title}</div>'
        f'<div style="font-size:13px;color:{_urgency(g, today)}">{html.escape(entry.meta)}</div>'
        + (f'<div style="font-size:13px;color:#5b6660">Also posted by: {also}</div>' if also else "")
        + (f'<div style="font-size:13px;color:#5b6660">{summary}</div>' if summary else "")
        + f'<div style="font-size:14px;color:#3b4642">{html.escape(_score_text(g.score))}'
        + (f" &#183; {reason}" if reason else "")
        + "</div></td></tr>"
    )


def _link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}" style="color:#1e5a44">{html.escape(label)}</a>'


def _build(plan: Plan, settings: NotifySettings, today: date, caps: Dict[str, int]) -> Tuple[List[_Section], List[Notice], bool]:
    shown: List[Notice] = []
    sections: List[_Section] = []
    dashboard = settings.dashboard_url.rstrip("/")
    in_dashboard = " in the dashboard" if dashboard else ""

    changes = plan.changes[: caps["changes"]]
    changed_ids = {n.grant.opp_id for n in changes}
    # A reminder for a grant whose deadline just moved rides on the change notice.
    folded = [n for n in plan.reminders if n.grant.opp_id in changed_ids]
    reminders = [n for n in plan.reminders if n.grant.opp_id not in changed_ids][: caps["reminders"]]
    removed = plan.removed[: caps["removed"]]
    new = plan.new[: caps["new"]]
    shown.extend(reminders + changes + folded + removed + new)

    if reminders:
        sections.append(_Section("reminders", "Deadline reminders", [_Entry(n.grant, _meta(REMINDER, n.grant, today)) for n in reminders], len(reminders)))
    if changes:
        sections.append(_Section("changes", "Deadlines that moved", [_Entry(n.grant, _meta(CHANGED, n.grant, today, n.previous_date)) for n in changes], len(changes)))
    if removed:
        sections.append(_Section("removed", "Tracked grants no longer open", [_Entry(n.grant, _meta(REMOVED, n.grant, today)) for n in removed], len(removed)))
    if new:
        more = ""
        backlog = len(plan.new) - len(new)
        if backlog:
            more = f"{_plural(backlog, 'more grant')} qualify and will follow in the next emails."
        sections.append(_Section("new", "New grants that fit", [_Entry(n.grant, _meta("new", n.grant, today)) for n in new], len(plan.new), more))

    above = {g.opp_id for n in shown for g in (n.grant, *n.grant.also)}
    # The overview counts as delivered unless trimming dropped a whole bucket, or
    # there was nothing to say; grants already listed above are not repeated in it.
    overview_shown = bool(plan.overview)
    listed = False
    for index, (label, members) in enumerate(plan.overview):
        rest = [g for g in members if not any(m.opp_id in above for m in (g, *g.also))]
        if not rest:
            continue
        cap = caps.get(f"overview{index}", 0)
        visible = rest[:cap]
        if not visible:
            overview_shown = False
            continue
        listed = True
        hidden = len(rest) - len(visible)
        sections.append(
            _Section(
                f"overview{index}", label, [_Entry(g, _meta("overview", g, today)) for g in visible], len(rest),
                f"and {hidden} more{in_dashboard}." if hidden else "",
            )
        )
    if overview_shown and not listed:
        sections.append(_Section("overview-note", "", [], 0, "This week's deadlines are all in the list above."))
    return sections, shown, overview_shown


def _subject(plan: Plan, shown: Sequence[Notice], sections: Sequence[_Section], settings: NotifySettings) -> str:
    counts: Dict[str, int] = {}
    moved = {n.grant.opp_id for n in shown if n.kind == CHANGED}
    for notice in shown:
        if notice.kind == REMINDER and notice.grant.opp_id in moved:
            continue  # listed once, under the deadline change
        counts[notice.kind] = counts.get(notice.kind, 0) + 1
    parts = []
    if counts.get("new"):
        parts.append(f"{counts['new']} new")
    if counts.get(REMINDER):
        parts.append(_plural(counts[REMINDER], "deadline reminder"))
    if counts.get(CHANGED):
        parts.append(_plural(counts[CHANGED], "deadline change"))
    if counts.get(REMOVED):
        parts.append(f"{counts[REMOVED]} withdrawn")
    if not parts:
        upcoming = sum(s.total for s in sections if s.key.startswith("overview"))
        horizon = max([int(h) for h in settings.horizons if int(h) > 0] or [90])
        parts.append(f"the week ahead, {_plural(upcoming, 'deadline')} in the next {horizon} days")
    return "GrantWatch: " + ", ".join(parts)


def _compose(plan: Plan, settings: NotifySettings, today: date, caps: Dict[str, int]) -> Optional[RenderedEmail]:
    sections, shown, overview_shown = _build(plan, settings, today, caps)
    if not sections:
        return None
    dashboard = settings.dashboard_url.rstrip("/")
    token = plan.recipient.unsubscribe_token
    unsubscribe = f"{dashboard}/unsubscribe?token={token}" if dashboard and token else ""
    day = today.strftime("%A, %b %d, %Y")
    profile_line = _clip(settings.profile_line, _MAX_PROFILE_LINE)

    text: List[str] = [f"GrantWatch for {day}"]
    if profile_line:
        text.append(profile_line)
    if dashboard:
        text.append(f"Dashboard: {dashboard}")
    text.append("")
    parts: List[str] = []
    for section in sections:
        heading = table = ""
        if section.entries:
            text.append(f"{section.title} ({section.count})")
            text.append("")
            for entry in section.entries:
                text.extend(_text_entry(entry))
            heading = (
                f'<h2 style="font-size:17px;font-weight:600;color:#1e5a44;margin:26px 0 4px">{html.escape(section.title)}'
                f' <span style="color:#5b6660;font-weight:400">({section.count})</span></h2>'
            )
            rows = "".join(_html_entry(entry, today) for entry in section.entries)
            table = f'<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:15px;line-height:1.45">{rows}</table>'
        if section.more:
            text.extend([f"{'  ' if section.entries else ''}{section.more}", ""])
        more = f'<p style="font-size:13px;color:#5b6660;margin:6px 0 0">{html.escape(section.more)}</p>' if section.more else ""
        parts.append(heading + table + more)

    if plan.scoring_active:
        footnote = (f"Scores are relative to the research profile. Grants below {plan.min_score}/5 are left out "
                    "unless tracked or marked Relevant by the team.")
    else:
        footnote = (f"These grants match the research keywords in the profile, those closing within {URGENT_DAYS} days "
                    "first, then most matches first. Set up relevance scoring to rank them by fit.")
    footer = [footnote]
    if plan.has_tracked:
        footer.append("Tracked grants get a reminder at each of these marks before they close: "
                      + ", ".join(f"{d} days" for d in sorted(settings.reminder_days, reverse=True)) + ".")
    text.extend(footer)
    if unsubscribe:
        text.extend(["", f"Unsubscribe: {unsubscribe}"])

    header_links = f" &#183; {_link(dashboard, 'Open the dashboard')}" if dashboard else ""
    footer_html = "".join(f'<p style="font-size:12px;color:#5b6660;margin:6px 0 0">{html.escape(line)}</p>' for line in footer)
    if unsubscribe:
        footer_html += f'<p style="font-size:12px;color:#5b6660;margin:10px 0 0">{_link(unsubscribe, "Unsubscribe")}</p>'
    # A document of its own, light only: a dark-mode client would otherwise invert the colors above.
    html_doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">'
        f"<title>GrantWatch for {html.escape(day)}</title>"
        "<style>:root{color-scheme:light}</style></head>"
        '<body style="margin:0;background:#f7f8f6;color-scheme:light">'
        f'<div style="max-width:640px;margin:0 auto;padding:24px 16px;background:#f7f8f6;color:#15221c;font-family:{_FONT}">'
        '<div style="font-size:22px;font-weight:600">GrantWatch</div>'
        f'<div style="font-size:14px;color:#5b6660;margin-top:2px">{html.escape(day)}'
        + (f" &#183; {html.escape(profile_line)}" if profile_line else "")
        + header_links
        + "</div>"
        + "".join(parts)
        + f'<div style="margin-top:28px">{footer_html}</div>'
        "</div></body></html>"
    )
    return RenderedEmail(
        subject=_subject(plan, shown, sections, settings),
        text="\n".join(text).rstrip() + "\n",
        html=html_doc,
        shown=shown,
        overview_shown=overview_shown,
        unsubscribe_url=unsubscribe,
    )


def render_email(plan: Plan, settings: NotifySettings, today: date) -> Optional[RenderedEmail]:
    """The email for ``plan``, or None when there is nothing to say."""
    if plan.empty:
        return None
    caps: Dict[str, int] = {
        "reminders": min(_UNTRIMMED, len(plan.reminders)),
        "changes": min(_UNTRIMMED, len(plan.changes)),
        "removed": min(_UNTRIMMED, len(plan.removed)),
        "new": min(max(0, settings.max_new), len(plan.new)),
    }
    for index, (_, members) in enumerate(plan.overview):
        caps[f"overview{index}"] = min(max(0, settings.max_per_bucket), len(members))
    # Trimmed first to last: the overview is repeated weekly, new grants roll
    # over to the next email, and reminders matter most.
    order = [f"overview{i}" for i in reversed(range(len(plan.overview)))] + ["new", "removed", "changes", "reminders"]

    while True:
        email = _compose(plan, settings, today, caps)
        if email is None or email.size <= settings.max_bytes:
            return email
        key = next((k for k in order if caps.get(k, 0) > 0), None)
        if key is None:
            return None
        caps[key] -= max(1, caps[key] // 4)
