"""Infer season-aware Handler / Hybrid / Cutter player designations.

Roster position labels are authoritative when available. UFA box scores and
USAU G/A/T tournament lines provide progressively weaker fallbacks. Sparse or
missing evidence remains ``unknown`` rather than being mislabeled ``hybrid``.
"""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from analysis.backtest import DB_PATH
from ufa.link import resolve_links

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OVERRIDES = ROOT / "data" / "player_role_overrides.csv"
DEFAULT_PLAYERS = ROOT / "data" / "player_elo.csv"
DEFAULT_OUTPUT = ROOT / "data" / "player_roles.csv"

ROLES = ("unknown", "cutter", "hybrid", "handler")
POSITION_SCORE = {
    "Cutter": -1.0,
    "Deep": -1.0,
    "Defense (Cutter)": -1.0,
    "Handler": 1.0,
    "Dump": 1.0,
    "Defense (Handler)": 1.0,
    "Handler/Cutter": 0.0,
    "Mid": 0.0,
    "Defense (Cutter/Handler)": 0.0,
}
HANDLER_THRESHOLD = 0.30
CUTTER_THRESHOLD = -0.30
MIN_CONFIDENCE = 0.25


@dataclass(frozen=True)
class RoleRecord:
    player_id: int | str
    season: int
    role: str
    handler_index: float
    confidence: float
    evidence: float
    source: str
    overridden: bool = False
    note: str = ""


def _pid(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _num(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def role_for(index: float, confidence: float) -> str:
    if confidence < MIN_CONFIDENCE:
        return "unknown"
    if index >= HANDLER_THRESHOLD:
        return "handler"
    if index <= CUTTER_THRESHOLD:
        return "cutter"
    return "hybrid"


def _record(pid, season, index, confidence, evidence, source) -> RoleRecord:
    index = float(index)
    confidence = min(max(float(confidence), 0.0), 1.0)
    return RoleRecord(
        _pid(pid), int(season), role_for(index, confidence), index,
        confidence, float(evidence), source,
    )


def load_position_roles(con) -> dict[tuple, RoleRecord]:
    votes = defaultdict(list)
    try:
        rows = con.execute("""
            SELECT rp.player_id, ev.season, TRIM(re.position)
            FROM roster_entries re
            JOIN roster_players rp
              ON rp.event_team_id = re.event_team_id AND rp.name = re.name
            JOIN event_teams et ON et.event_team_id = re.event_team_id
            JOIN events ev ON ev.event_id = et.event_id
            WHERE TRIM(COALESCE(re.position, '')) != ''
        """)
        for pid, season, position in rows:
            score = POSITION_SCORE.get(position)
            if score is not None:
                votes[(_pid(pid), int(season))].append(score)
    except sqlite3.OperationalError:
        return {}

    out = {}
    for key, scores in votes.items():
        counts = Counter(scores)
        index = sum(scores) / len(scores)
        agreement = max(counts.values()) / len(scores)
        confidence = min(0.98, 0.72 + 0.08 * math.log1p(len(scores))) * agreement
        out[key] = _record(*key, index, confidence, len(scores), "position")
    return out


def load_ufa_roles(con) -> dict[tuple, RoleRecord]:
    try:
        links = resolve_links(con)
        rows = con.execute("""
            SELECT player_id, year,
                   COALESCE(opointsplayed,0) + COALESCE(dpointsplayed,0),
                   COALESCE(completions,0), COALESCE(throwattempts,0),
                   COALESCE(yardsthrown,0), COALESCE(yardsreceived,0),
                   COALESCE(hockeyassists,0), COALESCE(assists,0),
                   COALESCE(goals,0), COALESCE(catches,0)
            FROM ufa_player_stats
        """)
    except sqlite3.OperationalError:
        return {}

    out = {}
    for upid, season, points, completions, attempts, ty, ry, ha, assists, goals, catches in rows:
        pid = links.get(upid)
        if pid is None or points <= 0:
            continue
        # Volume ratio, not performance quality: attempts establish handling
        # responsibility; completions, assists, HA and TY reinforce it. Catches,
        # goals and RY establish downfield receiving responsibility.
        handler = completions + 0.15 * attempts + 3 * assists + 2 * ha + ty / 15
        cutter = catches + 3 * goals + ry / 15
        index = math.log((handler + 10) / (cutter + 10))
        confidence = 0.20 + 0.70 * (1.0 - math.exp(-points / 150.0))
        out[(_pid(pid), int(season))] = _record(
            pid, season, index, confidence, points, "ufa",
        )
    return out


def load_usau_stat_roles(con) -> dict[tuple, RoleRecord]:
    totals = defaultdict(lambda: [0, 0, 0, 0])
    try:
        rows = con.execute("""
            SELECT rp.player_id, ev.season, re.points, re.assists, re.turns
            FROM roster_entries re
            JOIN roster_players rp
              ON rp.event_team_id = re.event_team_id AND rp.name = re.name
            JOIN event_teams et ON et.event_team_id = re.event_team_id
            JOIN events ev ON ev.event_id = et.event_id
            WHERE re.points != '' OR re.assists != '' OR re.turns != ''
        """)
    except sqlite3.OperationalError:
        return {}

    for pid, season, goals, assists, turns in rows:
        total = totals[(_pid(pid), int(season))]
        total[0] += _num(goals)
        total[1] += _num(assists)
        total[2] += _num(turns)
        total[3] += 1

    out = {}
    for (pid, season), (goals, assists, turns, events) in totals.items():
        actions = goals + assists + turns
        if actions <= 0:
            continue
        # Turns are role evidence because a player must possess the disc to turn
        # it over; they are deliberately not treated as positive performance.
        handler = assists + 0.5 * turns
        cutter = 1.5 * goals
        index = math.log((handler + 2) / (cutter + 2))
        confidence = min(
            0.70,
            (0.15 + 0.55 * (1.0 - math.exp(-actions / 25.0)))
            * min(1.0, events / 2.0),
        )
        out[(pid, season)] = _record(
            pid, season, index, confidence, actions, "usau-stats",
        )
    return out


def infer_roles(con) -> dict[tuple, RoleRecord]:
    """Return the strongest available record for every player-season."""
    usau = load_usau_stat_roles(con)
    ufa = load_ufa_roles(con)
    positions = load_position_roles(con)
    # Increasing authority: official roster position > rich UFA line > USAU G/A/T.
    return {**usau, **ufa, **positions}


def load_overrides(path: Path = DEFAULT_OVERRIDES) -> dict[tuple, tuple[str, str]]:
    if not path.exists():
        return {}
    out = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("player_id") or not row.get("season"):
                continue
            role = (row.get("role") or "").strip().lower()
            if role not in ROLES:
                raise ValueError(f"invalid player role override {role!r}")
            out[(_pid(row["player_id"]), int(row["season"]))] = (
                role, (row.get("note") or "").strip(),
            )
    return out


def apply_overrides(
    records: dict[tuple, RoleRecord], overrides: dict[tuple, tuple[str, str]],
) -> dict[tuple, RoleRecord]:
    out = dict(records)
    for (pid, season), (role, note) in overrides.items():
        prior = out.get((pid, season))
        out[(pid, season)] = RoleRecord(
            pid, season, role,
            prior.handler_index if prior else 0.0,
            1.0,
            prior.evidence if prior else 0.0,
            "override",
            True,
            note,
        )
    return out


def build_player_roles(
    con,
    target_seasons: dict[int | str, int],
    overrides_path: Path = DEFAULT_OVERRIDES,
) -> tuple[dict[tuple, RoleRecord], dict[int | str, RoleRecord]]:
    """Build historical evidence rows and one current row per target player."""
    records = apply_overrides(infer_roles(con), load_overrides(overrides_path))
    by_player = defaultdict(list)
    for record in records.values():
        by_player[record.player_id].append(record)
    for values in by_player.values():
        values.sort(key=lambda r: r.season)

    current = {}
    for raw_pid, raw_season in target_seasons.items():
        pid, season = _pid(raw_pid), int(raw_season)
        exact = records.get((pid, season))
        if exact is not None:
            current[pid] = exact
            continue
        previous = [r for r in by_player.get(pid, ()) if r.season < season]
        if previous:
            prior = previous[-1]
            gap = season - prior.season
            confidence = prior.confidence * (0.75 ** gap)
            carried = _record(
                pid, season, prior.handler_index, confidence, prior.evidence,
                f"prior-{prior.source}",
            )
            if prior.overridden:
                carried = replace(carried, overridden=True, note=prior.note)
            current[pid] = carried
            records[(pid, season)] = carried
        else:
            unknown = RoleRecord(pid, season, "unknown", 0.0, 0.0, 0.0, "none")
            current[pid] = unknown
            records[(pid, season)] = unknown
    return records, current


def write_role_csv(path: Path, records: dict[tuple, RoleRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow([
            "player_id", "season", "role", "handler_index", "confidence",
            "evidence", "source", "overridden", "note",
        ])
        for record in sorted(
            records.values(), key=lambda r: (str(r.player_id), r.season),
        ):
            writer.writerow([
                record.player_id, record.season, record.role,
                round(record.handler_index, 4), round(record.confidence, 4),
                round(record.evidence, 1), record.source,
                int(record.overridden), record.note,
            ])


def report(records: dict[tuple, RoleRecord], current: dict) -> None:
    roles = Counter(r.role for r in current.values())
    sources = Counter(r.source for r in current.values())
    print(f"wrote {len(records):,} player-season roles; {len(current):,} current players")
    print("roles: " + ", ".join(f"{k}={roles[k]:,}" for k in ROLES))
    print("sources: " + ", ".join(f"{k}={v:,}" for k, v in sources.most_common()))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--players", type=Path, default=DEFAULT_PLAYERS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    args = parser.parse_args()

    with args.players.open(newline="") as f:
        targets = {
            _pid(row["player_id"]): int(row["last_season"])
            for row in csv.DictReader(f)
            if row.get("last_season") and row["last_season"] != "?"
        }
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        records, current = build_player_roles(con, targets, args.overrides)
    finally:
        con.close()
    write_role_csv(args.output, records)
    report(records, current)


if __name__ == "__main__":
    main()
