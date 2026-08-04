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
- EXCEPT across gender divisions. USAU's men's divisions (club, college,
  college-d3) and its women's division are not two halves of one career: a
  name in both is two people, so those shards are always split. Mixed bridges
  to either freely, which is what puts every division on one rating scale.
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
from collections import Counter, defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "usau.db"
AMBIGUITY_REPORT = DB_PATH.parent / "ambiguities.csv"

# Gender-matching groups. Every division here sits on a Men's or a Women's
# competition level (see scraper.build_db.DIVISIONS), so "has played women's"
# is a division fact, not an inference. Mixed belongs to neither: it is the
# bridge, and a mixed player's group is read off the Pronouns column USAU
# publishes on the roster page.
MENS_DIVISIONS = {"club-men", "college", "college-d3"}
WOMENS_DIVISIONS = {"club-women", "college-women", "college-women-d3"}
MIXED_DIVISIONS = {"club-mixed"}


def division_gender(division: str) -> str | None:
    if division in MENS_DIVISIONS:
        return "m"
    if division in WOMENS_DIVISIONS:
        return "w"
    return None


def pronoun_gender(pronouns: str | None) -> str | None:
    """'H' -> m, 'S/T' -> w, anything ambiguous or absent -> None.

    USAU stores the roster Pronouns column as slash-joined initials: H(e),
    S(he), T(hey), Z(e). Only a clean he-or-she reading is used. 'T' alone,
    the Z forms and the 42 rows carrying both H and S say nothing about which
    gender-matching ratio the player counts against, so they stay unknown
    rather than being guessed into a bucket.
    """
    if not pronouns:
        return None
    tokens = {t.strip().upper() for t in pronouns.split("/") if t.strip()}
    has_h, has_s = "H" in tokens, "S" in tokens
    if has_h and not has_s:
        return "m"
    if has_s and not has_h:
        return "w"
    return None


# First-name likelihood fallback, applied only after division and pronoun
# evidence are exhausted. Both thresholds were measured, not picked: see the
# note at the call site in main().
NAME_MIN_SIGHTINGS = 5
NAME_MIN_SHARE = 0.95


def first_name(display_name: str) -> str:
    """ASCII-folded lowercase first token — the key the prior is built on."""
    s = unicodedata.normalize("NFKD", display_name)
    s = s.encode("ascii", "ignore").decode().strip().lower()
    parts = s.split()
    return parts[0] if parts else ""


def name_gender(name_counts, class_total, fn: str) -> str | None:
    """P(first name | gender) vote, or None when the name is too rare or mixed.

    Compares likelihoods rather than the raw male share so the 83/17 class
    imbalance of the labelled pool cannot drag every borderline name male.
    """
    seen = name_counts.get(fn)
    if not seen or seen["m"] + seen["w"] < NAME_MIN_SIGHTINGS:
        return None
    lm = seen["m"] / max(class_total["m"], 1)
    lw = seen["w"] / max(class_total["w"], 1)
    if lm + lw == 0:
        return None
    gender, share = ("m", lm / (lm + lw)) if lm >= lw else ("w", lw / (lm + lw))
    return gender if share >= NAME_MIN_SHARE else None


SCHEMA = """
DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS roster_players;
CREATE TABLE players (
    player_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    ambiguous INTEGER DEFAULT 0,
    gender TEXT NOT NULL DEFAULT '',
    gender_source TEXT NOT NULL DEFAULT ''
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
               ev.season, ev.division, ev.start_date, ev.end_date, ev.event_id,
               re.pronouns
        FROM roster_entries re
        JOIN event_teams et ON et.event_team_id = re.event_team_id
        JOIN events ev ON ev.event_id = et.event_id
    """).fetchall()

    # norm_name -> (division, season) -> set of clubs; and every roster row
    appearances = defaultdict(lambda: defaultdict(set))
    divisions_of = defaultdict(set)   # nname -> {division}, for the bridge log
    windows = defaultdict(list)   # nname -> [(club, start, end, event_id)]
    roster_rows = []  # (event_team_id, raw_name, nname, club, season, division, pg)
    display_for = {}
    # nname -> {'m','w'} from DIVISION play only, which is the hard evidence.
    genders_of = defaultdict(set)
    for (etid, raw_name, full_name, disp, season, division,
         start_date, end_date, event_id, pronouns) in rows:
        nname = norm_name(raw_name)
        if not nname:
            continue
        club = norm_club(full_name, disp)
        appearances[nname][(division, season)].add(club)
        divisions_of[nname].add(division)
        dg = division_gender(division)
        if dg:
            genders_of[nname].add(dg)
        if start_date:
            windows[nname].append((club, start_date, end_date, event_id))
        roster_rows.append((etid, raw_name, nname, club, season, division,
                            pronoun_gender(pronouns)))
        display_for.setdefault(nname, re.sub(r"\s+", " ", raw_name).strip())

    # A name played in BOTH a men's division and the women's division is two
    # people, not a bridge — 275 names, and no body is eligible for both
    # series. Their shards are split by gender group, and their mixed rows are
    # routed by the roster page's own Pronouns column. Mixed rows that pronouns
    # cannot place get a third shard: guessing would corrupt a real career,
    # while an unplaced shard only loses the link (see the asymmetry note
    # below). 181 of the 275 also play mixed, so this is a handful of rows.
    split_gender = {n for n, gs in genders_of.items() if len(gs) > 1}

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
    display_of: dict[int, str] = {}     # player_id -> display name, for the name prior

    def player_for(nname: str, club: str, division: str, pg: str | None) -> int:
        if nname in ambiguous_names:
            key = (nname, club)
        elif nname in blocked:          # reviewed: college/club are two people
            key = (nname, division)
        elif nname in split_gender:
            key = (nname, division_gender(division) or pg or "?")
        else:
            key = (nname,)
        if key not in player_ids:
            cur = con.execute(
                "INSERT INTO players (display_name, norm_name, ambiguous) VALUES (?,?,?)",
                (display_for[nname], nname, int(nname in ambiguous_names)))
            player_ids[key] = cur.lastrowid
            display_of[cur.lastrowid] = display_for[nname]
        return player_ids[key]

    # Gender-matching evidence per resolved identity. Division play is
    # decisive: any women's-division appearance makes a player female-matching,
    # any men's-division appearance male-matching, and the split above
    # guarantees no identity now holds both. Mixed-only players fall back to
    # the pronoun majority across their roster rows, and stay unknown when
    # that is silent or contradictory.
    seen = set()
    div_evidence: dict[int, set] = defaultdict(set)
    pro_evidence: dict[int, list] = defaultdict(list)
    for etid, raw_name, nname, club, season, division, pg in roster_rows:
        pid = player_for(nname, club, division, pg)
        dg = division_gender(division)
        if dg:
            div_evidence[pid].add(dg)
        elif pg:
            pro_evidence[pid].append(pg)
        if (etid, raw_name) not in seen:
            seen.add((etid, raw_name))
            con.execute(
                "INSERT OR IGNORE INTO roster_players (event_team_id, name, player_id) VALUES (?,?,?)",
                (etid, raw_name, pid))

    genders, sources = {}, {}
    for pid in player_ids.values():
        dg = div_evidence.get(pid, set())
        if len(dg) == 1:
            genders[pid], sources[pid] = next(iter(dg)), "division"
            continue
        votes = pro_evidence.get(pid, [])
        m, w = votes.count("m"), votes.count("w")
        g = "m" if m > w else "w" if w > m else ""
        genders[pid] = g
        sources[pid] = "pronouns" if g else ""

    # Third pass, for mixed-only players USAU published no pronouns for: a
    # first-name likelihood learned from the players the first two passes DID
    # place. 11,010 identities reach here, and dropping them all would empty
    # the site's gender filter of a tenth of the corpus.
    #
    # It must be a LIKELIHOOD, P(name | gender), not the raw share of a name
    # that is male. The labelled pool is 83% men, so a posterior threshold
    # passes male names trivially and female names almost never: it placed the
    # unplaced at 4.7 men per woman. Dividing by each class's total removes
    # that, and the calibration check is external — mixed rosters are
    # gender-balanced by USAU's ratio rules, so the answer should come out near
    # even. It does: 1.09 men per woman.
    #
    # Held out on a BALANCED half of the labelled pool (balanced for the same
    # reason), min 5 sightings and a 0.95 likelihood share score 98.6% accurate
    # at 63% coverage; on the real unplaced population it places 72%. The other
    # 3,124 keep gender='' and show only under "all genders" — a rare first
    # name is not evidence of anything.
    name_counts, class_total = defaultdict(Counter), Counter()
    for pid, g in genders.items():
        if g:
            name_counts[first_name(display_of[pid])][g] += 1
            class_total[g] += 1
    for pid, g in genders.items():
        if g:
            continue
        guess = name_gender(name_counts, class_total, first_name(display_of[pid]))
        if guess:
            genders[pid], sources[pid] = guess, "name"
    con.executemany("UPDATE players SET gender=?, gender_source=? WHERE player_id=?",
                    [(g, sources[pid], pid) for pid, g in genders.items() if g])
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

    # bridge players: same (unambiguous) name in two or more divisions -> one
    # identity. Highest false-merge risk in the system, so log every one for
    # review. Gender-split names are excluded: they are no longer one identity.
    bridge_report = AMBIGUITY_REPORT.parent / "cross_division_links.csv"
    n_bridges = 0
    with open(bridge_report, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["norm_name", "divisions", "teams", "review"])
        for nname in sorted(divisions_of):
            if (len(divisions_of[nname]) > 1 and nname not in ambiguous_names
                    and nname not in blocked and nname not in split_gender):
                teams = sorted({c for _k, cs in appearances[nname].items()
                                for c in cs})
                w.writerow([nname, "; ".join(sorted(divisions_of[nname])),
                            "; ".join(teams), overrides.get(nname, "")])
                n_bridges += 1

    n_players = con.execute("SELECT count(*) FROM players").fetchone()[0]
    n_links = con.execute("SELECT count(*) FROM roster_players").fetchone()[0]
    by_gender = dict(con.execute(
        "SELECT gender, count(*) FROM players GROUP BY gender"))
    print(f"{n_players} players, {n_links} roster links; "
          f"{len(raw_ambiguous)} name collisions -> {len(ambiguous_names)} split "
          f"per-club on a same-weekend conflict, "
          f"{len(raw_ambiguous) - len(ambiguous_names)} auto-merged; "
          f"{len(split_gender)} names split across men's/women's; "
          f"{n_bridges} cross-division bridge players\n"
          f"gender-matching: {by_gender.get('m', 0)} male, "
          f"{by_gender.get('w', 0)} female, {by_gender.get('', 0)} unknown "
          f"(by " + ", ".join(
              f"{src or 'none'} {n}" for src, n in con.execute(
                  "SELECT gender_source, count(*) FROM players "
                  "WHERE gender<>'' GROUP BY gender_source ORDER BY 2 DESC")) + ")\n"
          f"reports: {AMBIGUITY_REPORT}, {bridge_report}")
    con.close()


if __name__ == "__main__":
    main()
