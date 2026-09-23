/**
 * Press the real colour buttons against a real server.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * Found 2026-09-23 while writing the dashboard how-to. The judgement buttons
 * passed the display word -- 'Red' -- as the colour, and `POST /attempts`
 * accepts exactly 'RED' | 'ORANGE' | 'GREEN' and refuses anything else rather
 * than guess. So every answer given through the live app was refused with a
 * 400. Every colour test before this one sent colours to the API directly;
 * none pressed a button, so none saw it. The same mismatch kept the "Which
 * part failed?" panel shut in every mode (`needGap` compares 'RED').
 *
 * And behind that, a second defect: the attempt is written when the colour is
 * chosen -- before the learner can know what they missed -- so gaps named
 * afterwards were never sent anywhere. The weak list was empty for every real
 * learner. They are now attached with `POST /attempts/<id>/gaps`.
 *
 * Every step below goes button -> component -> real client -> HTTP -> server
 * -> database, and is read back from the server.
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import test from 'node:test';

import { loadComponent, mount } from './render_harness.mjs';

const PORT = 9771 + (process.pid % 200);
const ORIGIN = `http://127.0.0.1:${PORT}`;

const SEED = `
import json, sys
sys.path.insert(0, ".")
from student.db import Database, new_id, now_iso
db = Database(sys.argv[1])
for email in ("red@example.com", "orange@example.com", "green@example.com", "early@example.com"):
    uid = db.create_user(email, "correct-horse")
    nid = new_id("nb")
    db.execute("INSERT INTO notebooks (id,owner_id,title,subject,created_at) VALUES (?,?,?,?,?)",
               (nid, uid, "Renal", "renal", now_iso()))
    db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
               "correct_index,rationale,validation_status,generated_by_candidate_id,"
               "prompt_version,generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               ("q-" + email.split("@")[0], nid, "mcq", "What does a low FeNa indicate?",
                json.dumps(["Pre-renal", "Intrinsic", "Post-renal", "Normal"]), 0,
                "Because.", "approved", "cand", "v1", now_iso()))
print("seeded")
`;

let server, dir, db, api, Component;

function run(cmd, args) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '', err = '';
    p.stdout.on('data', (d) => { out += d; });
    p.stderr.on('data', (d) => { err += d; });
    p.on('close', (code) => resolve({ code, out, err }));
  });
}

async function up(tries = 100) {
  for (let i = 0; i < tries; i++) {
    try { if ((await fetch(`${ORIGIN}/health`)).ok) return true; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

async function settle(pred, what) {
  for (let i = 0; i < 200; i++) {
    if (pred()) return;
    await new Promise((r) => setTimeout(r, 20));
  }
  throw new Error('timed out waiting for ' + what);
}

test.before(async () => {
  dir = mkdtempSync(join(tmpdir(), 'quintek-buttons-'));
  db = join(dir, 'q.db');
  writeFileSync(join(dir, 'seed.py'), SEED);
  const seeded = await run('python3', [join(dir, 'seed.py'), db]);
  if (seeded.code !== 0) throw new Error('seeding failed: ' + seeded.err);
  server = spawn('python3', ['-m', 'benchmark.cli', 'serve-student', '--host', '127.0.0.1',
    '--port', String(PORT), '--db', db, '--no-ai'],
    { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
  if (!await up()) throw new Error('the server did not start, so NOTHING was verified');
  globalThis.window = { __QUINTEK_STUDENT_API__: ORIGIN };
  api = await import('../../frontend/quintek-student-api.js?buttons=' + Date.now());
  Component = loadComponent();
});

test.after(() => {
  if (server) server.kill('SIGKILL');
  if (dir) rmSync(dir, { recursive: true, force: true });
  delete globalThis.window;
});

/** A learner at the colour question: logged in, session started, the live
 *  question served, an option picked and submitted. */
async function atTheColourQuestion(who) {
  api.setToken((await api.login(who + '@example.com', 'correct-horse')).token);
  const session = await api.startSession(1, 'adaptive');
  const next = await api.nextQuestion(session.session_id);
  assert.ok(next.question, 'the seeded question was not served');
  const inst = mount(Component, {
    route: 'revise', studentApi: api,
    liveSessionId: session.session_id, liveQuestion: next.question,
    liveQuestionId: next.question.question_id,
    picked: 1, submitted: true, judgement: null, gaps: [],
  });
  return inst;
}

async function press(inst, label) {
  const button = inst.renderVals().judgements.find((j) => j.label === label);
  assert.ok(button, `no ${label} button`);
  button.set();
  await settle(() => inst.state.attemptState && inst.state.attemptState !== 'saving',
               'the attempt to save');
}

for (const [who, label, stored] of [['red', 'Red', 'RED'], ['orange', 'Orange', 'ORANGE'],
                                    ['green', 'Green', 'GREEN']]) {
  test(`pressing ${label} records ${stored} and reveals the answer`, async () => {
    const inst = await atTheColourQuestion(who);
    await press(inst, label);
    assert.equal(inst.state.attemptError || '', '',
      `the server refused the ${label} button: ${inst.state.attemptError}`);
    assert.equal(inst.state.attemptState, 'ready');
    assert.ok(inst.state.liveReveal, 'no reveal after a recorded attempt');
    assert.ok(inst.state.liveAttemptId, 'the attempt id was not kept');
    // The server holds the colour the learner pressed, and nothing else.
    const bank = await api.questionBank();
    const row = (bank.questions || bank).find((q) => q.id === 'q-' + who);
    assert.equal(row.last_colour, stored);
    const v = inst.renderVals();
    assert.equal(v.needGap, stored !== 'GREEN',
      `the "which part failed?" panel is ${v.needGap ? 'open' : 'shut'} after ${label}`);
    assert.match(v.attemptLine, new RegExp(' · ' + label + '$'));
  });
}

test('a gap named after Red is saved to the weak list, against that answer', async () => {
  api.setToken((await api.login('red@example.com', 'correct-horse')).token);
  const session = await api.startSession(1, 'unseen').catch(() => null);
  // The red learner has answered their only question; answer it again through
  // a fresh session so this test does not depend on the one above.
  const s = session || await api.startSession(1, 'adaptive');
  const next = await api.nextQuestion(s.session_id);
  const inst = mount(Component, {
    route: 'revise', studentApi: api, liveSessionId: s.session_id,
    liveQuestion: next.question, liveQuestionId: next.question.question_id,
    picked: 1, submitted: true, judgement: null, gaps: [],
  });
  await press(inst, 'Red');
  let v = inst.renderVals();
  assert.equal(v.gapLive, true);
  assert.deepEqual(Array.from(v.gapOptions), [], 'a live question offered invented gap chips');
  assert.match(v.gapPrompt, /in your own words/);

  v.setGapDraft({ target: { value: 'FeNa interpretation' } });
  await inst.renderVals().addGap();
  v = inst.renderVals();
  assert.equal(v.gapError, '', v.gapError);
  assert.deepEqual(Array.from(v.gapOptions, (g) => g.label), ['FeNa interpretation ✓']);
  assert.equal(inst.state.gapDraft, '');

  const gaps = await api.gaps();
  const list = Array.isArray(gaps) ? gaps : gaps.gaps;
  assert.deepEqual(list.map((g) => [g.label, g.colour]), [['FeNa interpretation', 'RED']],
    'the gap did not reach the server, so the weak list stays empty');
});

test('an empty gap is not sent, and a gap before the answer is saved says why', async () => {
  const inst = await atTheColourQuestion('early');
  // Colour chosen but the attempt not yet back: no id to attach to.
  inst.setState({ judgement: 'RED', attemptState: 'saving', liveAttemptId: null,
                  gapDraft: 'something' });
  await inst.renderVals().addGap();
  assert.match(inst.state.gapError, /still being saved/);
  inst.setState({ gapDraft: '   ' });
  await inst.renderVals().addGap();
  assert.match(inst.state.gapError, /Type what you did not know/);
  const gaps = await api.gaps();
  assert.equal((Array.isArray(gaps) ? gaps : gaps.gaps).length, 0);
});

test('a gap the server refuses shows the reason, keeps the draft, and adds no chip', async () => {
  const inst = await atTheColourQuestion('orange');
  await press(inst, 'Orange');
  const long = 'x'.repeat(121);           // the input caps at 120; the server is the rule
  inst.renderVals().setGapDraft({ target: { value: long } });
  await inst.renderVals().addGap();
  const v = inst.renderVals();
  assert.match(v.gapError, /at most 120/);
  assert.deepEqual(Array.from(v.gapOptions), [],
    'a chip says "saved" for a gap the server refused');
  assert.equal(inst.state.gapDraft, long, 'the draft was thrown away on a refusal');
});

test('the design-mode chips are unchanged when there is no backend', () => {
  const inst = mount(Component, { route: 'revise', studentApi: null, submitted: true,
                                  picked: 0, qi: 0 });
  inst.renderVals().judgements.find((j) => j.label === 'Orange').set();
  const v = inst.renderVals();
  assert.equal(inst.state.judgement, 'ORANGE');
  assert.equal(v.needGap, true);
  assert.equal(v.gapLive, false);
  assert.ok(v.gapOptions.length > 0, 'the fixture chips vanished from design mode');
});
