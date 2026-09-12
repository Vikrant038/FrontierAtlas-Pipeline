"""
Deterministic scoring functions for AIOrbit 100-Point Rubric (Pre-LLM Phase).
Evaluates Activity (15), Adoption (10), Documentation (5), and Reliability (5).
Maximum deterministic score: 40 points.
All functions are pure and independently unit-testable.
"""

from datetime import datetime, timezone
from typing import Optional, Tuple
from dateutil import parser as date_parser

from aiorbit.schema import MCPEntry


def score_activity(pushed_at: Optional[str], archived: bool = False, now: Optional[datetime] = None) -> float:
    """
    Score Activity / Maintenance (max 15 points):
    - archived == True -> 0
    - pushed <= 30 days ago -> 15.0
    - pushed <= 90 days ago -> 10.0
    - pushed <= 180 days ago -> 5.0
    - older or unknown -> 0.0
    """
    if archived or not pushed_at:
        return 0.0

    try:
        dt = date_parser.parse(pushed_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0

    current_time = now or datetime.now(timezone.utc)
    days_ago = (current_time - dt).total_seconds() / 86400.0

    if days_ago <= 30:
        return 15.0
    elif days_ago <= 90:
        return 10.0
    elif days_ago <= 180:
        return 5.0
    return 0.0


def score_adoption(stars: Optional[int]) -> float:
    """
    Score Adoption / Traction (max 10 points):
    - stars == 0 or None -> 0.0
    - 1 <= stars < 10 -> 2.0
    - 10 <= stars < 100 -> 4.0
    - 100 <= stars < 500 -> 6.0
    - 500 <= stars < 2000 -> 8.0
    - stars >= 2000 -> 10.0
    """
    if stars is None or stars <= 0:
        return 0.0
    if stars < 10:
        return 2.0
    if stars < 100:
        return 4.0
    if stars < 500:
        return 6.0
    if stars < 2000:
        return 8.0
    return 10.0


def score_documentation(docs_url: Optional[str], has_readme: bool = True) -> float:
    """
    Score Documentation / Ease of use (max 5 points):
    - Dedicated docs URL -> 5.0
    - Has repository README -> 3.0
    - None -> 0.0
    """
    if docs_url and len(docs_url.strip()) > 5:
        return 5.0
    if has_readme:
        return 3.0
    return 0.0


def score_reliability(site_alive: bool = True, is_fork: bool = False) -> float:
    """
    Score Reliability / Trust heuristic (max 5 points):
    - Independent repository + site alive -> 5.0
    - Forked repository or dead site -> 2.0
    - Completely unverified -> 0.0
    """
    if not site_alive:
        return 1.0
    if is_fork:
        return 2.5
    return 5.0


def score_recency(pushed_at: Optional[str], archived: bool = False, now: Optional[datetime] = None) -> float:
    """
    Score Recency / Momentum (max 5 points):
    - archived == True -> 0.0
    - pushed <= 30 days ago -> 5.0
    - pushed <= 90 days ago -> 4.0
    - pushed <= 180 days ago -> 3.0
    - pushed <= 365 days ago -> 2.0
    - older or unknown -> 0.0
    """
    if archived or not pushed_at:
        return 0.0

    try:
        dt = date_parser.parse(pushed_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0

    current_time = now or datetime.now(timezone.utc)
    days_ago = (current_time - dt).total_seconds() / 86400.0

    if days_ago <= 30:
        return 5.0
    elif days_ago <= 90:
        return 4.0
    elif days_ago <= 180:
        return 3.0
    elif days_ago <= 365:
        return 2.0
    return 0.0


def compute_deterministic_score(
    entry: MCPEntry,
    has_readme: bool = True,
    is_fork: bool = False,
    site_alive: bool = True,
    now: Optional[datetime] = None,
) -> Tuple[float, float]:
    """
    Compute deterministic score breakdown:
    Returns (activity_score, partial_overall_score) where partial_overall_score <= 45.0.
    """
    act = score_activity(entry.github_pushed_at, archived=bool(entry.github_archived), now=now)
    adopt = score_adoption(entry.github_stars)
    docs = score_documentation(entry.docs_url, has_readme=has_readme)
    rec = score_recency(entry.github_pushed_at, archived=bool(entry.github_archived), now=now)
    rel = score_reliability(site_alive=site_alive, is_fork=is_fork)

    partial_total = act + adopt + docs + rec + rel
    return act, round(partial_total, 2)


def compute_deterministic_breakdown(
    entry: MCPEntry,
    has_readme: bool = True,
    is_fork: bool = False,
    site_alive: bool = True,
    now: Optional[datetime] = None,
) -> Tuple[float, float, float, float, float, float]:
    """
    Returns (activity, adoption, docs, recency, reliability, total_deterministic)
    where total_deterministic <= 45.0.
    """
    act = score_activity(entry.github_pushed_at, archived=bool(entry.github_archived), now=now)
    adopt = score_adoption(entry.github_stars)
    docs = score_documentation(entry.docs_url, has_readme=has_readme)
    rec = score_recency(entry.github_pushed_at, archived=bool(entry.github_archived), now=now)
    rel = score_reliability(site_alive=site_alive, is_fork=is_fork)
    total = act + adopt + docs + rec + rel
    return act, adopt, docs, rec, rel, round(total, 2)
