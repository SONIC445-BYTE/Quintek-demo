/**
 * The screens APP_BEHAVIOUR 2.6-2.8 recorded as FIXTURE, driven against a
 * real server through the real client module.
 *
 * WHAT THIS COVERS
 * ----------------
 * Item T of the manual test plan named five screens still rendering module
 * constants: progress, gaps, mastery, demonstrations and the studio. Gaps was
 * already live. The rest are wired now, along with the notebook list, the
 * reminder settings and the revision dashboard's strategy picker -- all of
 * which turned out to be the same class of problem.
 *
 * Every assertion below is on a payload SHAPE the `.dc.html` binds. The
 * component itself is not rendered here (that needs React and a DOM); what is
 * checked is that the field the screen reads exists, is named what the screen
 * thinks it is named, and carries what the screen claims it carries. Three of
 * the bugs this file was written for were exactly that: `nb.sources` against a
 * payload whose `sources` is an array, `WEAKNESS_FIRST` against a server that
 * accepts six other names, and a heatmap built positionally from a list the
 * server returns newest-first and sparse.
 *
 * Skips loudly if the server cannot start. A skip is not a pass.
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import test from 'node:test';

const PORT = 8231 + (process.pid % 300);
const ORIGIN = `http://127.0.0.1:${PORT}`;

/**
 * A learner with enough history for every screen to have something to say.
 *
 * Two notebooks, so the notebook list cannot pass by rendering one row. Two
 * concepts in DIFFERENT colours, so the mastery split cannot pass by
 * returning one bucket. One concept deliberately has NO attempts, which is
 * the case that made "0%" a lie. Attempts are dated on two separate days so
 * the heatmap has something to place, and one of them is old enough to sit in
 * a different column from today.
 */
const SEED = `
import json, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")
from student.db import Database, new_id, now_iso
db = Database(sys.argv[1])
uid = db.create_user("screens@example.com", "correct-horse")

def iso(days_ago):
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")

nb1, nb2 = new_id("nb"), new_id("nb")
db.execute("INSERT INTO notebooks (id,owner_id,title,subject,created_at) VALUES (?,?,?,?,?)",
           (nb1, uid, "Cardiology", "cardiology", now_iso()))
# A subject OUTSIDE the four-colour palette, so an unmapped subject cannot
# throw on .label of undefined the way it used to on the graph.
db.execute("INSERT INTO notebooks (id,owner_id,title,subject,created_at) VALUES (?,?,?,?,?)",
           (nb2, uid, "Renal", "renal", now_iso()))

sid = new_id("src")
db.execute("INSERT INTO sources (id,notebook_id,kind,filename,status,uploaded_at)"
           " VALUES (?,?,?,?,?,?)", (sid, nb1, "pdf", "notes.pdf", "extracted", now_iso()))

c_attempted, c_untouched = new_id("cpt"), new_id("cpt")
for cid, name, subject, nb in ((c_attempted, "Ferritin", "biochemistry", nb1),
                               (c_untouched, "Loop diuretics", "renal", nb2)):
    db.execute("INSERT INTO concepts (id,canonical_name,normalized_name,subject,first_seen_at)"
               " VALUES (?,?,?,?,?)", (cid, name, name.lower(), subject, now_iso()))
    db.execute("INSERT INTO notebook_concepts (notebook_id,concept_id) VALUES (?,?)", (nb, cid))

# RED on the attempted concept, so colour_counts has a non-zero RED and the
# untouched one stays at its default.
db.execute("INSERT INTO concept_state (user_id,concept_id,colour,correct_count,wrong_count,"
           "consecutive_correct,last_seen_at) VALUES (?,?,?,?,?,?,?)",
           (uid, c_attempted, "RED", 1, 3, 0, iso(0)))

qids = []
for i in range(3):
    qid = "q-screen-%d" % i
    qids.append(qid)
    db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
               "correct_index,rationale,source_id,validation_status,"
               "generated_by_candidate_id,prompt_version,generated_at)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (qid, nb1, "mcq", "Question %d about ferritin?" % i,
                json.dumps(["A", "B", "C", "D"]), 0, "Because.", sid,
                "approved" if i < 2 else "pending", "cand", "v1", iso(i)))
    db.execute("INSERT INTO question_concepts (question_id,concept_id,role) VALUES (?,?,?)",
               (qid, c_attempted, "target"))

# Attempts on two distinct days, so the heat grid has two populated cells in
# two different columns rather than one.
for n, (qid, days) in enumerate(((qids[0], 0), (qids[1], 0), (qids[0], 5))):
    db.execute("INSERT INTO attempts (id,user_id,question_id,session_id,user_answer,"
               "correct_answer,is_correct,user_colour,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (new_id("att"), uid, qid, None, 0, 0, 1 if n else 0,
                "GREEN" if n else "RED", iso(days)))

db.execute("INSERT INTO question_demos (id,owner_id,title,question,question_type,"
           "difficulty,reasoning_depth,stem_structure,question_target,"
           "distractor_strategy,answer_format,notes,created_at)"
           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (new_id("demo"), uid, "Vignette style", "A 64-year-old presents with...",
            "clinical vignette", "pg2", "two-step", "stem then lead-in",
            "single best answer", "common misconception", "", "", now_iso()))
print("seeded")
`;

function run(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const proc = spawn(cmd, args, { ...opts, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '', err = '';
    proc.stdout.on('data', (d) => { out += d; });
    proc.stderr.on('data', (d) => { err += d; });
    proc.on('close', (code) => resolve({ code, out, err }));
  });
}

async function waitForHealth(tries = 80) {
  for (let i = 0; i < tries; i++) {
    try {
      const res = await fetch(`${ORIGIN}/health`);
      if (res.ok) return true;
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

test('the remaining screens read live data through the real client', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'quintek-screens-'));
  const dbPath = join(dir, 'q.db');
  const seedPath = join(dir, 'seed.py');
  writeFileSync(seedPath, SEED);

  const seeded = await run('python3', [seedPath, dbPath], { cwd: process.cwd() });
  assert.equal(seeded.code, 0, `seeding failed: ${seeded.err}`);

  const server = spawn('python3', ['-m', 'benchmark.cli', 'serve-student',
    '--host', '127.0.0.1', '--port', String(PORT), '--db', dbPath, '--no-ai'],
    { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
  let serverErr = '';
  server.stderr.on('data', (d) => { serverErr += d; });
  t.after(() => { server.kill('SIGKILL'); rmSync(dir, { recursive: true, force: true }); });

  const up = await waitForHealth();
  if (!up) {
    assert.fail(`the server did not start, so NOTHING was verified: ${serverErr.slice(0, 600)}`);
  }

  globalThis.window = { __QUINTEK_STUDENT_API__: ORIGIN };
  const api = await import('../../frontend/quintek-student-api.js?screens=' + Date.now());
  assert.equal(api.configured, true, 'the client did not see a configured backend');
  const login = await api.login('screens@example.com', 'correct-horse');
  api.setToken(login.token);

  // --- notebooks --------------------------------------------------------
  const notebooks = await api.notebooks();
  assert.equal(notebooks.length, 2, 'both notebooks should come back');

  // The fixture called these `sources`, `concepts` and `questions` and they
  // were NUMBERS. The server's names are different and `sources` elsewhere in
  // the API is an ARRAY, so binding the fixture's names against this payload
  // renders a blank or "[object Object] sources".
  const cardio = notebooks.find((n) => n.title === 'Cardiology');
  assert.ok(cardio, 'the Cardiology notebook is missing');
  assert.equal(typeof cardio.source_count, 'number');
  assert.equal(typeof cardio.concept_count, 'number');
  assert.equal(typeof cardio.question_count, 'number');
  assert.equal(typeof cardio.due_count, 'number');
  assert.equal(cardio.source_count, 1);
  assert.equal(cardio.question_count, 3);
  assert.equal(cardio.sources, undefined,
    'the notebook list payload gained a `sources` field; the screen binds source_count');

  // A notebook whose subject is outside the four-colour palette. Indexing
  // SUBJ[...] directly on this would throw on `.label` of undefined and blank
  // the whole list, which is the bug already fixed once on the graph.
  const renal = notebooks.find((n) => n.title === 'Renal');
  assert.equal(renal.subject, 'renal');

  // --- the question bank ------------------------------------------------
  const bank = await api.questionBank();
  assert.equal(bank.length, 3);
  const row = bank[0];
  for (const field of ['id', 'stem', 'family', 'validation_status',
                       'notebook_title', 'attempt_count', 'generated_at']) {
    assert.ok(field in row, `the bank row is missing ${field}, which the screen binds`);
  }
  // The server's vocabulary is lowercase. The fixture's filter tabs are
  // 'Approved' / 'Flagged' / 'Pending', so the screen title-cases the real
  // value rather than mapping an unknown status onto a familiar one.
  const statuses = bank.map((q) => q.validation_status).sort();
  assert.deepEqual(statuses, ['approved', 'approved', 'pending']);
  assert.equal(bank.filter((q) => q.validation_status === 'approved').length, 2,
    'the Approved tab must not be empty for this learner');

  // --- demonstrations ---------------------------------------------------
  // `listDemos` returns the envelope, not the array -- the screen reads
  // `payload.demos`. Pinned here so a later "tidy-up" that unwraps it in the
  // client cannot silently empty the demonstrations screen.
  const demoPayload = await api.listDemos();
  const demos = demoPayload.demos;
  assert.ok(Array.isArray(demos), 'listDemos should return { demos: [...] }');
  assert.equal(demos.length, 1);
  // The screen renders a spec table from these named columns. The fixture
  // held the same information as [key, value] pairs.
  for (const field of ['id', 'title', 'question', 'question_type', 'difficulty',
                       'reasoning_depth', 'stem_structure', 'question_target',
                       'distractor_strategy', 'answer_format', 'created_at']) {
    assert.ok(field in demos[0], `the demonstration is missing ${field}`);
  }
  assert.equal(demos[0].notes, '',
    'a blank field must come back blank, so the spec table can omit its row');

  // --- progress: the mastery split and the heatmap ----------------------
  const progress = await api.progress();
  assert.equal(typeof progress.concepts_tracked, 'number');
  assert.equal(progress.colour_counts.RED, 1, 'the red concept should be counted');
  assert.equal(typeof progress.due_count, 'number');
  assert.equal(progress.attempts_total, 3);

  // THE HEATMAP CONTRACT. `activity` is sparse and ordered NEWEST FIRST, so a
  // grid built from its position would paint the two study days adjacent at
  // one end and call the result twelve weeks of history. It has to be keyed
  // by date.
  assert.ok(Array.isArray(progress.activity));
  assert.equal(progress.activity.length, 2,
    'two distinct study days were seeded; activity should have exactly two rows');
  const days = progress.activity.map((a) => a.d);
  assert.deepEqual([...days].sort().reverse(), days, 'activity is newest-first');
  assert.notEqual(days[0], days[1], 'the two rows should be different dates');
  for (const a of progress.activity) {
    assert.match(a.d, /^\d{4}-\d{2}-\d{2}$/, 'a day key must be a plain ISO date');
    assert.equal(typeof a.n, 'number');
  }
  assert.equal(progress.activity.reduce((t, a) => t + a.n, 0), 3,
    'every attempt should be accounted for in the activity grid');

  // --- concept mastery --------------------------------------------------
  const concepts = await api.concepts();
  assert.equal(concepts.length, 2);
  const attempted = concepts.find((c) => c.canonical_name === 'Ferritin');
  const untouched = concepts.find((c) => c.canonical_name === 'Loop diuretics');

  assert.equal(attempted.correct_count, 1);
  assert.equal(attempted.wrong_count, 3);
  assert.equal(attempted.colour, 'RED');

  // THE 0% TRAP. A concept nobody has been asked about has no percentage.
  // Rendering `correct / (correct + wrong)` here divides by zero, and
  // rendering the 0 that falls out of `|| 0` tells a learner they get this
  // wrong every time when they have never seen it.
  assert.equal(untouched.correct_count, 0);
  assert.equal(untouched.wrong_count, 0);
  assert.equal(untouched.correct_count + untouched.wrong_count, 0,
    'the untouched concept must have no attempts, which is the case the screen '
    + 'renders as an em dash rather than as 0%');

  // --- the revision dashboard and its strategy picker -------------------
  const dashboard = await api.revisionDashboard();
  assert.equal(typeof dashboard.recommended_question_count, 'number');
  assert.ok(dashboard.recommended_question_count > 0);
  assert.equal(typeof dashboard.open_gaps, 'number');
  assert.ok(dashboard.colour_counts, 'the dashboard carries the counts the queue stats show');

  // THE STRATEGIES THE SERVER ACTUALLY ACCEPTS. The picker offered
  // WEAKNESS_FIRST, WEAK_PLUS_SECTION and COVERAGE -- none of which exist --
  // and `startRevision` passed the literal 'adaptive' regardless, so choosing
  // one changed a highlight and nothing else.
  assert.deepEqual(dashboard.strategies,
    ['adaptive', 'red', 'orange', 'green', 'due', 'unseen'],
    'the strategy list the picker is built from has changed');

  // The status code alone cannot answer this. `student/api.py:1037` maps EVERY
  // ValueError out of `start_session` to 422, and the engine raises one for
  // two unrelated reasons: an unknown strategy NAME, and a known strategy that
  // matched no questions. So "orange" on a learner with nothing orange comes
  // back 422 exactly like a typo would.
  //
  // That conflation is a real wart -- an empty queue is a state to render, not
  // an error -- and it is recorded as one rather than papered over here. The
  // check below is on the MESSAGE, which does distinguish them.
  const startWith = async (strategy) => {
    const res = await fetch(`${ORIGIN}/revision/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 Authorization: `Bearer ${login.token}` },
      body: JSON.stringify({ count: 3, strategy }),
    });
    let body = {};
    try { body = await res.json(); } catch { /* not json */ }
    return { status: res.status, detail: String(body.error || body.detail || '') };
  };

  for (const strategy of dashboard.strategies) {
    const { status, detail } = await startWith(strategy);
    assert.ok(!/unknown strategy/.test(detail),
      `the server does not know the strategy '${strategy}', which the picker offers: `
      + `${status} ${detail}`);
  }

  // And an invented one still IS unknown, so the loop above is not passing
  // because the server stopped validating names.
  const bogus = await startWith('WEAKNESS_FIRST');
  assert.equal(bogus.status, 422);
  assert.match(bogus.detail, /unknown strategy/,
    'the server accepted WEAKNESS_FIRST, so this test can no longer tell a '
    + 'real strategy name from an invented one');

  // --- reminders (ADR-031) ----------------------------------------------
  // The single daily "trigger" is retired. Reminders are rows the learner
  // writes; the screen-level behaviour is in reminders_screen_live.test.mjs,
  // and this checks the client against the served routes end to end.
  const empty = await api.listReminders();
  assert.deepEqual(empty.reminders, []);
  assert.equal(empty.delivery_configured, false,
    'a sender is configured now; the screen\'s "delivery is off" copy needs revisiting');

  const label = 'Iron studies before the ward round\n  (bring the ferritin chart)';
  const one = await api.createReminder({ label, localDate: '2099-01-10',
                                         localTime: '07:30', timezone: 'Asia/Kolkata' });
  const two = await api.createReminder({ label: 'revise micro', localDate: '2099-01-11',
                                         localTime: '20:00', timezone: 'Asia/Kolkata' });
  assert.equal(one.label, label, 'the label came back changed');
  assert.equal(one.fire_at, '2099-01-10T02:00:00Z');

  // It PERSISTED, rather than being echoed back.
  const listed = await api.listReminders();
  assert.deepEqual(listed.reminders.map((r) => r.label), [label, 'revise micro']);

  const edited = await api.updateReminder(one.id, { localTime: '08:00' });
  assert.equal(edited.local_time, '08:00');
  assert.equal(edited.label, label, 'an edit of the time rewrote the text');

  const cancelled = await api.cancelReminder(two.id);
  assert.equal(cancelled.status, 'cancelled');
  const after = await api.listReminders();
  assert.equal(after.reminders.find((r) => r.id === one.id).status, 'pending',
    'cancelling one reminder changed another');

  // A refusal carries the server's reason through the client.
  await assert.rejects(
    api.createReminder({ label: 'x', localDate: '2099-10-25', localTime: '01:30',
                         timezone: 'Europe/London' }),
    /happens twice in Europe\/London/);

  delete globalThis.window;
});
