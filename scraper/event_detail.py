"""Parse one event's schedule (club men/women/mixed, or college): games and teams."""

import html as htmllib
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import fetch

# URL path segment per division. The college slugs have no hyphen (verified
# against a live event both ways: /schedule/Men/CollegeMen/ and
# /schedule/Women/CollegeWomen/ serve, and both hyphenated forms 404). The
# club gender slugs do carry it — /schedule/Mixed/Club-Mixed/ and
# /schedule/Women/Club-Women/ both serve, their unhyphenated forms 404. D-III
# is not a separate path on either level — USAU files it under the same
# College schedule, so the split is by event NAME, done in build_db.
DIVISION_PATHS = {"club-men": "Men/Club-Men", "college": "Men/CollegeMen",
                  "college-d3": "Men/CollegeMen",
                  "college-women": "Women/CollegeWomen",
                  "college-women-d3": "Women/CollegeWomen",
                  "club-women": "Women/Club-Women",
                  "club-mixed": "Mixed/Club-Mixed"}


def schedule_url(event_url: str, division: str = "club-men") -> str:
    return event_url.rstrip("/") + "/schedule/" + DIVISION_PATHS[division] + "/"


def fetch_schedule(event_url: str, division: str = "club-men", session=None,
                   refresh: bool = False) -> str | None:
    """Schedule HTML for the division, or None if the event has no page."""
    try:
        return fetch.get(schedule_url(event_url, division), session=session, refresh=refresh)
    except fetch.NotFound:
        return None


def _team_from_link(a) -> tuple[str, str] | tuple[None, None]:
    """(event_team_id, display_name) from an EventTeamId link."""
    if a is None or "EventTeamId=" not in (a.get("href") or ""):
        return None, None
    team_id = htmllib.unescape(a["href"].split("EventTeamId=")[1])
    name = re.sub(r"\s*\(\d+\)\s*$", "", a.get_text(strip=True))  # strip seed "(3)"
    return team_id, name


def _game_id(el) -> str | None:
    a = el.find("a", href=re.compile("EventGameId="))
    if a is None:
        return None
    return htmllib.unescape(a["href"].split("EventGameId=")[1])


def _int_or_none(text: str):
    text = text.strip()
    return int(text) if text.isdigit() else None


def parse_games(schedule_html: str, event_year: int) -> tuple[list[dict], dict]:
    """Returns (games, teams). teams maps event_team_id -> display name.

    Pool-play games are <tr data-game=...> rows; bracket games are
    <div class="bracket_game"> blocks. Both carry data-type spans.

    Each game also carries the page's own SLOT id (the pool row's data-game,
    the bracket div's id) — the fixture's identity, stable across the
    tournament. game_key is not: an unplayed slot has no EventGameId link, so
    it falls back to a synthetic key, and the moment USAU seeds the slot the
    key becomes the real game id. Keyed on game_key alone a mid-event refetch
    inserts the seeded game beside the placeholder it replaces; build_db uses
    the slot to drop the stale row.
    """
    soup = BeautifulSoup(schedule_html, "lxml")
    games, teams = [], {}

    def record_team(a):
        tid, name = _team_from_link(a)
        if tid:
            teams[tid] = name
        return tid

    # ---- pool play rows ----
    for row in soup.find_all("tr", attrs={"data-game": True}):
        get = lambda t: row.find(attrs={"data-type": t})
        home_a = (get("game-team-home") or row).find("a", href=re.compile("EventTeamId="))
        away_a = (get("game-team-away") or row).find("a", href=re.compile("EventTeamId="))
        date_el, time_el = get("game-date"), get("game-time")
        status_el = get("game-status")
        pool = None
        table = row.find_parent("table")
        if table is not None:
            header = table.find_previous(["h3", "h4"])
            if header is not None:
                pool = header.get_text(strip=True)
        date_str = date_el.get_text(strip=True) if date_el else ""
        m = re.search(r"(\d{1,2})/(\d{1,2})", date_str)
        iso_date = f"{event_year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None
        games.append({
            "game_key": _game_id(row) or f"pool-{row['data-game']}",
            "slot": row["data-game"],
            "stage": pool or "pool",
            "date": iso_date,
            "time": time_el.get_text(strip=True) if time_el else None,
            "home_id": record_team(home_a),
            "away_id": record_team(away_a),
            "home_score": _int_or_none(get("game-score-home").get_text()) if get("game-score-home") else None,
            "away_score": _int_or_none(get("game-score-away").get_text()) if get("game-score-away") else None,
            "status": status_el.get_text(strip=True) if status_el else None,
        })

    # ---- bracket games ----
    for div in soup.find_all("div", class_="bracket_game"):
        get = lambda t: div.find(attrs={"data-type": t})
        home_a = (get("game-team-home") or div).find("a", href=re.compile("EventTeamId="))
        away_a = (get("game-team-away") or div).find("a", href=re.compile("EventTeamId="))
        col = div.find_previous("h4", class_="col_title")
        status_el = div.find("span", class_="game-status")
        date_el = div.find("span", class_="date")
        iso_date, time_str = None, None
        if date_el is not None:
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\s*(.*)", date_el.get_text(strip=True))
            if m:
                iso_date = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
                time_str = m.group(4).strip() or None
        games.append({
            "game_key": _game_id(div) or f"bracket-{div.get('id', '')}",
            "slot": div.get("id"),
            "stage": col.get_text(strip=True) if col else "bracket",
            "date": iso_date,
            "time": time_str,
            "home_id": record_team(home_a),
            "away_id": record_team(away_a),
            "home_score": _int_or_none(get("game-score-home").get_text()) if get("game-score-home") else None,
            "away_score": _int_or_none(get("game-score-away").get_text()) if get("game-score-away") else None,
            "status": status_el.get_text(strip=True) if status_el else None,
        })

    return games, teams
