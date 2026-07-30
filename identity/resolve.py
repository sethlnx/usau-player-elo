"""Resolve roster name strings to canonical player IDs.

Strategy:
- Normalize names (unicode, whitespace, case).
- A name on two or more teams in the SAME (division, season) collides. It is
  only SPLIT per club when the shards also contradict physically — different
  teams in overlapping date windows, which one body cannot do. Collisions
  without that conflict resolve to a single identity.
- A name appearing in different divisions the same season (college spring +
  club summer) never collides — that's a bridge player, and linking them is
  the whole point of the unified rating. Cross-division links are logged to
  their own review file since they carry the most false-merge risk.
- The date conflict is asymmetric evidence: present, it proves two people;
  absent, it merely fails to disprove them. Same-named players in different
  regions who never coincide therefore merge, and a bad merge corrupts two
  histories where a bad split only loses a link — so every auto-merged
  collision is logged to data/ambiguities.csv for review, and a `block`
  verdict splits one back apart.

Usage: python -m identity.resolve
Writes tables `players` and `roster_players` into data/usau.db, an ambiguity
report at data/ambiguities.csv, and cross-division links at
data/cross_division_links.csv.

Manual review verdicts live in data/link_overrides.csv (norm_name, action,
note): action "block" splits that name's college and club identities (the
name match was two different people); "merge" is its inverse — it forces one
identity for a name the (division, season) rule called ambiguous, for when
review shows the multi-club season was one person (a youth or fifth-year
player on several summer rosters); "confirm" marks the bridge reviewed-OK so
audits skip it. See analysis/bridge_audit.py for the review queue.
"""

import csv
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "usau.db"
AMBIGUITY_REPORT = DB_PATH.parent / "ambiguities.csv"

SCHEMA = """
DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS roster_players;
CREATE TABLE players (
    player_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    ambiguous INTEGER DEFAULT 0
);
CREATE TABLE roster_players (
    event_team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    PRIMARY KEY (event_team_id, name)
);
"""


def norm_name(name: str) -> str:
    """Normalized match key, with diacritics folded.

    An accent is a data-entry choice, not an identity signal — 'Zachary
    Hébert' (2025) and 'Zachary Hebert' (2026) are one player on one club,
    split into two careers by the accent alone. Folding is safe because the
    splits a name appearing on multiple clubs in a season, so a fold can only
    merge within a single club-season, never across two people on two clubs.
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[.’']", "", s)          # periods and apostrophes
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


# Clubs entered under inconsistent names across seasons. Keyed on the normalized
# (lowercased, seed-stripped) variant -> canonical display name. This is the
# club-level analogue of the player alias table; extend as more are found.
CLUB_ALIASES = {
    "rhino": "Rhino Slam!",   # Portland Rhino Slam! registered as "Rhino" in 2025
    # Eugene's Dark Star registered as "Dark Star-D" in 2023-24. Same city, no
    # concurrent "Dark Star" entry either year, and 15-19 shared players across
    # each naming boundary. Left unmerged it split six careers in two
    # (Wayte, Moll, Bosworth, Anderson, Koenigsberg, Dillender-Kinast).
    "dark star-d": "Dark Star",
}


def canonical_club(full_name: str | None, display_name: str | None) -> str:
    """Display-cased club name with cross-season aliases merged.

    Single source of truth for club identity: the player resolver (via
    norm_club below), the team rankings, and the time series all route club
    names through here, so an alias added to CLUB_ALIASES takes effect in
    every place a club is grouped — including which players count as teammates.
    """
    s = (full_name or display_name or "?").strip()
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)       # seed suffix
    s = re.sub(r"\s+", " ", s)
    return CLUB_ALIASES.get(s.lower(), s)


def norm_club(full_name: str | None, display_name: str | None) -> str:
    """Normalized (lowercased) canonical club key for identity matching."""
    return canonical_club(full_name, display_name).lower()


def load_overrides(path: Path) -> dict[str, str]:
    """norm_name -> action ('block' | 'confirm') from the review file."""
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {norm_name(r["norm_name"]): r["action"].strip().lower()
                for r in csv.DictReader(f) if r.get("norm_name")}


def has_date_conflict(windows: list[tuple[str, str, str, int]]) -> bool:
    """True if two DIFFERENT clubs at DIFFERENT events hold overlapping dates.

    One body cannot be at two tournaments the same weekend, so such a clash
    proves the name covers at least two people. `windows` is
    (club, start, end, event_id); end falls back to start for single-day events.

    Pairs inside the SAME event are ignored. Being listed on two rosters at one
    tournament is an administrative artifact — a player entered on both a club's
    main and B squad, or a duplicate entry — not evidence of two humans. Counting
    those split 159 names that nothing else separates (37% of all splits),
    stranding 3,408 games: Ben Thoennes appears on both Dark Star and Oregon
    Eruption! at Eugene Summer Solstice 2019, which shattered a 331-game career
    into four shards of 184/89/65/6.

    O(n log n) by start date — only adjacent-in-time pairs can overlap once
    sorted, but a long multi-day event can straddle several later ones, so the
    inner scan runs until the next start clears the running max end.
    """
    ws = sorted((s, e or s, c, ev) for c, s, e, ev in windows)
    for i, (_, end_i, club_i, ev_i) in enumerate(ws):
        for start_j, _, club_j, ev_j in ws[i + 1:]:
            if start_j > end_i:
                break
            if club_j != club_i and ev_j != ev_i:
                return True
    return False


def main():
    con = sqlite3.connect(DB_PATH)
    overrides = load_overrides(AMBIGUITY_REPORT.parent / "link_overrides.csv")
    blocked = {n for n, a in overrides.items() if a == "block"}
    merged = {n for n, a in overrides.items() if a == "merge"}
    rows = con.execute("""
        SELECT re.event_team_id, re.name, et.full_name, et.display_name,
               ev.season, ev.division, ev.start_date, ev.end_date, ev.event_id
        FROM roster_entries re
        JOIN event_teams et ON et.event_team_id = re.event_team_id
        JOIN events ev ON ev.event_id = et.event_id
    """).fetchall()

    # norm_name -> (division, season) -> set of clubs; and every roster row
    appearances = defaultdict(lambda: defaultdict(set))
    divisions_of = defaultdict(set)   # nname -> {division}, for the bridge log
    windows = defaultdict(list)   # nname -> [(club, start, end, event_id)]
    roster_rows = []  # (event_team_id, raw_name, nname, club, season, division)
    display_for = {}
    for (etid, raw_name, full_name, disp, season, division,
         start_date, end_date, event_id) in rows:
        nname = norm_name(raw_name)
        if not nname:
            continue
        club = norm_club(full_name, disp)
        appearances[nname][(division, season)].add(club)
        divisions_of[nname].add(division)
        if start_date:
            windows[nname].append((club, start_date, end_date, event_id))
        roster_rows.append((etid, raw_name, nname, club, season, division))
        display_for.setdefault(nname, re.sub(r"\s+", " ", raw_name).strip())

    # ambiguous = 2+ clubs within the SAME (division, season). Cross-division
    # same-season appearances are bridge players, not collisions.
    #
    # Ambiguity is career-wide by construction: the split key is (name, club),
    # so one colliding season splits every OTHER season too, even the clean
    # ones. 86% of ambiguous names were unambiguous in at least one
    # division-season, so the raw rule over-splits badly.
    #
    # So a raw collision only SPLITS when the shards also contradict physically
    # — different teams in overlapping date windows. Multi-club seasons without
    # that (a youth or fifth-year player on several summer rosters, a name
    # entered under two spellings of one club) collapse to one identity, which
    # stops the shards from re-debuting at the division base and re-burning a
    # provisional window each. Note the asymmetry this accepts: a conflict
    # PROVES two people, while its absence only fails to disprove them, so
    # two same-named players in different regions who never coincide now merge
    # wrongly. Reviewed `block` verdicts are the correction for those.
    raw_ambiguous = {
        nname for nname, scopes in appearances.items()
        if any(len(clubs) > 1 for clubs in scopes.values())
    }
    conflicting = {n for n in raw_ambiguous if has_date_conflict(windows[n])}
    ambiguous_names = conflicting - merged

    con.executescript(SCHEMA)
    player_ids: dict[tuple, int] = {}   # identity key -> player_id

    def player_for(nname: str, club: str, division: str) -> int:
        if nname in ambiguous_names:
            key = (nname, club)
        elif nname in blocked:          # reviewed: college/club are two people
            key = (nname, division)
        else:
            key = (nname,)
        if key not in player_ids:
            cur = con.execute(
                "INSERT INTO players (display_name, norm_name, ambiguous) VALUES (?,?,?)",
                (display_for[nname], nname, int(nname in ambiguous_names)))
            player_ids[key] = cur.lastrowid
        return player_ids[key]

    seen = set()
    for etid, raw_name, nname, club, season, division in roster_rows:
        pid = player_for(nname, club, division)
        if (etid, raw_name) not in seen:
            seen.add((etid, raw_name))
            con.execute(
                "INSERT OR IGNORE INTO roster_players (event_team_id, name, player_id) VALUES (?,?,?)",
                (etid, raw_name, pid))
    con.commit()

    # Every raw collision is logged, split or not: the auto-merged ones are now
    # the false-merge risk and need review more than the split ones do.
    with open(AMBIGUITY_REPORT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["norm_name", "verdict", "division", "season", "clubs"])
        for nname in sorted(raw_ambiguous):
            verdict = "split" if nname in ambiguous_names else "auto-merged"
            for (division, season), clubs in sorted(appearances[nname].items()):
                if len(clubs) > 1:
                    w.writerow([nname, verdict, division, season,
                                "; ".join(sorted(clubs))])

    # bridge players: same (unambiguous) name in both divisions -> one identity.
    # Highest false-merge risk in the system, so log every one for review.
    bridge_report = AMBIGUITY_REPORT.parent / "cross_division_links.csv"
    n_bridges = 0
    with open(bridge_report, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["norm_name", "college_teams", "club_teams", "review"])
        for nname in sorted(divisions_of):
            if (len(divisions_of[nname]) > 1 and nname not in ambiguous_names
                    and nname not in blocked):
                college = sorted({c for (d, s), cs in appearances[nname].items()
                                  if d == "college" for c in cs})
                club = sorted({c for (d, s), cs in appearances[nname].items()
                               if d == "club" for c in cs})
                w.writerow([nname, "; ".join(college), "; ".join(club),
                            overrides.get(nname, "")])
                n_bridges += 1

    n_players = con.execute("SELECT count(*) FROM players").fetchone()[0]
    n_links = con.execute("SELECT count(*) FROM roster_players").fetchone()[0]
    print(f"{n_players} players, {n_links} roster links; "
          f"{len(raw_ambiguous)} name collisions -> {len(ambiguous_names)} split "
          f"per-club on a same-weekend conflict, "
          f"{len(raw_ambiguous) - len(ambiguous_names)} auto-merged; "
          f"{n_bridges} college<->club bridge players\n"
          f"reports: {AMBIGUITY_REPORT}, {bridge_report}")
    con.close()


if __name__ == "__main__":
    main()
