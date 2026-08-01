"""Rank cross-division bridge links by false-merge suspicion.

For every bridge in data/cross_division_links.csv, gather the evidence the
name match ignores — listed heights on each side, team cities, and how far
the merged history moves the player's rating versus a replay with every
cross-division link severed — and write a review queue to
data/bridge_audit.csv (most suspect first).

Five divisions, so both halves are division-generic. Heights are compared
pairwise across every division the bridge spans and the widest gap is the
verdict; the rating baseline re-keys each roster slot to (player, division),
which is exactly the merge this file is auditing, rather than the old
club-only replay that could only see college<->club.

A hard height contradiction (both sides listed, medians >= 4" apart, 2+
samples each) is as close to proof of two different people as this data
offers; --write-overrides appends those as `block` rows to
data/link_overrides.csv for identity.resolve to enforce.

Usage: python -m analysis.bridge_audit [--write-overrides]
"""

import csv
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analysis.backtest as bt
from elo.engine import EloConfig
from identity.resolve import load_overrides, norm_name

DATA = Path(__file__).resolve().parent.parent / "data"
HEIGHT_CONFLICT_INCHES = 4
MIN_HEIGHT_SAMPLES = 2


def parse_height(h: str) -> int | None:
    m = re.match(r"^\s*(\d)'\s*(\d{1,2})", h or "")
    return int(m.group(1)) * 12 + int(m.group(2)) if m else None


def main(write_overrides: bool = False):
    con = sqlite3.connect(bt.DB_PATH)

    # evidence per (norm_name, division): heights, cities, clubs
    heights = defaultdict(list)
    cities = defaultdict(set)
    for raw, division, height, city in con.execute("""
            SELECT re.name, ev.division, re.height, et.city
            FROM roster_entries re
            JOIN event_teams et USING (event_team_id)
            JOIN events ev ON ev.event_id = et.event_id"""):
        nname = norm_name(raw)
        inches = parse_height(height)
        if inches:
            heights[(nname, division)].append(inches)
        if city:
            cities[(nname, division)].add(city.strip())

    pid_of = {}
    for pid, nname in con.execute(
            "SELECT player_id, norm_name FROM players WHERE ambiguous=0"):
        pid_of[nname] = pid

    # Rating shift: the merged replay against one where a bridge is severed —
    # every roster slot re-keyed to (player_id, division), so a player who
    # spans divisions is replayed as one identity per division. The difference
    # at a bridge IS what merging bought or cost it.
    games = bt.load_games(con)
    rosters, clubs = bt.load_maps(con)
    et_div = dict(con.execute("""
        SELECT et.event_team_id, COALESCE(ev.division, 'club')
        FROM event_teams et JOIN events ev ON ev.event_id = et.event_id"""))
    split_rosters = {etid: [(pid, et_div.get(etid, "club")) for pid in pids]
                     for etid, pids in rosters.items()}
    cfg = EloConfig()
    _, full = bt.replay("player", games, rosters, clubs, cfg)
    _, severed = bt.replay("player", games, split_rosters, clubs, cfg)
    con.close()

    links_file = DATA / "cross_division_links.csv"
    overrides_file = DATA / "link_overrides.csv"
    overrides = load_overrides(overrides_file)

    audit = []
    with open(links_file, newline="") as f:
        for row in csv.DictReader(f):
            nname = row["norm_name"]
            if overrides.get(nname) in ("block", "confirm"):
                continue
            divisions = [d for d in row["divisions"].split("; ") if d]
            meds = {d: statistics.median(heights[(nname, d)])
                    for d in divisions
                    if len(heights.get((nname, d), [])) >= MIN_HEIGHT_SAMPLES}
            hdiff = (max(meds.values()) - min(meds.values())) if len(meds) > 1 else None
            conflict = hdiff is not None and hdiff >= HEIGHT_CONFLICT_INCHES
            pid = pid_of.get(nname)
            fs = full.players.get(pid) if pid else None
            # Severed, the player exists once per division; the largest of
            # those is the identity the merge grew out of.
            shards = [severed.players[(pid, d)] for d in divisions
                      if (pid, d) in severed.players] if pid else []
            base = max(shards, key=lambda s: s.games) if shards else None
            shift = (fs.rating - base.rating) if fs and base else 0.0
            audit.append({
                "norm_name": nname,
                "height_conflict": int(conflict),
                "rating_shift": round(shift, 1),
                "height_gap_in": round(hdiff, 1) if hdiff is not None else None,
                "divisions": row["divisions"],
                "med_heights": "; ".join(f"{d} {meds[d]:.0f}\"" for d in sorted(meds)),
                "n_heights": "; ".join(
                    f"{d} {len(heights.get((nname, d), []))}" for d in divisions),
                "teams": row["teams"],
                "cities": "; ".join(sorted(
                    {c for d in divisions for c in cities.get((nname, d), set())})),
            })

    audit.sort(key=lambda a: (-a["height_conflict"], -abs(a["rating_shift"])))
    out = DATA / "bridge_audit.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(audit[0].keys()))
        w.writeheader()
        w.writerows(audit)
    n_conflict = sum(a["height_conflict"] for a in audit)
    print(f"wrote {out}: {len(audit)} unreviewed bridges, "
          f"{n_conflict} hard height conflicts, "
          f"{sum(abs(a['rating_shift']) > 200 for a in audit)} with |shift| > 200")

    if write_overrides and n_conflict:
        new_rows = [a["norm_name"] for a in audit if a["height_conflict"]]
        exists = overrides_file.exists()
        with open(overrides_file, "a", newline="") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["norm_name", "action", "note"])
            for nname in new_rows:
                w.writerow([nname, "block", "auto: height medians >= 4in apart"])
        print(f"appended {len(new_rows)} block rows to {overrides_file} "
              f"— re-run identity.resolve to apply")


if __name__ == "__main__":
    main(write_overrides="--write-overrides" in sys.argv)
