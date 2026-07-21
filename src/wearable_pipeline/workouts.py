"""Match the same real-world workout across devices by time overlap.

Whoop and Google Health assign different ids to the same session, so we pair
them when their [start, end] windows overlap. Greedy by largest overlap; each
session is matched at most once. Unmatched sessions are returned, never dropped.
Pure function — no DB, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Workout


@dataclass(frozen=True)
class WorkoutPair:
    whoop: Workout
    google: Workout
    overlap_minutes: float


@dataclass(frozen=True)
class MatchResult:
    pairs: list[WorkoutPair]
    unmatched_whoop: list[Workout]
    unmatched_google: list[Workout]


def _end(w: Workout) -> datetime:
    return w.end_time or w.start_time


def _overlap_minutes(a: Workout, b: Workout) -> float:
    start = max(a.start_time, b.start_time)
    end = min(_end(a), _end(b))
    delta = (end - start).total_seconds() / 60
    return delta if delta > 0 else 0.0


def match_workouts(
    whoop: list[Workout], google: list[Workout]
) -> MatchResult:
    candidates: list[tuple[float, int, int]] = []
    for i, w in enumerate(whoop):
        for j, g in enumerate(google):
            ov = _overlap_minutes(w, g)
            if ov > 0:
                candidates.append((ov, i, j))
    candidates.sort(key=lambda c: c[0], reverse=True)

    used_w: set[int] = set()
    used_g: set[int] = set()
    pairs: list[WorkoutPair] = []
    for ov, i, j in candidates:
        if i in used_w or j in used_g:
            continue
        used_w.add(i)
        used_g.add(j)
        pairs.append(WorkoutPair(whoop=whoop[i], google=google[j], overlap_minutes=ov))

    unmatched_whoop = [w for i, w in enumerate(whoop) if i not in used_w]
    unmatched_google = [g for j, g in enumerate(google) if j not in used_g]
    return MatchResult(
        pairs=pairs,
        unmatched_whoop=unmatched_whoop,
        unmatched_google=unmatched_google,
    )
