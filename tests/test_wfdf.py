import unittest

from scraper.wfdf import (
    FetchedPage,
    merge_team_lists,
    parse_all_teams,
    parse_games,
    parse_live_games,
    parse_live_teams,
    parse_player_card,
    parse_player_index,
    parse_team_index,
)


def page(html: str) -> FetchedPage:
    return FetchedPage(html, "https://results.wfdf.sport/event/", "2026-08-07T00:00:00+00:00", "hash", True)


class WFDFParserTests(unittest.TestCase):
    def test_complete_team_list_preserves_unplaced_teams(self):
        standings = parse_team_index(page("""
            <table><tr><th>Placement</th><th>Open</th></tr>
            <tr><td>Gold</td><td><img src="flags/Belgium.png"><a href="?view=teamcard&team=1">Gentle</a></td></tr></table>
        """))
        all_teams = parse_all_teams(page("""
            <td class="tdcontent"><table>
              <tr><th>Open</th></tr>
              <tr><td><a href="?view=teamcard&team=1">Gentle</a></td><td></td><td><a href="?view=country&country=1">Belgium</a></td></tr>
              <tr><td><a href="?view=teamcard&team=2">Late withdrawal</a></td><td></td><td><a href="?view=country&country=2">France</a></td></tr>
            </table></td>
        """))
        teams = merge_team_lists(all_teams, standings)
        self.assertEqual([1, None], [team["place"] for team in teams])
        self.assertEqual(["Belgium", "France"], [team["country"] for team in teams])

    def test_player_index_and_card_recover_roster_stats(self):
        index = page("""
            <td class="tdcontent">
              <a href="?view=playercard&series=0&player=12">Listed Name</a>
              <a href="?view=playercard&series=0&player=12">Listed Name</a>
              <a href="?view=playercard&series=0&player=3">Other Name</a>
            </td>
        """)
        self.assertEqual(["3", "12"], parse_player_index(index))

        card = page("""
            <td class="tdcontent"><div class="content">
              <h1>#80 Jon Aaron</h1>
              <p>Team: <a href="?view=teamcard&team=105">Big Fish, Little Fish</a></p>
              <table><tr><th>Games</th><th>Assists</th><th>Goals</th><th>Tot.</th></tr>
                <tr><td>8</td><td>10</td><td>14</td><td>24</td></tr></table>
            </div></td>
        """)
        self.assertEqual({
            "player_id": "2382", "team_id": "105", "name": "Jon Aaron",
            "number": "80", "games": "8", "assists": "10", "points": "14",
        }, parse_player_card(card, "2382"))

    def test_game_parser_keeps_scores_and_division(self):
        teams = [
            {"team_id": "1", "name": "Gentle", "division": "club-men"},
            {"team_id": "2", "name": "Clapham", "division": "club-men"},
        ]
        games = parse_games(page("""
            <td class="tdcontent"><h3>Sun 24.7.2022</h3>
              <table><tr><th colspan="8">Open - Final</th></tr>
                <tr><td>15:00</td><td>Field 1</td><td>Gentle</td><td>15</td>
                  <td><a href="?view=gameplay&game=77">Game play</a></td>
                  <td>12</td><td>Clapham</td><td></td></tr>
              </table>
            </td>
        """), teams)
        self.assertEqual("2022-07-24", games[0]["date"])
        self.assertEqual(("1", "2"), (games[0]["home_team_id"], games[0]["away_team_id"]))
        self.assertEqual((15, 12, "played"), (games[0]["home_score"], games[0]["away_score"], games[0]["state"]))
    def test_game_parser_assigns_stable_ids_without_game_links(self):
        teams = [
            {"team_id": "1", "name": "Gentle", "division": "club-men"},
            {"team_id": "2", "name": "Clapham", "division": "club-men"},
        ]
        html = """
            <td class="tdcontent"><h3>Sun 24.7.2022</h3>
              <table><tr><th colspan="8">Open - Final</th></tr>
                <tr><td>15:00</td><td>Field 1</td><td>Gentle</td><td>15</td>
                  <td>-</td><td>12</td><td>Clapham</td><td></td></tr>
              </table>
            </td>
        """
        first = parse_games(page(html), teams)
        second = parse_games(page(html), teams)
        self.assertEqual(first[0]["game_id"], second[0]["game_id"])
        self.assertTrue(first[0]["game_id"].startswith("row-"))

    def test_live_parser_maps_series_and_incomplete_scores(self):
        reference = {
            "series": [{"series_id": 2, "name": "Open"}],
            "pools": [{"pool_id": 1, "poolname": "Pool A"}],
            "countries": [{"country_id": 1, "name": "Belgium"}],
            "teams": [
                {"team_id": 10, "name": "Gentle", "country": 1, "series": 2},
                {"team_id": 11, "name": "Clapham", "country": 1, "series": 2},
            ],
        }
        games = parse_live_games({
            "games": [{
                "game_id": 7,
                "hometeam": 10,
                "visitorteam": 11,
                "homescore": 15,
                "time": "2025-09-01 12:00:00",
                "pool": 1,
            }],
        }, reference)
        teams = parse_live_teams(reference)
        self.assertEqual(2, len(teams))
        self.assertEqual("club-men", games[0]["division"])
        self.assertEqual("scheduled", games[0]["state"])
        self.assertIsNone(games[0]["away_score"])


if __name__ == "__main__":
    unittest.main()
