const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const site = fs.readFileSync(
  path.join(__dirname, '..', 'analysis', 'site.py'), 'utf8',
);
const start = site.indexOf('function trendValueAtPosition(series, position) {');
const end = site.indexOf('function trendEventX(eventIndex) {', start);
assert(start >= 0 && end > start, 'trend helpers must remain in the site template');

const context = {
  curMode: 'elo',
  curMed: new Map(),
  trendEventPositions: new Map([
    [0, 0], [1, 1], [2, 2], [3, 3],
  ]),
  seriesVal: (series, index) => series.vals[index],
};
vm.runInNewContext(
  `${site.slice(start, end)}\nthis.trendValueAtPosition = trendValueAtPosition;`,
  context,
);

const series = [
  {label: 'Player A', events: [0, 3], vals: [100, 200]},
  {label: 'Player B', events: [0, 3], vals: [200, 100]},
  {label: 'Player C', events: [0, 1], vals: [150, 150]},
];

function rankedNames(position) {
  return series.map((series, seriesIndex) => {
    const value = context.trendValueAtPosition(series, position);
    return value === null ? null : {series, seriesIndex, value};
  }).filter(Boolean).sort((a, b) => b.value - a.value ||
    a.series.label.localeCompare(b.series.label) ||
    a.seriesIndex - b.seriesIndex).map(row => row.series.label);
}

assert.deepStrictEqual(rankedNames(1), [
  'Player B', 'Player C', 'Player A',
], 'ranking must follow interpolated line positions before crossover');
assert.deepStrictEqual(rankedNames(1.4), [
  'Player B', 'Player A',
], 'ranking must follow the cursor position before crossover');
assert.deepStrictEqual(rankedNames(1.6), [
  'Player A', 'Player B',
], 'ranking must swap at the crossover under the cursor');
assert.ok(
  Math.abs(context.trendValueAtPosition(series[0], 1) - (100 + (100 / 3))) <
    1e-9,
  'scrub values must interpolate between plotted points',
);
assert.strictEqual(
  context.trendValueAtPosition(series[2], 2), null,
  'a series with no later point must not remain current',
);

console.log('trends scrub ranking contract tests passed');
