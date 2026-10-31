"""What the team's Relevant / Not for us votes mean, without the database.

Pure functions over plain data, so the API, the email planner, the scorer and
the tests share one definition of each rule.

A vote is 1 (Relevant) or -1 (Not for us); a person has one vote per grant and
a new one replaces the old. A grant's net verdict is the sign of its votes'
sum: more Relevant than Not for us is 1, the reverse is -1, and a tie is 0,
which counts as no verdict. A program posted by several services (see
grants.display) is judged on the sum of its postings' votes, and a person's
vote on a program sits on exactly one of its postings.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from grants.display import Posting, group_postings

# How many marked grants go to the scorer as examples, and how long each may be.
EXAMPLE_LIMIT = 10
ENTRY_CHARS = 200


class Votes(NamedTuple):
    """The votes on one grant: how many each way, and one person's own verdict (None if they have none)."""

    up: int = 0
    down: int = 0
    mine: Optional[int] = None


def clean_text(value: Any, limit: Optional[int] = None) -> str:
    """One line of text: control characters removed, whitespace folded to single spaces, cut to ``limit``.

    Format characters (zero-width, bidirectional overrides) go too: this text ends up in a prompt.
    """
    kept = "".join(ch for ch in str(value if value is not None else "") if ch.isspace() or unicodedata.category(ch)[0] != "C")
    text = " ".join(kept.split())
    return text[:limit].rstrip() if limit is not None else text


def net_verdict(net_votes: int) -> int:
    """1 when Relevant outnumbers Not for us, -1 when it is outnumbered, else 0 (no verdict)."""
    return (net_votes > 0) - (net_votes < 0)


def relevant_postings(postings: Sequence[Posting], net_votes: Mapping[str, int]) -> List[str]:
    """The opp_ids, sorted, of every posting of each program the team marked Relevant on balance.

    ``net_votes`` is each posting's Relevant votes less its Not for us votes (a posting
    missing from it has none). A program is judged on its postings' votes together, as the
    email planner judges it, and all its postings count: the dashboard then folds the row
    as it does at any score floor, and the program is listed as the email announces it.
    """
    out: List[str] = []
    for group in group_postings(postings):
        members = (group.lead, *group.also)
        if net_verdict(sum(net_votes.get(opp_id, 0) for opp_id in members)) > 0:
            out.extend(members)
    return sorted(out)


def _sign(verdict: Any) -> int:
    return (verdict > 0) - (verdict < 0)


def _vote_key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    """A total order on one person's votes on a grant: the newest, whatever order the rows came in."""
    return (row["created_at"], _sign(row["verdict"]), str(row.get("title") or ""), str(row.get("agency") or ""), str(row.get("summary") or ""))


def _newest_first(items: Iterable[Tuple[Any, str, int, Mapping[str, Any]]]) -> List[Tuple[Any, str, int, Mapping[str, Any]]]:
    """Newest vote first, ties by opp_id (the sort is stable, so sort by the tie-break first)."""
    return sorted(sorted(items, key=lambda d: d[1]), key=lambda d: d[0], reverse=True)


def _entry(sign: int, row: Mapping[str, Any]) -> str:
    title = clean_text(row.get("title")) or clean_text(row["opp_id"]) or "untitled"
    agency = clean_text(row.get("agency"))
    summary = clean_text(row.get("summary"))
    text = title + (f" ({agency})" if agency else "") + (f": {summary}" if summary else "")
    # Backticks could close the fence the entries are sent in.
    return f"{'+' if sign > 0 else '-'} {text}".replace("`", "'")[:ENTRY_CHARS].rstrip()


def feedback_examples(rows: Iterable[Mapping[str, Any]], limit: int = EXAMPLE_LIMIT) -> List[str]:
    """Up to ``limit`` lines, ``+ Title (Agency): summary`` or ``- ...``, to show the scorer what the team wants.

    ``rows`` are vote rows: opp_id, email, verdict, created_at and the title,
    agency and summary copied when the vote was cast. Each person's latest
    vote on a grant is the one that counts. A grant's verdict is the sign of
    its votes' sum, and one with a tie is left out. The most recently voted
    grants come first, up to ``limit // 2`` of each verdict; when one verdict
    has fewer than that, the other fills the rest. Each line is one line of
    clean text, at most ENTRY_CHARS long.
    """
    latest: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (str(row["opp_id"]), str(row["email"]))
        if key not in latest or _vote_key(row) > _vote_key(latest[key]):
            latest[key] = row
    by_grant: Dict[str, List[Mapping[str, Any]]] = {}
    for (opp_id, _), row in latest.items():
        by_grant.setdefault(opp_id, []).append(row)

    decided = []
    for opp_id, votes in by_grant.items():
        sign = net_verdict(sum(_sign(v["verdict"]) for v in votes))
        if sign:
            newest = max(votes, key=_vote_key)  # its title and summary are the freshest copy
            decided.append((newest["created_at"], opp_id, sign, newest))

    decided = _newest_first(decided)
    liked = [d for d in decided if d[2] > 0]
    disliked = [d for d in decided if d[2] < 0]
    half = max(0, limit) // 2
    taken_up, taken_down = liked[:half], disliked[:half]
    if len(liked) < half:
        taken_down = disliked[: max(0, limit) - len(taken_up)]
    elif len(disliked) < half:
        taken_up = liked[: max(0, limit) - len(taken_down)]
    return [_entry(sign, row) for _, _, sign, row in _newest_first(taken_up + taken_down)]


def with_feedback(rows: Sequence[Mapping[str, Any]], counts: Optional[Mapping[str, Votes]]) -> List[Dict[str, Any]]:
    """API rows (see grants.display.group_rows) with what the team thinks of each.

    Adds ``my_verdict`` (the viewer's own vote on the program, null when signed out) and
    ``my_vote_opp_id`` (the posting that vote sits on: the first of the lead and the
    ``also_posted`` ones that holds it, else the lead, where a new vote goes), ``votes_up``
    and ``votes_down`` (all the program's postings together), ``team_verdict`` (1, -1, or 0
    for none or a tie) and ``hidden``: the team said Not for us and nobody tracks it.
    Without ``counts`` (the votes could not be read) those are null and nothing is hidden.
    Rows keep their order; the input is not changed.
    """
    out = []
    for source in rows:
        row = dict(source)
        if counts is None:
            row.update(my_verdict=None, my_vote_opp_id=row["opp_id"], votes_up=None, votes_down=None, team_verdict=None, hidden=False)
        else:
            ids = [row["opp_id"]] + [a["opp_id"] for a in row.get("also_posted") or ()]
            postings = [counts.get(opp_id, Votes()) for opp_id in ids]
            up, down = sum(v.up for v in postings), sum(v.down for v in postings)
            team = net_verdict(up - down)
            mine = next(((opp_id, v.mine) for opp_id, v in zip(ids, postings) if v.mine is not None), (ids[0], None))
            row.update(my_verdict=mine[1], my_vote_opp_id=mine[0], votes_up=up, votes_down=down, team_verdict=team,
                       hidden=team < 0 and not row.get("watched"))
        out.append(row)
    return out
