const assert = require('assert');
const {
  simulateTournament, stageForRound, resolveForecast, favoritePicks,
} = require('../analysis/tournament_sim');

const poolGames = [];
for (let home = 0; home < 8; home++) {
  for (let away = home + 1; away < 8; away++) {
    poolGames.push([home, away, null, null, 0]);
  }
}
const detail = {
  t: [0, 1, 2, 3, 4, 5, 6, 7],
  r: [1900, 1800, 1700, 1600, 1500, 1400, 1300, 1200],
  q: ['Championship final', 'Championship semifinals',
      'Championship quarterfinals', 'Prequarterfinals'],
  p: [[0, poolGames]],
  b: [['champ', 0, [[
    [0, 1, null, null, 0], [2, 3, null, null, 0],
    [4, 5, null, null, 0], [6, 7, null, null, 0],
  ], [
    [0, 2, null, null, 0], [4, 6, null, null, 0],
  ], [[0, 4, null, null, 0]]]]],
};
assert.strictEqual(stageForRound(detail, 2), 'quarterfinal');
assert.strictEqual(stageForRound(detail, 1), 'semifinal');
assert.strictEqual(stageForRound(detail, 0), 'final');
assert.strictEqual(stageForRound(detail, 3), null);

const first = simulateTournament(detail, {runs: 5000, seed: 17});
const second = simulateTournament(detail, {runs: 5000, seed: 17});
assert.deepStrictEqual(first, second, 'seeded simulations must be reproducible');
assert(Math.abs(first.pools[0].teams.reduce((sum, row) => sum + row.percent, 0) - 100) < 1e-9);

const forced = simulateTournament(detail, {
  runs: 1000, seed: 17, selected: {0: 7},
});
assert.strictEqual(forced.pools[0].teams.find(row => row.team === 7).percent, 100);
assert.throws(() => simulateTournament(detail, {selected: {0: 99}}), RangeError);

for (const row of first.teams) {
  assert(row.quarterfinal >= row.semifinal);
  assert(row.semifinal >= row.final);
  assert(row.final >= row.champion);
  for (const stage of ['quarterfinal', 'semifinal', 'final', 'champion']) {
    assert(row[stage] >= 0 && row[stage] <= 100);
  }
}

const manualDetail = {
  t: [0, 1, 2, 3],
  r: [1900, 1800, 1700, 1600],
  f: {
    p: [
      ['Pool A', [[0, 1, -1, null, null, 'a']]],
      ['Pool B', [[2, 3, -1, null, null, 'b']]],
    ],
    b: [9, 0, [[[
      [0, 0, 0], [0, 1, 0], -1, null, null, 'title',
    ]]]],
  },
};
const publishedDetail = JSON.stringify(manualDetail);
let manual = resolveForecast(manualDetail, {p: {a: 0, b: 2}, b: {}});
assert.strictEqual(manual.pools[0].complete, true);
assert.strictEqual(manual.bracket.rounds[0][0].home, 0);
assert.strictEqual(manual.bracket.rounds[0][0].away, 2);
manual = resolveForecast(manualDetail, {p: {a: 0, b: 2}, b: {'0:0': 2}});
assert.strictEqual(manual.bracket.champion, 2, 'a bracket pick must advance');
const favorites = favoritePicks(manualDetail);
assert.strictEqual(resolveForecast(manualDetail, favorites).bracket.champion, 0);
assert.strictEqual(JSON.stringify(manualDetail), publishedDetail,
  'interactive picks must not mutate published tournament detail');

console.log('tournament simulator contract tests passed');
