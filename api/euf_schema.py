"""Bounded read-only GraphQL schema over the normalized European SQLite DB."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import strawberry
from strawberry.dataloader import DataLoader

from scraper.euf import EUF_DB

MAX_PAGE = 200
DEFAULT_PAGE = 50
PLAYED_STATES = ("played", "has_outcome", "forfeit")


def db_path() -> Path:
    return Path(os.environ.get("EUF_DB", EUF_DB))


def connect_readonly(path: str | Path | None = None) -> sqlite3.Connection:
    resolved = Path(path or db_path()).resolve()
    con = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def page_args(first: int, offset: int) -> tuple[int, int]:
    if not 1 <= first <= MAX_PAGE:
        raise ValueError(f"first must be between 1 and {MAX_PAGE}")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    return first, offset


@strawberry.type
class PageInfo:
    first: int
    offset: int
    has_next_page: bool


@strawberry.type
class SourceRef:
    source: str
    source_id: str
    source_url: str
    observed_at: str
    payload_hash: str


@strawberry.type
class Game:
    id: strawberry.ID
    event_id: strawberry.ID
    stage: str | None
    field: str | None
    date: str | None
    time: str | None
    home_team_id: strawberry.ID | None
    home_team: str | None
    away_team_id: strawberry.ID | None
    away_team: str | None
    home_score: int | None
    away_score: int | None
    status: str
    played: bool
    sources: list[SourceRef]


@strawberry.type
class GameConnection:
    nodes: list[Game]
    total_count: int
    page_info: PageInfo


@strawberry.type
class Standing:
    place: int
    division: str
    team_id: strawberry.ID
    team: str
    source: str


@strawberry.type
class Team:
    id: strawberry.ID
    name: str
    country: str | None
    sources: list[SourceRef]
    _db: strawberry.Private[str]

    @strawberry.field
    def roster_available(self) -> bool | None:
        with closing(connect_readonly(self._db)) as con:
            states = [row[0] for row in con.execute(
                """SELECT DISTINCT r.state
                   FROM event_teams t
                   JOIN roster_availability r USING(event_id)
                   WHERE t.canonical_team_id=?""", (str(self.id),)
            )]
            ranking_counts = [row[0] for row in con.execute(
                """SELECT record_count FROM ranking_roster_observations
                   WHERE team_id=?""", (str(self.id),)
            )]
        if "public" in states or any(ranking_counts):
            return True
        if "empty" in states or ranking_counts:
            return False
        return None

    @strawberry.field
    def players(self) -> list[str] | None:
        available = self.roster_available()
        if available is None:
            return None
        with closing(connect_readonly(self._db)) as con:
            return [row[0] for row in con.execute(
                """SELECT name FROM (
                     SELECT r.name name
                     FROM roster_entries r
                     JOIN event_teams t USING(event_team_id)
                     WHERE t.canonical_team_id=?
                     UNION
                     SELECT r.player_name name
                     FROM ranking_roster_entries r
                     JOIN ranking_roster_observations o
                       ON o.source=r.source AND o.roster_id=r.roster_id
                     WHERE o.team_id=?
                   ) ORDER BY name""",
                (str(self.id), str(self.id)),
            )]


@strawberry.type
class TeamConnection:
    nodes: list[Team]
    total_count: int
    page_info: PageInfo


@strawberry.type
class Event:
    id: strawberry.ID
    event_code: str
    name: str
    season: int
    divisions: list[str]
    start_date: str | None
    end_date: str | None
    roster_state: str | None
    sources: list[SourceRef]
    _db: strawberry.Private[str]

    @strawberry.field
    async def games(
        self,
        info: strawberry.Info,
        first: int = DEFAULT_PAGE,
        offset: int = 0,
        played: bool | None = None,
    ) -> GameConnection:
        context = info.context if isinstance(info.context, dict) else {}
        loader = context.get("event_games")
        if loader is None:
            loader = event_game_loader(self._db)
            context["event_games"] = loader
        return await loader.load((str(self.id), first, offset, played))

    @strawberry.field
    def standings(self) -> list[Standing]:
        with closing(connect_readonly(self._db)) as con:
            rows = con.execute(
                """SELECT s.place,s.division,t.canonical_team_id,t.display_name,s.source
                   FROM standings s JOIN event_teams t USING(event_team_id)
                   WHERE s.event_id=? ORDER BY s.place""", (str(self.id),)
            ).fetchall()
        return [Standing(
            place=row["place"], division=row["division"],
            team_id=strawberry.ID(row["canonical_team_id"]),
            team=row["display_name"], source=row["source"],
        ) for row in rows]


@strawberry.type
class EventConnection:
    nodes: list[Event]
    total_count: int
    page_info: PageInfo


def _sources(
    con: sqlite3.Connection, entity_type: str, local_key: str
) -> list[SourceRef]:
    return [SourceRef(
        source=row["source"], source_id=row["source_id"],
        source_url=row["source_url"], observed_at=row["observed_at"],
        payload_hash=row["payload_hash"],
    ) for row in con.execute(
        """SELECT source,source_id,source_url,observed_at,payload_hash
           FROM source_entities WHERE entity_type=? AND local_key=?
           ORDER BY source,source_id""", (entity_type, local_key)
    )]

def _source_map(
    con: sqlite3.Connection, entity_type: str, local_keys: list[str]
) -> dict[str, list[SourceRef]]:
    grouped = {key: [] for key in local_keys}
    if not local_keys:
        return grouped
    marks = ",".join("?" for _ in local_keys)
    rows = con.execute(
        f"""SELECT local_key,source,source_id,source_url,observed_at,payload_hash
            FROM source_entities WHERE entity_type=? AND local_key IN ({marks})
            ORDER BY source,source_id""",
        [entity_type, *local_keys],
    )
    for row in rows:
        grouped[row["local_key"]].append(SourceRef(
            source=row["source"], source_id=row["source_id"],
            source_url=row["source_url"], observed_at=row["observed_at"],
            payload_hash=row["payload_hash"],
        ))
    return grouped


def _event(
    con: sqlite3.Connection,
    row: sqlite3.Row,
    path: str,
    sources: list[SourceRef] | None = None,
) -> Event:
    key = str(row["event_id"])
    if sources is None:
        sources = _sources(con, "event", key)
        sources.extend(_sources(con, "division", key))
    sources = list({
        (ref.source, ref.source_id, ref.source_url, ref.payload_hash): ref
        for ref in sources
    }.values())
    return Event(
        id=strawberry.ID(key), event_code=row["event_code"], name=row["name"],
        season=row["season"], divisions=[row["division"]],
        start_date=row["start_date"], end_date=row["end_date"],
        roster_state=row["roster_state"], sources=sources, _db=path,
    )


def _filters(
    *,
    source: str | None = None,
    event_code: str | None = None,
    season: int | None = None,
    division: str | None = None,
    team: str | None = None,
    played: bool | None = None,
) -> tuple[str, list[Any]]:
    clauses, params = ["1=1"], []
    if source:
        clauses.append("EXISTS (SELECT 1 FROM source_entities sx WHERE "
                       "sx.local_key=CAST(e.event_id AS TEXT) AND "
                       "sx.entity_type IN ('event','division') AND sx.source=?)")
        params.append(source)
    if event_code:
        clauses.append("d.event_code=?")
        params.append(event_code)
    if season is not None:
        clauses.append("e.season=?")
        params.append(season)
    if division:
        clauses.append("e.division=?")
        params.append(division)
    if team:
        clauses.append("EXISTS (SELECT 1 FROM event_teams ft WHERE "
                       "ft.event_id=e.event_id AND lower(ft.display_name) LIKE ?)")
        params.append(f"%{team.lower()}%")
    if played is not None:
        marks = ",".join("?" for _ in PLAYED_STATES)
        op = "IN" if played else "NOT IN"
        clauses.append(f"EXISTS (SELECT 1 FROM games fg WHERE fg.event_id=e.event_id "
                       f"AND fg.status {op} ({marks}))")
        params.extend(PLAYED_STATES)
    return " AND ".join(clauses), params


def load_events(
    path: str,
    *,
    first: int = DEFAULT_PAGE,
    offset: int = 0,
    source: str | None = None,
    event_code: str | None = None,
    season: int | None = None,
    division: str | None = None,
    team: str | None = None,
    played: bool | None = None,
) -> EventConnection:
    first, offset = page_args(first, offset)
    where, params = _filters(
        source=source, event_code=event_code, season=season,
        division=division, team=team, played=played,
    )
    base = " FROM events e JOIN euf_event_details d USING(event_id) WHERE " + where
    with closing(connect_readonly(path)) as con:
        total = con.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
        rows = con.execute(
            """SELECT e.event_id,e.name,e.season,e.division,e.start_date,e.end_date,
                      d.event_code,d.roster_state""" + base +
            " ORDER BY e.start_date DESC,e.event_id LIMIT ? OFFSET ?",
            [*params, first, offset],
        ).fetchall()
        keys = [str(row["event_id"]) for row in rows]
        event_sources = _source_map(con, "event", keys)
        division_sources = _source_map(con, "division", keys)
        nodes = [
            _event(con, row, path,
                   event_sources[key] + division_sources[key])
            for row, key in zip(rows, keys)
        ]
    return EventConnection(
        nodes=nodes, total_count=total,
        page_info=PageInfo(first=first, offset=offset,
                           has_next_page=offset + first < total),
    )


def _game_nodes(
    con: sqlite3.Connection, rows: list[sqlite3.Row]
) -> list[Game]:
    keys = [f"{row['event_id']}|{row['game_key']}" for row in rows]
    game_sources = _source_map(con, "game", keys)
    return [Game(
        id=strawberry.ID(key),
        event_id=strawberry.ID(str(row["event_id"])), stage=row["stage"],
        field=row["slot"], date=row["date"], time=row["time"],
        home_team_id=strawberry.ID(row["home_team_id"]) if row["home_team_id"] else None,
        home_team=row["home_team"],
        away_team_id=strawberry.ID(row["away_team_id"]) if row["away_team_id"] else None,
        away_team=row["away_team"], home_score=row["home_score"],
        away_score=row["away_score"], status=row["status"],
        played=row["status"] in PLAYED_STATES,
        sources=game_sources[key],
    ) for row, key in zip(rows, keys)]


def load_games(
    path: str,
    *,
    first: int = DEFAULT_PAGE,
    offset: int = 0,
    event_id: str | None = None,
    source: str | None = None,
    event_code: str | None = None,
    season: int | None = None,
    division: str | None = None,
    team: str | None = None,
    played: bool | None = None,
) -> GameConnection:
    first, offset = page_args(first, offset)
    clauses, params = ["1=1"], []
    if event_id:
        clauses.append("g.event_id=?")
        params.append(event_id)
    if source:
        clauses.append("EXISTS (SELECT 1 FROM source_entities sx WHERE "
                       "sx.entity_type='game' AND "
                       "sx.local_key=CAST(g.event_id AS TEXT)||'|'||g.game_key "
                       "AND sx.source=?)")
        params.append(source)
    if event_code:
        clauses.append("d.event_code=?")
        params.append(event_code)
    if season is not None:
        clauses.append("e.season=?")
        params.append(season)
    if division:
        clauses.append("e.division=?")
        params.append(division)
    if team:
        clauses.append("(lower(ht.display_name) LIKE ? OR lower(at.display_name) LIKE ?)")
        params.extend((f"%{team.lower()}%", f"%{team.lower()}%"))
    if played is not None:
        marks = ",".join("?" for _ in PLAYED_STATES)
        clauses.append(f"g.status {'IN' if played else 'NOT IN'} ({marks})")
        params.extend(PLAYED_STATES)
    base = """ FROM games g JOIN events e USING(event_id)
               JOIN euf_event_details d USING(event_id)
               LEFT JOIN event_teams ht ON ht.event_team_id=g.home_id
               LEFT JOIN event_teams at ON at.event_team_id=g.away_id
               WHERE """ + " AND ".join(clauses)
    with closing(connect_readonly(path)) as con:
        total = con.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
        rows = con.execute(
            """SELECT g.*,ht.canonical_team_id home_team_id,ht.display_name home_team,
                      at.canonical_team_id away_team_id,at.display_name away_team""" +
            base + " ORDER BY g.date,g.time,g.game_key LIMIT ? OFFSET ?",
            [*params, first, offset],
        ).fetchall()
        nodes = _game_nodes(con, rows)
    return GameConnection(
        nodes=nodes, total_count=total,
        page_info=PageInfo(first=first, offset=offset,
                           has_next_page=offset + first < total),
    )

def load_event_games_batch(
    path: str, keys: list[tuple[str, int, int, bool | None]]
) -> list[GameConnection]:
    for _, first, offset, _ in keys:
        page_args(first, offset)
    event_ids = sorted({key[0] for key in keys})
    marks = ",".join("?" for _ in event_ids)
    with closing(connect_readonly(path)) as con:
        rows = con.execute(
            f"""SELECT g.*,ht.canonical_team_id home_team_id,
                       ht.display_name home_team,
                       at.canonical_team_id away_team_id,
                       at.display_name away_team
                FROM games g
                LEFT JOIN event_teams ht ON ht.event_team_id=g.home_id
                LEFT JOIN event_teams at ON at.event_team_id=g.away_id
                WHERE g.event_id IN ({marks})
                ORDER BY g.event_id,g.date,g.time,g.game_key""",
            event_ids,
        ).fetchall()
        nodes = _game_nodes(con, rows)
    grouped: dict[str, list[Game]] = {event_id: [] for event_id in event_ids}
    for node in nodes:
        grouped[str(node.event_id)].append(node)
    connections = []
    for event_id, first, offset, played in keys:
        matching = grouped[event_id]
        if played is not None:
            matching = [game for game in matching if game.played is played]
        total = len(matching)
        connections.append(GameConnection(
            nodes=matching[offset:offset + first],
            total_count=total,
            page_info=PageInfo(
                first=first, offset=offset,
                has_next_page=offset + first < total,
            ),
        ))
    return connections


def event_game_loader(path: str) -> DataLoader:
    async def load(keys: list[tuple[str, int, int, bool | None]]):
        return load_event_games_batch(path, keys)

    return DataLoader(load_fn=load)


def _team(con: sqlite3.Connection, row: sqlite3.Row, path: str) -> Team:
    key = row["team_id"]
    return Team(
        id=strawberry.ID(key), name=row["name"], country=row["country"],
        sources=_sources(con, "team", key), _db=path,
    )


def load_teams(
    path: str,
    *,
    first: int = DEFAULT_PAGE,
    offset: int = 0,
    source: str | None = None,
    event_code: str | None = None,
    season: int | None = None,
    division: str | None = None,
    name: str | None = None,
) -> TeamConnection:
    first, offset = page_args(first, offset)
    clauses, params = ["1=1"], []
    if source:
        clauses.append("EXISTS (SELECT 1 FROM source_entities sx WHERE "
                       "sx.entity_type='team' AND sx.local_key=c.team_id AND sx.source=?)")
        params.append(source)
    if name:
        clauses.append("lower(c.name) LIKE ?")
        params.append(f"%{name.lower()}%")
    if event_code is not None:
        clauses.append(
            """EXISTS (
                 SELECT 1 FROM event_teams ft
                 JOIN events e USING(event_id)
                 JOIN euf_event_details d USING(event_id)
                 WHERE ft.canonical_team_id=c.team_id AND d.event_code=?
               )"""
        )
        params.append(event_code)
    for value, event_clause, ranking_clause in (
        (season, "e.season=?", "r.season=?"),
        (division, "e.division=?", "r.division=?"),
    ):
        if value is not None:
            clauses.append(
                """(EXISTS (
                     SELECT 1 FROM event_teams ft
                     JOIN events e USING(event_id)
                     WHERE ft.canonical_team_id=c.team_id AND """
                + event_clause
                + """) OR EXISTS (
                     SELECT 1 FROM ranking_roster_observations r
                     WHERE r.team_id=c.team_id AND """
                + ranking_clause
                + "))"
            )
            params.extend((value, value))
    where = " AND ".join(clauses)
    with closing(connect_readonly(path)) as con:
        total = con.execute(
            "SELECT COUNT(*) FROM canonical_teams c WHERE " + where, params
        ).fetchone()[0]
        rows = con.execute(
            "SELECT c.* FROM canonical_teams c WHERE " + where +
            " ORDER BY c.name,c.team_id LIMIT ? OFFSET ?",
            [*params, first, offset],
        ).fetchall()
        nodes = [_team(con, row, path) for row in rows]
    return TeamConnection(
        nodes=nodes, total_count=total,
        page_info=PageInfo(first=first, offset=offset,
                           has_next_page=offset + first < total),
    )


@strawberry.type
class Query:
    @strawberry.field
    def events(
        self,
        first: int = DEFAULT_PAGE,
        offset: int = 0,
        source: str | None = None,
        event_code: str | None = None,
        season: int | None = None,
        division: str | None = None,
        team: str | None = None,
        played: bool | None = None,
    ) -> EventConnection:
        return load_events(
            str(db_path()), first=first, offset=offset, source=source,
            event_code=event_code, season=season, division=division,
            team=team, played=played,
        )

    @strawberry.field
    def event(self, id: strawberry.ID) -> Event | None:
        path = str(db_path())
        with closing(connect_readonly(path)) as con:
            row = con.execute(
                """SELECT e.event_id,e.name,e.season,e.division,e.start_date,e.end_date,
                          d.event_code,d.roster_state
                   FROM events e JOIN euf_event_details d USING(event_id)
                   WHERE e.event_id=?""", (str(id),)
            ).fetchone()
            return _event(con, row, path) if row else None

    @strawberry.field
    def teams(
        self,
        first: int = DEFAULT_PAGE,
        offset: int = 0,
        source: str | None = None,
        event_code: str | None = None,
        season: int | None = None,
        division: str | None = None,
        name: str | None = None,
    ) -> TeamConnection:
        return load_teams(
            str(db_path()), first=first, offset=offset, source=source,
            event_code=event_code, season=season, division=division, name=name,
        )

    @strawberry.field
    def team(self, id: strawberry.ID) -> Team | None:
        path = str(db_path())
        with closing(connect_readonly(path)) as con:
            row = con.execute(
                "SELECT * FROM canonical_teams WHERE team_id=?", (str(id),)
            ).fetchone()
            return _team(con, row, path) if row else None

    @strawberry.field
    def games(
        self,
        first: int = DEFAULT_PAGE,
        offset: int = 0,
        source: str | None = None,
        event_code: str | None = None,
        season: int | None = None,
        division: str | None = None,
        team: str | None = None,
        played: bool | None = None,
    ) -> GameConnection:
        return load_games(
            str(db_path()), first=first, offset=offset, source=source,
            event_code=event_code, season=season, division=division,
            team=team, played=played,
        )


schema = strawberry.Schema(query=Query)
