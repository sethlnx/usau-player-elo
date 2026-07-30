"""Parse an event-team page: team profile + event roster (players)."""

from bs4 import BeautifulSoup

from . import fetch

TEAM_URL = fetch.BASE_URL + "/events/teams/?EventTeamId="


def fetch_team_page(event_team_id: str, session=None) -> str:
    return fetch.get(TEAM_URL + event_team_id, session=session)


def parse_team_page(page_html: str) -> dict:
    """Returns {"team": {...}, "players": [...]}."""
    soup = BeautifulSoup(page_html, "lxml")

    team: dict = {}
    profile = soup.find(class_="profile_info")
    if profile is not None:
        h4 = profile.find("h4")
        if h4 is not None:
            team["name"] = h4.get_text(strip=True)
    for key, dl_id in (
        ("competition_level", "CT_Main_0_ucTeamDetails_dlCompetitionLevel"),
        ("gender_division", "CT_Main_0_ucTeamDetails_dlGenderDivision"),
        ("city", "CT_Main_0_ucTeamDetails_dlCity"),
    ):
        el = soup.find(id=dl_id)
        if el is not None:
            dd = el.find("dd")
            team[key] = (dd.get_text(strip=True) if dd is not None
                         else el.get_text(" ", strip=True))

    players = []
    table = soup.find(id="CT_Main_0_ucTeamDetails_gvList")
    if table is not None:
        headers = [th.get_text(strip=True).split("Mere")[0] or th.get_text(strip=True)
                   for th in table.find_all("th")]
        for row in table.find_all("tr"):
            tds = row.find_all("td")
            if not tds:
                continue
            values = [td.get_text(" ", strip=True).replace("\xa0", "") for td in tds]
            player = dict(zip(headers, values))
            name = player.get("Player", "").strip()
            if not name:
                continue
            players.append({
                "number": player.get("No.", "").strip(),
                "name": name,
                "pronouns": player.get("Pronouns", "").strip(),
                "position": player.get("Position", "").strip(),
                "height": player.get("Height", "").strip(),
                "points": player.get("Points", "").strip(),
                "assists": player.get("Assists", "").strip(),
                "ds": player.get("Ds", "").strip(),
                "turns": player.get("Turns", "").strip(),
            })

    return {"team": team, "players": players}
