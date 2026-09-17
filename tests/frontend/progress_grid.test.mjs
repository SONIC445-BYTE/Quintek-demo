/**
 * `activityGrid`, `masteryPercent` and `studyStreak`.
 *
 * These three are the whole of the progress screen's arithmetic, pulled out of
 * the component so they can be tested against inputs rather than looked at.
 * Each one replaced something that was either a constant or a plausible-
 * looking guess, and each one has a specific wrong implementation that a
 * careless test would not distinguish from the right one:
 *
 *   activityGrid    a positional `.map()` over `activity`
 *   masteryPercent  `correct / (correct + wrong) || 0`
 *   studyStreak     counting rows instead of consecutive days
 *
 * Every test below is written so that the wrong implementation fails it. That
 * is the point: a passing test proves nothing until it has been shown to fail
 * with the bug present.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

/* No `window`, so the module loads unconfigured. These three functions are
 * pure and do not touch the network, which is exactly why they are testable
 * here at all. */
const { activityGrid, masteryPercent, studyStreak } =
  await import('../../frontend/quintek-student-api.js?grid=' + Date.now());

const TODAY = new Date(Date.UTC(2026, 8, 17));   // 2026-09-17
const ago = (n) => new Date(Date.UTC(2026, 8, 17) - n * 86400000)
  .toISOString().slice(0, 10);

test('activityGrid returns a fixed-length grid, oldest first, ending today', () => {
  const grid = activityGrid([], { today: TODAY });
  assert.equal(grid.length, 84);
  assert.equal(grid[83].date, '2026-09-17', 'the last cell must be today');
  assert.equal(grid[0].date, ago(83), 'the first cell must be 83 days back');
  assert.ok(grid.every((c) => c.n === 0), 'an empty history is 84 zero cells');
});

test('activityGrid places each day at its own date, not at its position', () => {
  /* THE BUG THIS EXISTS FOR.
   *
   * `activity` is sparse and newest-first. A positional implementation --
   * `activity.map((row, i) => ...)` -- would put 9 in cell 0 and 4 in cell 1,
   * at the OLD end of the grid, and would report only two non-zero cells
   * adjacent to each other. Every assertion below fails for it. */
  const activity = [
    { d: ago(0), n: 9 },
    { d: ago(40), n: 4 },
  ];
  const grid = activityGrid(activity, { today: TODAY });

  assert.equal(grid[83].n, 9, "today's count belongs in the last cell");
  assert.equal(grid[83 - 40].n, 4, 'a 40-day-old count belongs 40 cells back');
  assert.equal(grid[0].n, 0, 'the oldest cell had no attempts and must read zero');
  assert.equal(grid[1].n, 0);

  const filled = grid.map((c, i) => (c.n ? i : -1)).filter((i) => i >= 0);
  assert.deepEqual(filled, [43, 83],
    'the two study days must sit 40 cells apart, not adjacent');
});

test('activityGrid drops days older than the window rather than wrapping them', () => {
  const grid = activityGrid([{ d: ago(200), n: 7 }, { d: ago(3), n: 2 }],
                            { today: TODAY });
  assert.equal(grid.reduce((t, c) => t + c.n, 0), 2,
    'a day outside the twelve weeks must not be folded into a cell inside it');
  assert.equal(grid[80].n, 2);
});

test('activityGrid ignores a future date instead of reading past the end', () => {
  const future = new Date(Date.UTC(2026, 8, 20)).toISOString().slice(0, 10);
  const grid = activityGrid([{ d: future, n: 5 }], { today: TODAY });
  assert.equal(grid.length, 84);
  assert.equal(grid.reduce((t, c) => t + c.n, 0), 0);
});

test('activityGrid sums a repeated day rather than overwriting it', () => {
  const grid = activityGrid([{ d: ago(2), n: 3 }, { d: ago(2), n: 4 }],
                            { today: TODAY });
  assert.equal(grid[81].n, 7, 'two rows for one day must add up, not replace');
});

test('activityGrid survives a malformed payload', () => {
  for (const bad of [null, undefined, 'nonsense', [null], [{}], [{ d: null, n: 1 }]]) {
    const grid = activityGrid(bad, { today: TODAY });
    assert.equal(grid.length, 84, `a grid is still 84 cells for ${JSON.stringify(bad)}`);
    assert.ok(grid.every((c) => typeof c.n === 'number' && !Number.isNaN(c.n)));
  }
});

test('activityGrid is computed in UTC, not in the viewer local time', () => {
  /* Late-evening UTC is already tomorrow east of the line and still today
   * west of it. The grid must not move with the viewer: the server sliced
   * `created_at` in UTC, so a local-time grid would file today's attempts
   * under a date that does not exist yet for that learner. */
  const lateUtc = new Date(Date.UTC(2026, 8, 17, 23, 30));
  const earlyUtc = new Date(Date.UTC(2026, 8, 17, 0, 30));
  assert.equal(activityGrid([], { today: lateUtc })[83].date, '2026-09-17');
  assert.equal(activityGrid([], { today: earlyUtc })[83].date, '2026-09-17');
});

test('masteryPercent is null when nothing has been attempted', () => {
  /* NOT zero. `correct / (correct + wrong)` is NaN here and the usual
   * `|| 0` guard turns it into 0%, which tells a learner they get a concept
   * wrong every time when nobody has ever asked them about it. */
  assert.equal(masteryPercent(0, 0), null);
  assert.equal(masteryPercent(undefined, undefined), null);
  assert.equal(masteryPercent(null, null), null);
});

test('masteryPercent measures a genuine zero as zero', () => {
  /* And the converse must still work: a learner who has answered four
   * questions and got none right IS at 0%, and hiding that behind an em dash
   * would be the opposite error. */
  assert.equal(masteryPercent(0, 4), 0);
  assert.equal(masteryPercent(1, 3), 25);
  assert.equal(masteryPercent(3, 1), 75);
  assert.equal(masteryPercent(4, 0), 100);
});

test('studyStreak counts consecutive days, not rows', () => {
  /* Counting rows would report 3 for a learner who studied on three days
   * scattered across a month. */
  const scattered = [{ d: ago(0), n: 1 }, { d: ago(9), n: 1 }, { d: ago(20), n: 1 }];
  assert.equal(studyStreak(scattered, { today: TODAY }), 1);

  const run = [{ d: ago(0), n: 2 }, { d: ago(1), n: 1 }, { d: ago(2), n: 5 }];
  assert.equal(studyStreak(run, { today: TODAY }), 3);
});

test('studyStreak treats yesterday as a live streak and a gap as broken', () => {
  assert.equal(studyStreak([{ d: ago(1), n: 1 }, { d: ago(2), n: 1 }],
                           { today: TODAY }), 2,
    'not having studied YET today has not broken a streak');
  assert.equal(studyStreak([{ d: ago(2), n: 1 }, { d: ago(3), n: 1 }],
                           { today: TODAY }), 0,
    'a two-day gap is a broken streak, not a shorter one');
});

test('studyStreak ignores a day with zero attempts', () => {
  /* A row can exist with n === 0 only if the server changes, but a streak
   * built on "there is a row" rather than "there was work" would then count
   * days nobody studied. */
  assert.equal(studyStreak([{ d: ago(0), n: 0 }, { d: ago(1), n: 3 }],
                           { today: TODAY }), 1);
  assert.equal(studyStreak([], { today: TODAY }), 0);
  assert.equal(studyStreak(null, { today: TODAY }), 0);
});
