"""Link UFA playerIDs to USAU player identities.

A link is auto-accepted when the normalized name matches exactly one USAU
identity and at least one corroborating signal agrees: team-city geography,
jersey number, or overlapping active years. Everything else — ambiguous
USAU names, zero corroboration, no match — lands in the audit queue.

Links are keyed on the USAU identity key (norm_name [, club]) rather than
numeric player_id: identity.resolve rebuilds ids on every run. Use
resolve_links() to map accepted links to current player_ids at runtime.

Usage: python -m ufa.link
Writes data/ufa_links.csv (accepted) and data/ufa_link_audit.csv (queue).
"""

import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

from identity.resolve import norm_name
from scraper.build_db import DB_PATH

DATA = Path(__file__).resolve().parent.parent / "data"
LINKS_CSV = DATA / "ufa_links.csv"
AUDIT_CSV = DATA / "ufa_link_audit.csv"


def _city_token(s: str | None) -> str:
    """'San Francisco, California' / 'San Francisco' -> 'san francisco'."""
    return (s or "").split(",")[0].strip().lower()


def _load_ufa(con):
    """ufa_player_id -> {name, years, cities, jerseys}."""
    team_city = {(t, y): _city_token(c) for t, y, c in
                 con.execute("SELECT team_id, year, city FROM ufa_teams")}
    ufa = {}
    for pid, year, team, first, last, jersey in con.execute(
            "SELECT player_id, year, team_id, first_name, last_name, jersey "
            "FROM ufa_players"):
        rec = ufa.setdefault(pid, {
            "name": f"{first or ''} {last or ''}",
            "years": set(), "cities": set(), "jerseys": set()})
        rec["years"].add(year)
        city = team_city.get((team, year))
        if city:
            rec["cities"].add(city)
        if jersey:
            rec["jerseys"].add(jersey.strip().lstrip("0") or "0")
    return ufa


def _load_usau(con):
    """identity key -> {ambiguous, clubs, cities, jerseys, seasons}.

    Key is (norm_name,) for unambiguous names, (norm_name, club) otherwise —
    matching identity.resolve's player_for keys.
    """
    idx = defaultdict(lambda: {"cities": set(), "jerseys": set(),
                               "seasons": set(), "clubs": set()})
    ambiguous = {n for (n,) in con.execute(
        "SELECT DISTINCT norm_name FROM players WHERE ambiguous=1")}
    rows = con.execute("""
        SELECT re.name, re.number, et.city,
               COALESCE(et.full_name, et.display_name), ev.season
        FROM roster_entries re
        JOIN event_teams et USING (event_team_id)
        JOIN events ev ON ev.event_id = et.event_id""")
    for raw, number, city, club, season in rows:
        nname = norm_name(raw)
        if not nname:
            continue
        club_l = (club or "?").lower()
        key = (nname, club_l) if nname in ambiguous else (nname,)
        rec = idx[key]
        rec["seasons"].add(season)
        rec["clubs"].add(club_l)
        if city:
            rec["cities"].add(_city_token(city))
        if number:
            rec["jerseys"].add(number.strip().lstrip("0") or "0")
    return idx, ambiguous


def _evidence(u: dict, cand: dict) -> list[str]:
    ev = []
    if u["cities"] & cand["cities"]:
        ev.append("city:" + ";".join(sorted(u["cities"] & cand["cities"])))
    if u["jerseys"] & cand["jerseys"]:
        ev.append("jersey:" + ";".join(sorted(u["jerseys"] & cand["jerseys"])))
    if u["years"] & cand["seasons"]:
        ev.append("years:%d" % len(u["years"] & cand["seasons"]))
    return ev


def main():
    con = sqlite3.connect(DB_PATH)
    ufa = _load_ufa(con)
    usau, ambiguous = _load_usau(con)
    con.close()

    by_name = defaultdict(list)   # norm_name -> [identity key]
    for key in usau:
        by_name[key[0]].append(key)

    links, audit = [], []
    for pid, u in sorted(ufa.items()):
        nname = norm_name(u["name"])
        cands = by_name.get(nname, [])
        if not cands:
            audit.append([pid, nname, "no-match", "", ""])
            continue
        scored = sorted(((key, _evidence(u, usau[key])) for key in cands),
                        key=lambda kv: -len(kv[1]))
        best_key, best_ev = scored[0]
        club = best_key[1] if len(best_key) > 1 else ""
        # unique identity with evidence -> link; a strictly-best-evidenced
        # candidate among several also links (others scored zero)
        runner_up = len(scored) > 1 and len(scored[1][1]) > 0
        if best_ev and not runner_up:
            links.append([pid, nname, club, len(best_ev), " ".join(best_ev)])
        else:
            reason = ("ambiguous" if len(cands) > 1 else "no-corroboration")
            audit.append([pid, nname, reason, club, " ".join(best_ev)])

    with open(LINKS_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ufa_player_id", "norm_name", "club", "n_evidence", "evidence"])
        w.writerows(links)
    with open(AUDIT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ufa_player_id", "norm_name", "reason", "club", "evidence"])
        w.writerows(audit)

    n_nomatch = sum(1 for a in audit if a[2] == "no-match")
    print(f"{len(ufa)} UFA players: {len(links)} linked, "
          f"{len(audit) - n_nomatch} queued for review, {n_nomatch} no USAU match")
    print(f"wrote {LINKS_CSV} and {AUDIT_CSV}")


def resolve_links(con) -> dict[str, int]:
    """ufa_player_id -> current player_id, from the accepted links file."""
    if not LINKS_CSV.exists():
        return {}
    by_name: dict[tuple, int] = {}
    for pid, nname, amb in con.execute(
            "SELECT player_id, norm_name, ambiguous FROM players"):
        if not amb:
            by_name[(nname,)] = pid
    # ambiguous identities: map (norm_name, club) via roster membership
    for pid, nname, club in con.execute("""
            SELECT DISTINCT p.player_id, p.norm_name,
                   LOWER(COALESCE(et.full_name, et.display_name))
            FROM players p
            JOIN roster_players rp USING (player_id)
            JOIN event_teams et USING (event_team_id)
            WHERE p.ambiguous = 1"""):
        by_name[(nname, club)] = pid
    out = {}
    with open(LINKS_CSV, newline="") as f:
        for row in csv.DictReader(f):
            key = ((row["norm_name"], row["club"]) if row["club"]
                   else (row["norm_name"],))
            if key in by_name:
                out[row["ufa_player_id"]] = by_name[key]
    return out


if __name__ == "__main__":
    main()
