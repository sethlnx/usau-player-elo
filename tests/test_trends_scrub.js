const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const site = fs.readFileSync(
  path.join(__dirname, '..', 'analysis', 'site.py'), 'utf8',
);
const start = site.indexOf('function trendCalendarYear(event) {');
const end = site.indexOf('function trendEventX(eventIndex) {', start);
assert(start >= 0 && end > start, 'trend helpers must remain in the site template');

const context = {
  curMode: 'elo',
  curMed: new Map(),
  trendEventPositions: new Map([
    [0, 0], [1, 1], [2, 2], [3, 3],
  ]),
  HEV: {
    0: ['2025-01-01', 'Opening event', 2025],
    1: ['2025-10-01', 'October event', 2026],
    2: ['2025-12-01', 'Year-end event', 2026],
    3: ['2026-01-01', 'Next-year event', 2026],
  },
  seriesVal: (series, index) => series.vals[index],
};
vm.runInNewContext(
  `${site.slice(start, end)}\n` +
    `this.trendCalendarYear = trendCalendarYear;\n` +
    `this.trendValueAtPosition = trendValueAtPosition;`,
  context,
);

const series = [
  {label: 'Player A', events: [0, 3], vals: [100, 200]},
  {label: 'Player B', events: [0, 3], vals: [200, 100]},
  {label: 'Player C', events: [0, 1], vals: [150, 150]},
];

function rankedNames(position) {
  const eventYear = context.trendCalendarYear(Math.round(position));
  return series.map((series, seriesIndex) => {
    const value = context.trendValueAtPosition(series, position, eventYear);
    return value === null ? null : {series, seriesIndex, value};
  }).filter(Boolean).sort((a, b) => b.value - a.value ||
    a.series.label.localeCompare(b.series.label) ||
    a.seriesIndex - b.seriesIndex).map(row => row.series.label);
}

assert.deepStrictEqual(rankedNames(1), [
  'Player B', 'Player C', 'Player A',
], 'ranking must follow interpolated line positions before crossover');
assert.deepStrictEqual(rankedNames(1.4), [
  'Player B', 'Player C', 'Player A',
], 'same-year rankings must retain a player after their final tournament');
assert.deepStrictEqual(rankedNames(1.6), [
  'Player A', 'Player C', 'Player B',
], 'ranking must swap at the crossover under the cursor');
assert.ok(
  Math.abs(context.trendValueAtPosition(
    series[0], 1, context.trendCalendarYear(1),
  ) - (100 + (100 / 3))) <
    1e-9,
  'scrub values must interpolate between plotted points',
);
assert.strictEqual(
  context.trendValueAtPosition(
    series[2], 2, context.trendCalendarYear(2),
  ), 150,
  'a final rating must carry through later tournaments in the same year',
);
assert.strictEqual(
  context.trendValueAtPosition(
    series[2], 3, context.trendCalendarYear(3),
  ), null,
  'a final rating must not carry into a later year',
);

console.log('trends scrub ranking contract tests passed');
