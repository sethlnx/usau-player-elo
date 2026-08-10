(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TournamentSim = api;
}(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';

  const MILESTONES = ['quarterfinal', 'semifinal', 'final', 'champion'];

  function seededRandom(seed) {
    let state = seed >>> 0;
    return function () {
      state = (state + 0x6D2B79F5) | 0;
      let value = Math.imul(state ^ state >>> 15, 1 | state);
      value ^= value + Math.imul(value ^ value >>> 7, 61 | value);
      return ((value ^ value >>> 14) >>> 0) / 4294967296;
    };
  }

  function winProbability(a, b, scale) {
    return 1 / (1 + 10 ** ((b - a) / scale));
  }

  function actualStandings(games) {
    const ids = [];
    const seen = new Set();
    for (const game of games) {
      for (const team of [game[0], game[1]]) {
        if (!seen.has(team)) {
          seen.add(team);
          ids.push(team);
        }
      }
    }
    const records = new Map(ids.map((team, order) =>
      [team, {team, wins: 0, differential: 0, order}]));
    for (const game of games) {
      if (!Number.isFinite(game[2]) || !Number.isFinite(game[3])) continue;
      const home = records.get(game[0]);
      const away = records.get(game[1]);
      home.differential += game[2] - game[3];
      away.differential += game[3] - game[2];
      if (game[2] > game[3]) home.wins++;
      else if (game[3] > game[2]) away.wins++;
    }
    return [...records.values()]
      .sort((a, b) => b.wins - a.wins || b.differential - a.differential ||
                      a.order - b.order)
      .map(record => record.team);
  }

  function actualWinner(game) {
    return game[0];
  }

  function prepare(detail) {
    if (!detail || !Array.isArray(detail.t) || !Array.isArray(detail.p) ||
        !Array.isArray(detail.b)) {
      throw new TypeError('Tournament detail must include t, p, and b arrays');
    }
    const pools = [];
    detail.p.forEach(([later, games], sourceIndex) => {
      if (later || !games.length) return;
      const actualOrder = actualStandings(games);
      pools.push({sourceIndex, games, teams: actualOrder.slice(), actualOrder});
    });
    const bracket = detail.b.find(([kind, rootRank, rounds]) =>
      kind === 'champ' && rootRank === 0 && rounds.some(round => round.some(Boolean)));
    return {pools, bracket};
  }

  function normalizedRatings(detail) {
    const source = Array.isArray(detail.r) ? detail.r : [];
    const known = source.filter(Number.isFinite).slice().sort((a, b) => a - b);
    const fallback = known.length
      ? known[Math.floor((known.length - 1) / 2)]
      : 1500;
    return detail.t.map((_, index) => Number.isFinite(source[index])
      ? source[index]
      : fallback);
  }
  function stageForRound(detail, rank) {
    const label = String(Array.isArray(detail.q) ? detail.q[rank] || '' : '')
      .toLowerCase().replace(/[^a-z]+/g, ' ').trim();
    if (label && /\bquarterfinals?\b/.test(label) && !/\bpre\b/.test(label)) {
      return 'quarterfinal';
    }
    if (label && /\bsemifinals?\b/.test(label)) return 'semifinal';
    if (label && /\bfinal\b/.test(label) && !/\bsemi\b/.test(label)) return 'final';
    // Older sidecars did not carry round labels. Their rank ordering is
    // canonical, so retain a compatibility fallback while new payloads use q.
    return rank === 2 ? 'quarterfinal' : rank === 1 ? 'semifinal'
      : rank === 0 ? 'final' : null;
  }


  function simulateTournament(detail, options) {
    options = options || {};
    const runs = options.runs === undefined ? 5000 : options.runs;
    const scale = options.scale === undefined ? (detail.s || 260) : options.scale;
    const seed = options.seed === undefined ? 1 : options.seed;
    const selected = options.selected || {};
    if (!Number.isInteger(runs) || runs < 1 || runs > 100000) {
      throw new RangeError('runs must be an integer from 1 to 100000');
    }
    if (!Number.isFinite(scale) || scale <= 0) {
      throw new RangeError('scale must be positive');
    }

    const {pools, bracket} = prepare(detail);
    const ratings = normalizedRatings(detail);
    const random = seededRandom(seed);
    const poolCounts = pools.map(pool => new Map(pool.teams.map(team => [team, 0])));
    const milestoneCounts = Object.fromEntries(
      MILESTONES.map(stage => [stage, new Uint32Array(detail.t.length)]));
    const stages = {quarterfinal: false, semifinal: false, final: false, champion: false};

    for (const pool of pools) {
      const forced = selected[pool.sourceIndex];
      if (forced !== undefined && !pool.teams.includes(Number(forced))) {
        throw new RangeError(`selected winner is not in pool ${pool.sourceIndex}`);
      }
    }

    for (let run = 0; run < runs; run++) {
      const seedOccupant = new Map();
      for (let pi = 0; pi < pools.length; pi++) {
        const pool = pools[pi];
        const records = new Map(pool.teams.map(team =>
          [team, {team, wins: 0, tie: random()}]));
        for (const game of pool.games) {
          const home = game[0];
          const away = game[1];
          const winner = random() < winProbability(ratings[home], ratings[away], scale)
            ? home : away;
          records.get(winner).wins++;
        }
        const order = [...records.values()]
          .sort((a, b) => b.wins - a.wins || a.tie - b.tie)
          .map(record => record.team);
        const forced = selected[pool.sourceIndex];
        if (forced !== undefined) {
          const team = Number(forced);
          order.splice(order.indexOf(team), 1);
          order.unshift(team);
        }
        poolCounts[pi].set(order[0], poolCounts[pi].get(order[0]) + 1);
        for (let rank = 0; rank < pool.actualOrder.length; rank++) {
          seedOccupant.set(pool.actualOrder[rank], order[rank]);
        }
      }

      if (!bracket) continue;
      const [, rootRank, rounds] = bracket;
      const feederWinner = new Map();
      let champion = null;
      for (let roundIndex = 0; roundIndex < rounds.length; roundIndex++) {
        const rank = rootRank + rounds.length - 1 - roundIndex;
        const stage = stageForRound(detail, rank);
        if (stage) stages[stage] = true;
        for (const game of rounds[roundIndex]) {
          if (!game) continue;
          const resolve = actual => feederWinner.has(actual)
            ? feederWinner.get(actual)
            : seedOccupant.has(actual) ? seedOccupant.get(actual) : actual;
          const home = resolve(game[0]);
          const away = resolve(game[1]);
          if (stage) {
            milestoneCounts[stage][home]++;
            milestoneCounts[stage][away]++;
          }
          const winner = random() < winProbability(ratings[home], ratings[away], scale)
            ? home : away;
          feederWinner.set(actualWinner(game), winner);
          if (stage === 'final') champion = winner;
        }
      }
      if (champion !== null) {
        stages.champion = true;
        milestoneCounts.champion[champion]++;
      }
    }

    const percent = count => count * 100 / runs;
    return {
      runs,
      stages,
      pools: pools.map((pool, index) => ({
        sourceIndex: pool.sourceIndex,
        teams: pool.teams.map(team => ({team, percent: percent(poolCounts[index].get(team))})),
      })),
      teams: detail.t.map((_, team) => ({
        team,
        rating: Number.isFinite(detail.r && detail.r[team]) ? detail.r[team] : null,
        quarterfinal: percent(milestoneCounts.quarterfinal[team]),
        semifinal: percent(milestoneCounts.semifinal[team]),
        final: percent(milestoneCounts.final[team]),
        champion: percent(milestoneCounts.champion[team]),
      })),
    };
  }


  return {seededRandom, winProbability, stageForRound, simulateTournament};
}));
