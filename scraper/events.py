"""Enumerate USAU events for a competition level + season via the event search form."""

import re

import requests
from bs4 import BeautifulSoup

from . import fetch

SEARCH_URL = fetch.BASE_URL + "/events/tournament/?ViewAll=false&IsLeagueType=false&IsClinic=false&FilterByCategory=AE"

# Read off the live drpCompetitionLevelId dropdown, never inferred — the ids
# are not sequential by any rule ("Club-Mixed" is 7, "College-Mixed" is 4).
COMPETITION_LEVELS = {
    "Club-Men": "21", "Club-Women": "22", "Club-Mixed": "7",
    "College-Men": "27", "College-Women": "28",
}
# Site dropdown value per season, read off CT_HP_Mid_1$drpSeasonId — NOT derivable.
# It looks like `year - 2005` from 2019 up, but the sequence breaks below that:
# 2018 is "8" and 2017 is "7", while "13" and "12" are 2006 and 2007. Guessing
# the pattern silently scrapes decade-old events stamped with a recent season and
# raises nothing. Verify any new entry against the live dropdown before adding it.
SEASON_IDS = {
    2014: "4", 2015: "5", 2016: "6",
    2017: "7", 2018: "8", 2019: "14", 2020: "15",
    2021: "16", 2022: "17", 2023: "18", 2024: "19",
    2025: "20", 2026: "21", 2027: "22",
}
# 2014-2016 verified against the live dropdown AND enumerated: 68, 64 and 75
# Club-Men events, the same order as 2017+, so they scrape like any other year.
#
# The dropdown ALSO offers 2013 "3", 2012 "2", 2011 "1", 2010 "9", 2009 "10",
# 2008 "11", 2007 "12" and 2006 "13". Do not bother: those seasons are EMPTY.
# Enumerated every one of them — Club-Men holds a single "World Ultimate Club
# Championships" record in 2006 and another in 2010, zero events in 2007-2009
# and 2011-2013, and both WUCC records serve a schedule page with 0 games and
# 0 teams. College-Men, Club-Women and Club-Mixed are the same or emptier.
# USAU's event/roster system begins in 2014; the earlier seasons are dropdown
# scaffolding for a few international competitions it logged without results.
# 2014 is therefore a floor imposed by the SOURCE, not by any scraper — the
# GraphQL mirror independently starts there too (66 of these same 68 events).


def _form_data(page_html: str, season: int, competition_level: str,
               event_target: str | None = None) -> dict:
    """Full form payload: every field the page would submit, filters overridden."""
    data = fetch.form_defaults(page_html)
    data.update({
        "CT_HP_Mid_1$drpCompetitionLevelId": COMPETITION_LEVELS[competition_level],
        "CT_HP_Mid_1$drpSeasonId": SEASON_IDS[season],
    })
    if event_target is None:
        data["CT_HP_Mid_1$btnSubmit"] = "Search"
        data["__EVENTTARGET"] = ""
    else:
        data["__EVENTTARGET"] = event_target
    return data


GRID_IDS = ("CT_HP_Mid_1_gvCurrentUpcomingEvents", "CT_HP_Mid_1_gvPastEvents")
_POSTBACK_RE = re.compile(r"__doPostBack\('([^']+)'")


def _parse_event_rows(html: str) -> list[dict]:
    """Parse both the upcoming and past event grids into event dicts."""
    soup = BeautifulSoup(html, "lxml")
    events = []
    for grid_id in GRID_IDS:
        grid = soup.find(id=grid_id)
        if grid is None:
            continue
        for row in grid.find_all("tr"):
            link = row.find("a", href=re.compile(r"^/events/"))
            if link is None:
                continue
            tds = row.find_all("td")
            groups = [li.get_text(" ", strip=True) for li in row.find_all("li")]
            events.append({
                "name": link.get_text(strip=True),
                "url": fetch.BASE_URL + link["href"],
                "city": tds[2].get_text(strip=True) if len(tds) > 2 else "",
                "state": tds[3].get_text(strip=True) if len(tds) > 3 else "",
                "groups": groups,
                "dates": tds[5].get_text(strip=True) if len(tds) > 5 else "",
            })
    return events


def _pager_links(html: str) -> dict[str, dict[int, str]]:
    """grid id -> {page number: postback target} from each grid's pager row.

    Both grids paginate independently (25 upcoming / 20 past per page) and the
    ctlNN index of a page link shifts with the current page, so pages are
    tracked by their printed number, never by target string.
    """
    soup = BeautifulSoup(html, "lxml")
    links = {}
    for grid_id in GRID_IDS:
        grid = soup.find(id=grid_id)
        if grid is None:
            continue
        pages = {}
        for a in grid.find_all("a", href=_POSTBACK_RE):
            label = a.get_text(strip=True)
            if label.isdigit():
                pages[int(label)] = _POSTBACK_RE.search(a["href"]).group(1)
        links[grid_id] = pages
    return links


def list_events(season: int, competition_level: str = "Club-Men",
                session: requests.Session | None = None,
                refresh: bool = False) -> list[dict]:
    """All events for the given season/level, walking every result page.

    Pass refresh=True for an in-progress season: its cached search postbacks
    are stale (new events keep appearing until the season ends).
    """
    own_session = session is None
    session = session or requests.Session()
    try:
        landing = fetch.get(SEARCH_URL, session=session, refresh=True)
        first = fetch.post(SEARCH_URL, _form_data(landing, season, competition_level),
                           session, refresh=refresh)

        events = _parse_event_rows(first)
        current = first
        visited = {grid_id: {1} for grid_id in GRID_IDS}
        while True:
            nxt = next(((grid_id, page, target)
                        for grid_id, pages in _pager_links(current).items()
                        for page, target in sorted(pages.items())
                        if page not in visited[grid_id]), None)
            if nxt is None:
                break
            grid_id, page, target = nxt
            visited[grid_id].add(page)
            data = _form_data(current, season, competition_level, event_target=target)
            current = fetch.post(SEARCH_URL, data, session, refresh=refresh)
            events.extend(_parse_event_rows(current))

        # de-dupe on URL (upcoming grid + pagination can overlap)
        unique = {e["url"]: e for e in events}
        return list(unique.values())
    finally:
        if own_session:
            session.close()
