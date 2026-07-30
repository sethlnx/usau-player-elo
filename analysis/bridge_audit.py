"""Rank college<->club bridge links by false-merge suspicion.

For every bridge in data/cross_division_links.csv, gather the evidence the
name match ignores — listed heights on each side, team cities, and how far
the merged history moves the player's rating versus a club-only replay —
and write a review queue to data/bridge_audit.csv (most suspect first).

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

    # rating shift: merged history vs club-only replay
    games = bt.load_games(con)
    rosters, clubs = bt.load_maps(con)
    cfg = EloConfig()
    _, full = bt.replay("player", games, rosters, clubs, cfg)
    _, clubonly = bt.replay("player", [g for g in games if g["division"] == "club"],
                            rosters, clubs, cfg)
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
            hc = heights.get((nname, "college"), [])
            hb = heights.get((nname, "club"), [])
            med_c = statistics.median(hc) if hc else None
            med_b = statistics.median(hb) if hb else None
            hdiff = abs(med_c - med_b) if med_c and med_b else None
            conflict = (hdiff is not None and hdiff >= HEIGHT_CONFLICT_INCHES
                        and len(hc) >= MIN_HEIGHT_SAMPLES
                        and len(hb) >= MIN_HEIGHT_SAMPLES)
            pid = pid_of.get(nname)
            fs = full.players.get(pid) if pid else None
            cs = clubonly.players.get(pid) if pid else None
            shift = (fs.rating - cs.rating) if fs and cs else 0.0
            audit.append({
                "norm_name": nname,
                "height_conflict": int(conflict),
                "rating_shift": round(shift, 1),
                "med_height_college": med_c, "n_h_college": len(hc),
                "med_height_club": med_b, "n_h_club": len(hb),
                "college_teams": row["college_teams"],
                "club_teams": row["club_teams"],
                "college_cities": "; ".join(sorted(cities.get((nname, "college"), []))),
                "club_cities": "; ".join(sorted(cities.get((nname, "club"), []))),
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
