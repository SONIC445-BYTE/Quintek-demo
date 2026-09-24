/**
 * "How Quintek works" on Today, through the real component, the real client
 * and a running server.
 *
 * The exit condition: visible on a new account's first dashboard view, and
 * still reachable once the account has real data. Both are asserted from
 * state the SERVER returned -- a new account is one whose /progress reports
 * no attempts, not one this test declared new.
 *
 * The section's sentences are claims about behaviour. The ones that are
 * rules -- how a concept gets its colour, what the weak list holds, whether
 * reminders are delivered -- are checked here against the running system, so
 * the text and the behaviour cannot drift apart silently.
 *
 * Also pinned here, found while building this: with a backend configured,
 * Today and five screen subtitles showed the design file's figures ("4 red
 * gaps are unresolved, start with ferritin interpretation", "9 open gaps",
 * "188 concepts tracked") for as long as the real data took to load.
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import test from 'node:test';

import { loadComponent, mount, plain } from './render_harness.mjs';

const PORT = 9891 + (process.pid % 200);
const ORIGIN = `http://127.0.0.1:${PORT}`;

const SEED = `
import json, sys
sys.path.insert(0, ".")
from student.db import Database, new_id, now_iso
from student.api import StudentAPI
db = Database(sys.argv[1]); api = StudentAPI(db)
# "used": an account with real data -- one approved question, answered.
tok = api.handle("POST", "/auth/register", {},
                 {"email": "used@example.com", "password": "correct-horse"}, None)[1]["token"]
nid = api.handle("POST", "/notebooks", {}, {"title": "Renal", "subject": "renal"}, tok)[1]["id"]
db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
           "correct_index,rationale,validation_status,generated_by_candidate_id,"
           "prompt_version,generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
           ("q-used", nid, "mcq", "stem", json.dumps(["A", "B"]), 0, "r", "approved",
            "c", "v", now_iso()))
st, out = api.handle("POST", "/attempts", {},
                     {"question_id": "q-used", "user_answer": 1, "user_colour": "RED"}, tok)
assert st == 201, out
print("seeded")
`;

let server, dir, api, Component;

function run(cmd, args, input) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { cwd: process.cwd(), stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '', err = '';
    p.stdout.on('data', (d) => { out += d; });
    p.stderr.on('data', (d) => { err += d; });
    p.on('close', (code) => resolve({ code, out, err }));
    p.stdin.end(input || '');
  });
}

async function up(tries = 100) {
  for (let i = 0; i < tries; i++) {
    try { if ((await fetch(`${ORIGIN}/health`)).ok) return true; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

test.before(async () => {
  dir = mkdtempSync(join(tmpdir(), 'quintek-guide-'));
  const db = join(dir, 'q.db');
  writeFileSync(join(dir, 'seed.py'), SEED);
  const seeded = await run('python3', [join(dir, 'seed.py'), db]);
  if (seeded.code !== 0) throw new Error('seeding failed: ' + seeded.err);
  server = spawn('python3', ['-m', 'benchmark.cli', 'serve-student', '--host', '127.0.0.1',
    '--port', String(PORT), '--db', db, '--no-ai'],
    { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
  if (!await up()) throw new Error('the server did not start, so NOTHING was verified');
  globalThis.window = { __QUINTEK_STUDENT_API__: ORIGIN };
  api = await import('../../frontend/quintek-student-api.js?guide=' + Date.now());
  Component = loadComponent();
});

test.after(() => {
  if (server) server.kill('SIGKILL');
  if (dir) rmSync(dir, { recursive: true, force: true });
  delete globalThis.window;
});

/** The real loader, run against `inst`'s state. `mount` stubs every loader so
 *  no render test reaches the network by accident; these ones are the point. */
function real(inst, name) {
  const donor = new Component();
  donor.props = inst.props;
  Object.defineProperty(donor, 'state', { get: () => inst.state });
  donor.setState = (p) => inst.setState(p);
  return donor[name];
}

/** Today, as the app shows it after sign-in: the same two loads the component
 *  runs at mount, from the real server. */
async function today(email) {
  if (email) api.setToken((await api.login(email, 'correct-horse')).token);
  const inst = mount(Component, { route: 'today', studentApi: api });
  await real(inst, 'loadLearnerState')(api);
  await real(inst, 'loadReminders')(api);
  assert.equal(inst.state.learnerState, 'ready', inst.state.learnerError);
  return inst;
}

const item = (v, title) => {
  const hit = v.guideItems.find((g) => g.title === title);
  assert.ok(hit, `the how-to has no "${title}" item`);
  return hit;
};

test('a new account sees the how-to open on its first Today view', async () => {
  const out = await api.register(`fresh-${process.pid}@example.com`, 'correct-horse');
  api.setToken(out.token);
  const inst = await today();
  assert.equal(inst.state.liveProgress.attempts_total, 0, 'the account is not new');
  const v = inst.renderVals();
  assert.equal(v.isToday, true);
  assert.equal(v.guideOpen, true, 'a new account does not see the how-to');
  assert.deepEqual(plain(v.guideItems.map((g) => g.title)), [
    'Only checked questions', 'You choose the colour', 'How a concept gets its colour',
    'Your weak list', 'Reminders', 'Report a question']);
  assert.equal(v.guideToggleLabel, 'Hide');
  // `recommended_question_count` has a floor of 10; it is not a due count, and
  // a new account with no questions was told "10 questions are due".
  assert.ok(inst.state.liveDashboard.recommended_question_count >= 10);
  assert.equal(v.todayHeadline, 'Nothing is due and no gaps are open.');
});

test('an account with data sees it closed, and one tap opens it', async () => {
  const inst = await today('used@example.com');
  assert.ok(inst.state.liveProgress.attempts_total > 0, 'the seeded attempt is missing');
  let v = inst.renderVals();
  assert.equal(v.guideOpen, false);
  assert.equal(v.guideToggleLabel, 'Show');
  assert.match(v.guideSub, /Colours, your weak list, reminders and reporting/);
  v.toggleGuide();
  v = inst.renderVals();
  assert.equal(v.guideOpen, true, 'the section cannot be opened once there is data');
  assert.equal(v.guideItems.length, 6);
  v.toggleGuide();
  assert.equal(inst.renderVals().guideOpen, false, 'a new learner cannot close it either');
});

test('More links to it from anywhere, and it lands open on Today', async () => {
  const inst = await today('used@example.com');
  inst.setState({ route: 'settings' });
  const link = inst.renderVals().moreLinks.find((l) => l.label === 'How Quintek works');
  assert.ok(link, 'More has no link to the how-to');
  link.go();
  const v = inst.renderVals();
  assert.equal(v.isToday, true);
  assert.equal(v.guideOpen, true);
});

test('the reminders item says delivery is off, because the server says so', async () => {
  const inst = await today('used@example.com');
  const listed = await api.listReminders();
  assert.equal(listed.delivery_configured, false);
  const note = item(inst.renderVals(), 'Reminders').note;
  assert.match(note, /Nothing delivers them in a browser/);
  // And it states nothing either way until the server has answered.
  inst.setState({ remState: 'loading' });
  assert.match(item(inst.renderVals(), 'Reminders').note, /Settings shows how reminders are delivered/);
  // With a sender configured, the "nothing delivers them" sentence goes.
  inst.setState({ remState: 'ready', remDelivery: true });
  assert.equal(item(inst.renderVals(), 'Reminders').note, '');
});

test('the colour rule it states is the rule the server applies', async () => {
  const v = mount(Component, { route: 'today', studentApi: api }).renderVals();
  const body = item(v, 'How a concept gets its colour').body;
  assert.match(body, /last five colours/);
  assert.match(body, /Two Reds among them make it Red/);
  assert.match(body, /two most recent are Green and none of the five is Red/);
  assert.match(body, /Anything else is Orange/);
  // Each sentence, as a case, against the function the server calls.
  const r = await run('python3', ['-c', `
import sys, json; sys.path.insert(0, ".")
from student.knowledge import derive_concept_colour as d, EVIDENCE_WINDOW
print(json.dumps({
  "window": EVIDENCE_WINDOW,
  "two_reds": d(["GREEN", "RED", "GREEN", "RED", "GREEN"]),
  "red_sixth_ignored": d(["GREEN", "GREEN", "GREEN", "GREEN", "GREEN", "RED", "RED"]),
  "two_recent_green": d(["GREEN", "GREEN", "ORANGE", "ORANGE", "ORANGE"]),
  "green_with_a_red": d(["GREEN", "GREEN", "ORANGE", "RED"]),
  "one_green": d(["GREEN", "ORANGE"]),
  "one_red": d(["RED"]),
}))`]);
  assert.equal(r.code, 0, r.err);
  assert.deepEqual(JSON.parse(r.out), {
    window: 5, two_reds: 'RED', red_sixth_ignored: 'GREEN', two_recent_green: 'GREEN',
    green_with_a_red: 'ORANGE', one_green: 'ORANGE', one_red: 'ORANGE',
  });
});

test('the weak list holds only what the learner named', async () => {
  // "used" answered Red and named nothing: the guide says that answer is not
  // on the weak list, and it is not.
  api.setToken((await api.login('used@example.com', 'correct-horse')).token);
  const gaps = await api.gaps();
  assert.deepEqual(Array.isArray(gaps) ? gaps : gaps.gaps, []);
  const v = mount(Component, { route: 'today', studentApi: api }).renderVals();
  assert.match(item(v, 'Your weak list').body, /a Red answer with nothing named does not appear/);
  assert.match(item(v, 'Your weak list').note, /no button in the app yet to mark a gap resolved/);
});

test('the colour labels it names are the buttons the learner presses', () => {
  const inst = mount(Component, { route: 'revise', submitted: true, picked: 0 });
  const buttons = inst.renderVals().judgements.map((j) => j.label + ' — ' + j.note.toLowerCase());
  const body = item(inst.renderVals(), 'You choose the colour').body;
  for (const b of buttons) assert.ok(body.includes(b), `the how-to does not name "${b}"`);
});

test('the report kinds it lists are the ones the report sheet offers', () => {
  const inst = mount(Component, { route: 'revise', studentApi: api, submitted: true });
  const body = item(inst.renderVals(), 'Report a question').body.toLowerCase();
  assert.match(body, /report a problem with this question/);
  for (const k of ['the content', 'the marked answer', 'not in your source',
                   'more than one answer fits', 'unsafe or offensive', 'something else']) {
    assert.ok(body.includes(k), `missing report kind: ${k}`);
  }
  assert.equal(inst.renderVals().reportLabel, 'Report a problem with this question');
});

test('while the real data loads, no design-file figure is shown as the learner\'s', () => {
  const inst = mount(Component, { route: 'today', studentApi: api, learnerState: 'loading',
                                  nbsState: 'loading', demosState: 'loading',
                                  graphState: 'loading' });
  const v = inst.renderVals();
  assert.equal(v.todayHeadline, 'Loading your day…');
  assert.deepEqual(Array.from(v.colourCounts), []);
  assert.deepEqual(Array.from(v.topGaps), []);
  // What Today binds (see the isToday block of the template).
  const seen = JSON.stringify(['todayHeadline', 'todayWhy', 'colourCounts', 'topGaps',
                               'headerSub', 'streak'].map((k) => v[k]));
  for (const fixture of ['ferritin', '4 red gaps', 'Thursday 15 August']) {
    assert.ok(!seen.includes(fixture), `Today shows the fixture's "${fixture}" while loading`);
  }
  assert.equal(v.todayDemo, false);
  for (const route of ['today', 'weak', 'progress', 'home', 'graph', 'demos']) {
    inst.setState({ route });
    const sub = inst.renderVals().headerSub;
    assert.equal(sub, 'Loading…', `${route} subtitle while loading: ${sub}`);
  }
  // Every other builder with a design-file fallback: empty, not the fixture,
  // while its own data is on its way (and before its screen was ever opened).
  for (const k of ['heat', 'masteryStats', 'conceptMastery', 'notebooks', 'bank', 'demos']) {
    assert.deepEqual(Array.from(v[k] || []), [], `${k} shows fixture rows while loading`);
  }
  assert.ok(!/41-day|84 sessions/.test(JSON.stringify(v)), 'the fixture streak is shown');
  const graphLink = v.moreLinks.find((l) => l.label === 'Concept graph');
  assert.equal(graphLink.note, 'Concepts and how they connect',
    'More quotes the fixture graph before the learner has opened theirs');
  // Before the graph screen has ever been opened its state is 'off', not
  // 'loading' -- which is exactly when More is most likely to be read.
  const untouched = mount(Component, { route: 'more', studentApi: api });
  assert.equal(untouched.state.graphState, 'off');
  assert.equal(untouched.renderVals().moreLinks.find((l) => l.label === 'Concept graph').note,
    'Concepts and how they connect');
  // Closed while the learner state is unknown, so an account with data never
  // has it flash open.
  assert.equal(v.guideOpen, false);
});

test('design mode keeps its figures, labelled as a demo, with the how-to closed', () => {
  const v = mount(Component, { route: 'today', studentApi: null, learnerState: 'off' })
    .renderVals();
  assert.equal(v.todayDemo, true);
  assert.match(v.todayHeadline, /4 red gaps/);
  assert.equal(v.guideOpen, false);
});

test('a live Today no longer carries the "Demo data" banner', async () => {
  const inst = await today('used@example.com');
  assert.equal(inst.renderVals().todayDemo, false,
    'a live account is told its own counts are prototype values');
});
