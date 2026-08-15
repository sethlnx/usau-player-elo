"""Load the complete normalized corpus shared by rating experiments."""

from __future__ import annotations

import sqlite3

from analysis.backtest import (
    DB_PATH, load_games, load_maps, load_stat_events, load_ufa_stat_events,
)
from analysis.euf_ratings import EUF_DB, load_european_inputs, merge_inputs
from analysis.canada_ratings import load_canadian_inputs
from analysis.international_ratings import load_international_inputs
from elo.engine import EloConfig
from womens_pro.ratings import load_womens_pro_inputs


def load_corpus(cfg: EloConfig | None = None):
    if cfg is None:
        from analysis.rankings import PUBLISHED
        cfg = EloConfig(**PUBLISHED)
    con = sqlite3.connect(DB_PATH)
    try:
        european = load_european_inputs(con, EUF_DB)
        canadian = load_canadian_inputs(EUF_DB)
        international = load_international_inputs(con, european, EUF_DB)
        womens_pro = load_womens_pro_inputs(con)
        external = merge_inputs(european, international, canadian, womens_pro)
        games = sorted(
            [*load_games(con), *external.games], key=lambda game: game["sort"]
        )
        rosters, clubs = load_maps(con)
        rosters.update(external.rosters)
        clubs.update(external.clubs)
        stat_events = sorted(
            load_stat_events(con) + load_ufa_stat_events(con, cfg),
            key=lambda event: event[0],
        )
        return games, rosters, clubs, stat_events
    finally:
        con.close()
