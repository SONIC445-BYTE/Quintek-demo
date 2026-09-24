/**
 * The learner's side of the report loop, on the question bank.
 *
 * Until 2026-09-24 the report button was the end of the story: no screen said
 * whether anyone read a report, and a question an operator withdrew after
 * upholding one was labelled "Awaiting validation". Rows are built from the
 * payload shape `GET /questions` returns (pinned server-side by
 * tests/test_operator_surface.py) and rendered through the real component.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { loadComponent, mount } from './render_harness.mjs';

const Component = loadComponent();
const row = (over) => ({
  id: 'q1', stem: 'Low FeNa?', family: 'mcq', validation_status: 'approved',
  withheld: false, withheld_reason: null, my_report: null, attempt_count: 0,
  last_colour: null, generated_at: '2026-09-24T00:00:00Z', ...over,
});
const bank = (rows) => mount(Component, {
  route: 'bank', studentApi: { reportQuestion() {} }, bankState: 'ready', liveBank: rows,
}).renderVals().bank;

test('each report state reads as what happened to it', () => {
  const says = {
    open: 'You reported this · waiting for review',
    acknowledged: 'You reported this · seen by a reviewer',
    upheld: 'You reported this · upheld — withdrawn',
    rejected: 'You reported this · reviewed, not upheld',
  };
  for (const [state, text] of Object.entries(says)) {
    assert.equal(bank([row({ my_report: state })])[0].myReport, text);
  }
  assert.equal(bank([row({})])[0].myReport, '', 'an unreported question claims a report');
});

test('a withdrawn question says it was withdrawn, not that it awaits validation', () => {
  const [r] = bank([row({ withheld: true, stem: null, validation_status: 'flagged',
                          withheld_reason: 'withdrawn', my_report: 'upheld' })]);
  assert.match(r.stem, /^Withdrawn — a report on this question was upheld/);
  const [p] = bank([row({ withheld: true, stem: null, validation_status: 'pending',
                          withheld_reason: 'unvalidated' })]);
  assert.match(p.stem, /^Awaiting validation/);
});
