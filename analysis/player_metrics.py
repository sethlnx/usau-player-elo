"""Build the offline UFA-linked player metric pilot.

OVR is a presentation mapping of the existing player Elo. THR, POS, OFF, and
DEF are empirical-Bayes scorecards built from UFA season box scores. Attribute
scores are descriptive and deliberately do not feed the authoritative Elo.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import fmean, median, pstdev

from analysis.backtest import DB_PATH
from ufa.link import resolve_links

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAYERS = ROOT / "data" / "player_elo.csv"
DEFAULT_ROLES = ROOT / "data" / "player_roles.csv"
DEFAULT_REFERENCE = ROOT / "data" / "player_metric_reference.json"
DEFAULT_OUTPUT = ROOT / "data" / "player_metrics.csv"
OVR_DISPLAY_CENTER = 60.0
ATTRIBUTE_DISPLAY_CENTER = 70.0
DISPLAY_POINTS_PER_Z = 10.0
MODEL_VERSION = "ufa-eb-v4"
REFERENCE_SEASONS = (2022, 2023, 2024, 2025)
HISTORY_DECAY_PER_SEASON = 0.5
MIN_PRIOR_EXPOSURE = 25.0
MAX_HISTORY_PRIOR_MULTIPLIER = 4.0
ROLE_GROUPS = ("handler", "hybrid", "cutter")
MIN_REFERENCE_ROWS = 20

STAT_FIELDS = (
    "assists", "goals", "hockeyassists", "completions", "throwattempts",
    "throwaways", "stalls", "drops", "blocks", "catches", "callahans",
    "opointsplayed", "opointsscored", "dpointsplayed", "dpointsscored",
    "secondsplayed", "oopportunities", "oopportunityscores",
    "dopportunities", "dopportunitystops", "yardsthrown", "yardsreceived",
    "hucksattempted", "huckscompleted",
)


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    attribute: str
    numerator: tuple[tuple[str, float], ...]
    denominator: tuple[tuple[str, float], ...]
    family: str
    direction: float = 1.0


COMPONENTS = (
    # THR measures field-stretching and chance creation. Possession safety is
    # separate below, so aggressive high-value throwers are not graded mainly
    # on the completion profile of their harder attempts.
    ComponentSpec("huck_rate", "thr", (("hucksattempted", 1.0),),
                  (("throwattempts", 1.0),), "binomial"),
    ComponentSpec("huck_accuracy", "thr", (("huckscompleted", 1.0),),
                  (("hucksattempted", 1.0),), "binomial"),
    ComponentSpec("throwing_yards", "thr", (("yardsthrown", 1.0),),
                  (("throwattempts", 1.0),), "count"),
    ComponentSpec("creation", "thr",
                  (("assists", 1.0), ("hockeyassists", 1.0)),
                  (("throwattempts", 1.0),), "count"),
    # POS measures possession security with the disc and as a receiver.
    ComponentSpec("completion", "pos", (("completions", 1.0),),
                  (("throwattempts", 1.0),), "binomial"),
    ComponentSpec("throw_security", "pos",
                  (("throwattempts", 1.0), ("throwaways", -1.0),
                   ("stalls", -1.0)),
                  (("throwattempts", 1.0),), "binomial"),
    ComponentSpec("catch_security", "pos", (("catches", 1.0),),
                  (("catches", 1.0), ("drops", 1.0)), "binomial"),
    ComponentSpec("offensive_actions", "off",
                  (("goals", 1.0), ("assists", 1.0),
                   ("hockeyassists", 1.0)),
                  (("opointsplayed", 1.0), ("dpointsplayed", 1.0)), "count"),
    ComponentSpec("o_conversion", "off", (("oopportunityscores", 1.0),),
                  (("oopportunities", 1.0),), "binomial"),
    ComponentSpec("receiving_yards", "off", (("yardsreceived", 1.0),),
                  (("opointsplayed", 1.0), ("dpointsplayed", 1.0)), "count"),
    ComponentSpec("o_point_success", "off", (("opointsscored", 1.0),),
                  (("opointsplayed", 1.0),), "binomial"),
    ComponentSpec("blocks", "def",
                  (("blocks", 1.0), ("callahans", 1.0)),
                  (("dpointsplayed", 1.0),), "count"),
    ComponentSpec("d_stops", "def", (("dopportunitystops", 1.0),),
                  (("dopportunities", 1.0),), "binomial"),
    ComponentSpec("d_point_success", "def", (("dpointsscored", 1.0),),
                  (("dpointsplayed", 1.0),), "binomial"),
)
MIN_COMPONENTS = {"thr": 2, "pos": 2, "off": 2, "def": 2}

OUTPUT_FIELDS = (
    "rank", "player", "player_id", "ufa_player_ids", "season", "as_of_date",
    "stats_through", "model_version", "reference_fitted_at", "ovr", "elo", "elo_sigma",
    "elo_lo90", "elo_hi90", "thr", "thr_z", "thr_reliability", "pos",
    "pos_z", "pos_reliability", "off", "off_z", "off_reliability", "def",
    "def_z", "def_reliability", "role", "role_confidence", "role_source",
    "history_seasons", "weighted_prior_throw_attempts", "goals", "assists",
    "blocks",
    "turnovers", "completions", "throw_attempts", "huck_attempts",
    "huck_completions", "throwing_yards", "receiving_yards", "o_points",
    "d_points", "seconds_played", "stat_source", "coverage_flags",
)


def _pid(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _linear(row: dict, terms: tuple[tuple[str, float], ...]) -> float:
    return sum(_number(row.get(field)) * coefficient for field, coefficient in terms)


def _observation(spec: ComponentSpec, row: dict) -> tuple[float, float] | None:
    exposure = _linear(row, spec.denominator)
    if exposure <= 0:
        return None
    value = _linear(row, spec.numerator)
    if spec.family == "binomial":
        value = min(max(value, 0.0), exposure)
    elif value < 0:
        return None
    return value, exposure


def _display_score(z: float, center: float) -> int:
    """Map model z-scores onto a stable game-style 1–99 presentation scale."""
    return min(99, max(1, round(center + DISPLAY_POINTS_PER_Z * z)))


def _attribute_display_score(z: float) -> int:
    return _display_score(z, ATTRIBUTE_DISPLAY_CENTER)


def _ovr_score(z: float) -> int:
    return _display_score(z, OVR_DISPLAY_CENTER)


def _robust_location_scale(values: list[float]) -> tuple[float, float]:
    if not values:
        raise ValueError("cannot fit an empty OVR reference cohort")
    center = median(values)
    mad = median([abs(value - center) for value in values])
    scale = 1.4826 * mad
    if scale <= 1e-9:
        scale = pstdev(values)
    return center, max(scale, 1.0)


def _fit_component(samples: list[tuple[float, float]], family: str) -> dict:
    total_exposure = sum(exposure for _, exposure in samples)
    mean = sum(value for value, _ in samples) / total_exposure
    rates = [value / exposure for value, exposure in samples]
    observed_variance = fmean((rate - mean) ** 2 for rate in rates)
    if family == "binomial":
        sampling_variance = fmean(
            mean * (1.0 - mean) / exposure for _, exposure in samples
        )
        latent_variance = max(observed_variance - sampling_variance, 1e-8)
        prior_exposure = mean * (1.0 - mean) / latent_variance - 1.0
    else:
        sampling_variance = fmean(max(mean, 1e-9) / exposure for _, exposure in samples)
        latent_variance = max(observed_variance - sampling_variance, 1e-8)
        prior_exposure = max(mean, 1e-9) / latent_variance
    prior_exposure = min(max(prior_exposure, MIN_PRIOR_EXPOSURE), 1000.0)
    posteriors = [
        (value + prior_exposure * mean) / (exposure + prior_exposure)
        for value, exposure in samples
    ]
    spread = pstdev(posteriors)
    if spread <= 1e-9:
        spread = math.sqrt(latent_variance)
    return {
        "mean": mean,
        "prior_exposure": prior_exposure,
        "spread": max(spread, 1e-6),
        "sample_size": len(samples),
    }


def load_player_rows(path: Path = DEFAULT_PLAYERS) -> dict:
    with path.open(newline="") as handle:
        return {_pid(row["player_id"]): row for row in csv.DictReader(handle)}


def load_role_rows(path: Path = DEFAULT_ROLES) -> dict:
    roles = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            roles[(_pid(row["player_id"]), int(row["season"]))] = row
    return roles


def load_ufa_season_rows(con, links: dict[str, int]) -> list[dict]:
    """Load season totals with a conservative data-availability boundary.

    UFA season rows have no fetch timestamp. Their availability falls back to
    the latest dated Final in that season; if game dates are absent, September
    1 is the same conservative season-total boundary used by the Elo replay.
    A cutoff before that boundary excludes the whole cumulative row rather than
    leaking later games into a partial-season snapshot.
    """
    season_ends = dict(con.execute("""
        SELECT year, MAX(date)
        FROM ufa_games
        WHERE status = 'Final' AND date IS NOT NULL
        GROUP BY year
    """))
    columns = ", ".join(STAT_FIELDS)
    rows = []
    for raw in con.execute(
        f"SELECT player_id, year, {columns} FROM ufa_player_stats ORDER BY year, player_id"
    ):
        season = int(raw[1])
        row = {
            "ufa_player_id": raw[0], "season": season,
            "stats_through": season_ends.get(season) or f"{season}-09-01",
        }
        row.update(zip(STAT_FIELDS, raw[2:]))
        row["player_id"] = links.get(raw[0])
        rows.append(row)
    return rows


def _role_for(roles: dict, player_id, season: int) -> dict:
    exact = roles.get((player_id, season))
    if exact is not None:
        return exact
    previous = [
        row for (pid, year), row in roles.items()
        if pid == player_id and year < season
    ]
    if not previous:
        return {"role": "unknown", "confidence": "0", "source": "none"}
    return max(previous, key=lambda row: int(row["season"]))


def _aggregate_linked(
    rows: list[dict], season: int, as_of_date: str,
) -> list[dict]:
    grouped = {}
    for row in rows:
        pid = row.get("player_id")
        if (
            pid is None or row["season"] != season
            or row.get("stats_through", f"{row['season']}-09-01") > as_of_date
        ):
            continue
        aggregate = grouped.setdefault(pid, {
            "player_id": pid, "season": season, "ufa_player_ids": set(),
            "stats_through": "",
            **{field: 0.0 for field in STAT_FIELDS},
        })
        aggregate["ufa_player_ids"].add(row["ufa_player_id"])
        aggregate["stats_through"] = max(
            aggregate["stats_through"], row.get("stats_through", ""),
        )
        for field in STAT_FIELDS:
            aggregate[field] += _number(row.get(field))
    return list(grouped.values())


def fit_reference(
    rows: list[dict], players: dict, roles: dict,
    seasons: tuple[int, ...] = REFERENCE_SEASONS,
    fitted_at: str | None = None,
) -> dict:
    """Fit and freeze score normalization from available completed UFA seasons."""
    cutoff = fitted_at or date.today().isoformat()
    reference_rows = [
        row for row in rows
        if row["season"] in seasons
        and row.get("stats_through", f"{row['season']}-09-01") <= cutoff
    ]
    groups = {"all": reference_rows}
    for role in ROLE_GROUPS:
        groups[role] = [
            row for row in reference_rows
            if row.get("player_id") is not None
            and _role_for(roles, row["player_id"], row["season"]).get("role") == role
        ]

    components = {}
    for group, group_rows in groups.items():
        fitted = {}
        for spec in COMPONENTS:
            samples = [
                observation for row in group_rows
                if (observation := _observation(spec, row)) is not None
            ]
            if len(samples) >= MIN_REFERENCE_ROWS:
                fitted[spec.name] = _fit_component(samples, spec.family)
        if group == "all" or fitted:
            components[group] = fitted

    reference_pids = {
        row["player_id"] for row in reference_rows if row.get("player_id") is not None
    }
    elo_values = [
        _number(players[pid].get("elo")) for pid in reference_pids if pid in players
    ]
    center, scale = _robust_location_scale(elo_values)
    return {
        "model_version": MODEL_VERSION,
        "fitted_at": fitted_at or date.today().isoformat(),
        "reference_seasons": list(seasons),
        "ovr": {"center": center, "scale": scale, "sample_size": len(elo_values)},
        "presentation": {
            "ovr_center": OVR_DISPLAY_CENTER,
            "attribute_center": ATTRIBUTE_DISPLAY_CENTER,
            "points_per_z": DISPLAY_POINTS_PER_Z,
        },
        "history": {
            "decay_per_season": HISTORY_DECAY_PER_SEASON,
            "minimum_prior_exposure": MIN_PRIOR_EXPOSURE,
            "maximum_history_prior_multiplier": MAX_HISTORY_PRIOR_MULTIPLIER,
        },
        "components": components,
    }


def write_reference(path: Path, reference: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reference, indent=2, sort_keys=True) + "\n")


def load_reference(path: Path) -> dict:
    reference = json.loads(path.read_text())
    if reference.get("model_version") != MODEL_VERSION:
        raise ValueError(
            f"reference model {reference.get('model_version')!r} does not match {MODEL_VERSION!r}"
        )
    return reference


def _component_reference(reference: dict, role: str, name: str) -> dict | None:
    role_components = reference["components"].get(role, {})
    return role_components.get(name) or reference["components"]["all"].get(name)


def _prior_rows_by_player(
    rows: list[dict], season: int, as_of_date: str,
) -> dict:
    """Aggregate available pre-season rows by linked player and season."""
    grouped = {}
    for row in rows:
        pid = row.get("player_id")
        row_season = row["season"]
        if (
            pid is None or row_season >= season
            or row.get("stats_through", f"{row_season}-09-01") > as_of_date
        ):
            continue
        key = (pid, row_season)
        aggregate = grouped.setdefault(key, {
            "player_id": pid, "season": row_season,
            **{field: 0.0 for field in STAT_FIELDS},
        })
        for field in STAT_FIELDS:
            aggregate[field] += _number(row.get(field))
    by_player = defaultdict(list)
    for (pid, _), row in grouped.items():
        by_player[pid].append(row)
    return by_player


def _decayed_observation(
    spec: ComponentSpec, history: list[dict], season: int,
) -> tuple[float, float] | None:
    value = exposure = 0.0
    for row in history:
        observation = _observation(spec, row)
        if observation is None:
            continue
        row_value, row_exposure = observation
        weight = HISTORY_DECAY_PER_SEASON ** (season - row["season"])
        value += weight * row_value
        exposure += weight * row_exposure
    return (value, exposure) if exposure > 0 else None

def _attribute_score(
    attribute: str, row: dict, role: str, reference: dict,
    history: list[dict], season: int,
):
    values = []
    for spec in COMPONENTS:
        if spec.attribute != attribute:
            continue
        observation = _observation(spec, row)
        fitted = _component_reference(reference, role, spec.name)
        if observation is None or fitted is None:
            continue
        value, exposure = observation
        prior_exposure = fitted["prior_exposure"]
        historical = _decayed_observation(spec, history, season)
        if historical is None:
            prior_mean = fitted["mean"]
            historical_exposure = 0.0
        else:
            historical_value, historical_exposure = historical
            maximum_history_exposure = (
                MAX_HISTORY_PRIOR_MULTIPLIER * prior_exposure
            )
            if historical_exposure > maximum_history_exposure:
                scale = maximum_history_exposure / historical_exposure
                historical_value *= scale
                historical_exposure = maximum_history_exposure
            prior_mean = (
                historical_value + prior_exposure * fitted["mean"]
            ) / (historical_exposure + prior_exposure)
        posterior = (
            value + prior_exposure * prior_mean
        ) / (exposure + prior_exposure)
        z = spec.direction * (posterior - fitted["mean"]) / fitted["spread"]
        evidence = exposure + historical_exposure
        reliability = evidence / (evidence + prior_exposure)
        values.append((min(max(z, -4.0), 4.0), reliability))
    if len(values) < MIN_COMPONENTS[attribute]:
        return None
    # Uncertain component estimates contribute only in proportion to their
    # evidence, pulling sparse player cards back toward the role average.
    z = fmean(value * component_reliability
              for value, component_reliability in values)
    reliability = fmean(value for _, value in values)
    return _attribute_display_score(z), z, reliability


def build_snapshot(
    rows: list[dict], players: dict, roles: dict, reference: dict,
    season: int, as_of_date: str,
) -> list[dict]:
    """Return one current-season metric row per linked, rated UFA player."""
    if reference.get("model_version") != MODEL_VERSION:
        raise ValueError("metric reference and generator versions differ")
    output = []
    prior_rows = _prior_rows_by_player(rows, season, as_of_date)
    for row in _aggregate_linked(rows, season, as_of_date):
        pid = row["player_id"]
        player = players.get(pid)
        if player is None:
            continue
        role_row = _role_for(roles, pid, season)
        role = role_row.get("role", "unknown")
        history = prior_rows.get(pid, [])
        scores = {
            attribute: _attribute_score(
                attribute, row, role, reference, history, season,
            )
            for attribute in ("thr", "pos", "off", "def")
        }
        elo = _number(player.get("elo"))
        ovr_z = (elo - reference["ovr"]["center"]) / reference["ovr"]["scale"]
        attributes = [name for name, value in scores.items() if value is not None]
        flags = ["linked-ufa", *[f"attribute:{name}" for name in attributes]]
        flags.append(
            f"history-prior:{len(history)}-seasons" if history
            else "role-average-prior"
        )
        flags.append("current-season-cumulative" if season == int(as_of_date[:4])
                     else "complete-season")
        metric = {
            "player": player.get("player", "?"), "player_id": pid,
            "ufa_player_ids": ";".join(sorted(row["ufa_player_ids"])),
            "season": season, "as_of_date": as_of_date,
            "stats_through": row["stats_through"], "model_version": MODEL_VERSION,
            "reference_fitted_at": reference["fitted_at"],
            "ovr": _ovr_score(ovr_z), "elo": round(elo, 1),
            "elo_sigma": player.get("sigma", ""),
            "elo_lo90": player.get("lo90", ""), "elo_hi90": player.get("hi90", ""),
            "role": role, "role_confidence": role_row.get("confidence", "0"),
            "role_source": role_row.get("source", "none"),
            "history_seasons": len(history),
            "weighted_prior_throw_attempts": round(sum(
                _number(prior.get("throwattempts"))
                * HISTORY_DECAY_PER_SEASON ** (season - prior["season"])
                for prior in history
            ), 1),
            "goals": round(row["goals"]), "assists": round(row["assists"]),
            "blocks": round(row["blocks"]),
            "turnovers": round(row["throwaways"] + row["stalls"] + row["drops"]),
            "completions": round(row["completions"]),
            "throw_attempts": round(row["throwattempts"]),
            "huck_attempts": round(row["hucksattempted"]),
            "huck_completions": round(row["huckscompleted"]),
            "throwing_yards": round(row["yardsthrown"]),
            "receiving_yards": round(row["yardsreceived"]),
            "o_points": round(row["opointsplayed"]),
            "d_points": round(row["dpointsplayed"]),
            "seconds_played": round(row["secondsplayed"]),
            "stat_source": "ufa-season", "coverage_flags": ";".join(flags),
        }
        for attribute, result in scores.items():
            if result is None:
                metric[attribute] = metric[f"{attribute}_z"] = ""
                metric[f"{attribute}_reliability"] = ""
            else:
                score, z, reliability = result
                metric[attribute] = score
                metric[f"{attribute}_z"] = round(z, 4)
                metric[f"{attribute}_reliability"] = round(reliability, 4)
        output.append(metric)
    output.sort(key=lambda row: (-row["ovr"], -row["elo"], row["player"], row["player_id"]))
    for rank, row in enumerate(output, 1):
        row["rank"] = rank
    return output


def write_snapshot(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--players", type=Path, default=DEFAULT_PLAYERS)
    parser.add_argument("--roles", type=Path, default=DEFAULT_ROLES)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--season", type=int)
    parser.add_argument(
        "--as-of", default=date.today().isoformat(),
        help="snapshot date (YYYY-MM-DD); defaults to today",
    )
    parser.add_argument("--refit-reference", action="store_true")
    args = parser.parse_args()
    try:
        as_of = date.fromisoformat(args.as_of).isoformat()
    except ValueError as error:
        parser.error(f"--as-of must be YYYY-MM-DD: {error}")

    players = load_player_rows(args.players)
    roles = load_role_rows(args.roles)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        links = resolve_links(con)
        stats = load_ufa_season_rows(con, links)
    finally:
        con.close()
    if not stats:
        raise SystemExit("no UFA season statistics found")
    available_seasons = {
        row["season"] for row in stats
        if row.get("stats_through", f"{row['season']}-09-01") <= as_of
    }
    if not available_seasons:
        raise SystemExit(f"no UFA season statistics available by {as_of}")
    season = args.season or max(available_seasons)
    if season not in available_seasons:
        raise SystemExit(f"season {season} statistics are not available by {as_of}")
    if args.refit_reference:
        reference = fit_reference(stats, players, roles, fitted_at=as_of)
        write_reference(args.reference, reference)
        print(f"wrote {args.reference} ({reference['ovr']['sample_size']:,} OVR references)")
    elif args.reference.exists():
        reference = load_reference(args.reference)
        if reference["fitted_at"] > as_of:
            raise SystemExit(
                f"reference fitted at {reference['fitted_at']} is after {as_of}"
            )
    else:
        raise SystemExit(f"missing {args.reference}; run once with --refit-reference")

    snapshot = build_snapshot(stats, players, roles, reference, season, as_of)
    write_snapshot(args.output, snapshot)
    print(f"wrote {args.output} ({len(snapshot):,} linked UFA players, season {season})")


if __name__ == "__main__":
    main()
