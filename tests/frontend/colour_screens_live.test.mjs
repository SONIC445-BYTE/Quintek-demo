/**
 * What the colour screens RENDER, from real payloads, through the real
 * component.
 *
 * Every assertion here is on `renderVals()` output: the value the template
 * would show a learner. The payloads come from a running server, fetched with
 * the real client module, so the only thing standing between those payloads
 * and the assertions is the render code under test.
 *
 * THE DESIGN OF THE MAIN CASE
 * ---------------------------
 * Three learners. ONE concept, "Heart failure", chosen because it is also a
 * key in the design file's `CONCEPT_DATA` -- so the fixture fallback that
 * rendered another document's source and passage has a name to collide on.
 * Each learner answers the same kind of question correctly three times, so
 * CORRECTNESS is held constant. The only thing that differs between them is
 * the colour they chose. Whatever the screen shows must therefore come from
 * that colour and nothing else.
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import test from 'node:test';

import { loadComponent, mount, plain } from './render_harness.mjs';

const PORT = 9131 + (process.pid % 300);
const ORIGIN = `http://127.0.0.1:${PORT}`;

const SEED = `
import json, sys
sys.path.insert(0, ".")
from student.db import Database, new_id, now_iso
from student.api import StudentAPI
db = Database(sys.argv[1]); api = StudentAPI(db)

def concept(name, subject):
    cid = new_id("cpt")
    db.execute("INSERT INTO concepts (id,canonical_name,normalized_name,subject,first_seen_at)"
               " VALUES (?,?,?,?,?)", (cid, name, name.lower(), subject, now_iso()))
    return cid

HF = concept("Heart failure", "cardiology")          # collides with CONCEPT_DATA
RTA = concept("Renal tubular acidosis", "renal")     # subject outside the palette

def learner(email, colour, *, extra=False, gap=False):
    tok = api.handle("POST", "/auth/register", {},
                     {"email": email, "password": "correct-horse"}, None)[1]["token"]
    nid = api.handle("POST", "/notebooks", {},
                     {"title": "Cardio " + colour, "subject": "cardiology"}, tok)[1]["id"]
    sid, ck = new_id("src"), new_id("chk")
    db.execute("INSERT INTO sources (id,notebook_id,kind,filename,status,uploaded_at)"
               " VALUES (?,?,?,?,?,?)", (sid, nid, "pdf", "mine.pdf", "extracted", now_iso()))
    db.execute("INSERT INTO source_chunks (id,source_id,ordinal,text,locator_json,confidence,"
               "extraction_method,needs_review,status) VALUES (?,?,?,?,?,?,?,?,?)",
               (ck, sid, 1, "my own passage", json.dumps({"page": 2}), 0.9, "text", 0, "processed"))
    def question(qid, cpt):
        db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
                   "correct_index,rationale,source_id,chunk_id,validation_status,"
                   "generated_by_candidate_id,prompt_version,generated_at)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (qid, nid, "mcq", "Stem " + qid, json.dumps(["A","B","C","D"]), 0, "r",
                    sid, ck, "approved", "c", "v1", now_iso()))
        db.execute("INSERT INTO question_concepts (question_id,concept_id,role) VALUES (?,?,?)",
                   (qid, cpt, "target"))
        db.execute("INSERT OR IGNORE INTO notebook_concepts (notebook_id,concept_id) VALUES (?,?)",
                   (nid, cpt))
    q = "q-" + colour.lower()
    question(q, HF)
    for i in range(3):
        body = {"question_id": q, "user_answer": 0, "user_colour": colour}
        if gap and i == 0:
            body["gaps"] = ["Iron studies"]
        assert api.handle("POST", "/attempts", {}, body, tok)[0] == 201
    if extra:
        question("q-rta", RTA)          # present, never answered
    return tok

learner("red@example.com", "RED", extra=True, gap=True)
learner("orange@example.com", "ORANGE")
learner("green@example.com", "GREEN")

# A learner whose only concept nobody else has. The session-summary test runs
# as them, because every shared-concept session currently serves other
# learners' questions (ADR-030) and an attempt on one of those is refused --
# which would make the summary test fail for a reason that is not the summary.
SOLO = concept("Solo concept", "cardiology")
tok = api.handle("POST", "/auth/register", {},
                 {"email": "solo@example.com", "password": "correct-horse"}, None)[1]["token"]
nid = api.handle("POST", "/notebooks", {}, {"title": "Solo", "subject": "cardiology"}, tok)[1]["id"]
db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,correct_index,"
           "rationale,validation_status,generated_by_candidate_id,prompt_version,generated_at)"
           " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
           ("q-solo", nid, "mcq", "Solo stem", json.dumps(["A","B","C","D"]), 0, "r",
            "approved", "c", "v1", now_iso()))
db.execute("INSERT INTO question_concepts (question_id,concept_id,role) VALUES (?,?,?)",
           ("q-solo", SOLO, "target"))
db.execute("INSERT INTO notebook_concepts (notebook_id,concept_id) VALUES (?,?)", (nid, SOLO))
print("seeded")
`;

/** Strings that exist ONLY in the design file's record for "Heart failure".
 *  Any of them on the live screen is the collision. */
const FIXTURE_ONLY = [
  'Valvular disease notes.pdf',
  'Impaired filling or ejection',
  '22 ACROSS',
  'of 188',
];

function run(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { ...opts, stdio: ['ignore', 'pipe', 'pipe'] });
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

const CONCEPT_KEYS = ['cptState', 'cptStateColor', 'cptPriority', 'cptWhy', 'cptSource',
  'cptPassage', 'cptQCount', 'cptQuestions', 'cptEmpty', 'cptDue', 'cptInterval',
  'cptGaps', 'cptNoGaps', 'cptAttempts', 'cptByNotebook', 'cptLinks', 'headerTitle',
  'headerSub'];

function conceptView(inst) {
  const v = inst.renderVals();
  const out = {};
  for (const k of CONCEPT_KEYS) out[k] = v[k];
  return out;
}

let server, dir, api, Component;
const learners = {};

test.before(async () => {
  dir = mkdtempSync(join(tmpdir(), 'quintek-colour-'));
  const db = join(dir, 'q.db');
  writeFileSync(join(dir, 'seed.py'), SEED);
  const seeded = await run('python3', [join(dir, 'seed.py'), db], { cwd: process.cwd() });
  if (seeded.code !== 0) throw new Error(`seeding failed: ${seeded.err}`);

  server = spawn('python3', ['-m', 'benchmark.cli', 'serve-student', '--host', '127.0.0.1',
    '--port', String(PORT), '--db', db, '--no-ai'],
    { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
  if (!await up()) throw new Error('the server did not start, so NOTHING was verified');

  globalThis.window = { __QUINTEK_STUDENT_API__: ORIGIN };
  api = await import('../../frontend/quintek-student-api.js?colour=' + Date.now());
  Component = loadComponent();

  for (const colour of ['RED', 'ORANGE', 'GREEN']) {
    const email = colour.toLowerCase() + '@example.com';
    api.setToken((await api.login(email, 'correct-horse')).token);
    const concepts = await api.concepts();
    const list = Array.isArray(concepts) ? concepts : concepts.concepts;
    const hf = list.find((c) => c.canonical_name === 'Heart failure');
    const rta = list.find((c) => c.canonical_name === 'Renal tubular acidosis');
    const gaps = await api.gaps();
    learners[colour] = {
      concepts: list, hf, rta,
      hfDetail: await api.conceptDetail(hf.concept_id),
      rtaDetail: rta ? await api.conceptDetail(rta.concept_id) : null,
      gaps: Array.isArray(gaps) ? gaps : gaps.gaps,
      progress: await api.progress(),
      dashboard: await api.revisionDashboard(),
      bank: await api.questionBank(),
    };
  }
});

test.after(() => {
  if (server) server.kill('SIGKILL');
  if (dir) rmSync(dir, { recursive: true, force: true });
  delete globalThis.window;
});

function mountFor(colour, extra = {}) {
  const L = learners[colour];
  return mount(Component, {
    studentApi: api,
    learnerState: 'ready', liveProgress: L.progress, liveGaps: L.gaps,
    liveDashboard: L.dashboard, liveConcepts: L.concepts,
    bankState: 'ready', liveBank: L.bank,
    ...extra,
  });
}

function openOn(colour, detail) {
  return mountFor(colour, {
    route: 'concept',
    cpt: { name: detail.canonical_name, subject: detail.subject, id: detail.id },
    cptState: 'ready', liveConcept: detail,
  });
}

// ---------------------------------------------------------------------------
// A1 -- the concept screen follows the stored colour
// ---------------------------------------------------------------------------

test('the stored colour really does differ between the three learners', () => {
  // Without this, the render assertions below could pass on three identical
  // payloads and prove nothing.
  assert.equal(learners.RED.hfDetail.colour, 'RED');
  assert.equal(learners.ORANGE.hfDetail.colour, 'ORANGE');
  assert.equal(learners.GREEN.hfDetail.colour, 'GREEN');
  for (const c of ['RED', 'ORANGE', 'GREEN']) {
    assert.equal(learners[c].hfDetail.correct_count, 3, 'correctness must be held constant');
    assert.equal(learners[c].hfDetail.wrong_count, 0);
  }
});

for (const [colour, label, hex] of [['RED', 'Red', '#BC4C43'],
                                    ['ORANGE', 'Orange', '#B5812B'],
                                    ['GREEN', 'Green', '#1E7A57']]) {
  test(`concept screen renders ${label} for a concept the learner graded ${colour}`, () => {
    const v = conceptView(openOn(colour, learners[colour].hfDetail));
    assert.equal(v.cptState, label,
      `stored ${colour}, screen said ${v.cptState}. It is reading something other `
      + 'than the stored colour.');
    assert.equal(v.cptStateColor, hex);
    assert.match(v.cptWhy.find((w) => w.k === 'your colour').v, new RegExp(label));
  });
}

test('"Heart failure" never renders the design file\'s Heart failure', () => {
  for (const colour of ['RED', 'ORANGE', 'GREEN']) {
    const v = conceptView(openOn(colour, learners[colour].hfDetail));
    const rendered = JSON.stringify(v);
    for (const s of FIXTURE_ONLY) {
      assert.ok(!rendered.includes(s),
        `the ${colour} learner's "Heart failure" rendered "${s}", which exists only in `
        + 'the design file. That is another concept\'s document shown as theirs.');
    }
    assert.equal(v.headerTitle, 'Heart failure');
    // It shows THEIR numbers: one question, three right answers.
    assert.equal(v.cptQCount, '1 QUESTION ACROSS 1 FAMILY');
    assert.equal(v.cptWhy.find((w) => w.k === 'answered').v, '3 right of 3');
    assert.match(v.cptSource, new RegExp('Cardio ' + colour));
  }
});

test('a concept nobody has answered is "Not yet graded", not the server\'s ORANGE default', () => {
  const d = learners.RED.rtaDetail;
  assert.ok(d, 'the never-answered concept is missing from the fixture');
  assert.equal(d.colour, 'ORANGE',
    'the server no longer defaults an unanswered concept to ORANGE; revisit this test');
  const v = conceptView(openOn('RED', d));
  assert.equal(v.cptState, 'Not yet graded');
  assert.notEqual(v.cptStateColor, '#B5812B');
});

test('a subject outside the four-colour palette renders instead of throwing', () => {
  const d = learners.RED.rtaDetail;
  assert.equal(d.subject, 'renal');
  const v = conceptView(openOn('RED', d));          // threw on `.label` of undefined
  assert.equal(v.headerSub, 'Concept in renal');
});

test('with no concept id the screen says so, and borrows nothing', () => {
  const inst = mountFor('RED', { route: 'concept',
    cpt: { name: 'Heart failure', subject: 'cardiology', id: null },
    cptState: 'off', liveConcept: null });
  const v = conceptView(inst);
  assert.equal(v.cptState, 'Not linked');
  const rendered = JSON.stringify(v);
  for (const s of FIXTURE_ONLY) assert.ok(!rendered.includes(s), `borrowed "${s}"`);
});

test('while the concept is loading, nothing is borrowed either', () => {
  const d = learners.GREEN.hfDetail;
  const v = conceptView(mountFor('GREEN', { route: 'concept',
    cpt: { name: d.canonical_name, subject: d.subject, id: d.id },
    cptState: 'loading', liveConcept: null }));
  assert.equal(v.cptState, 'Loading…');
  const rendered = JSON.stringify(v);
  for (const s of FIXTURE_ONLY) assert.ok(!rendered.includes(s), `borrowed "${s}"`);
});

test('opening a concept from the weak list passes its id, so it can be fetched', () => {
  const loads = [];
  const inst = mount(Component, {
    studentApi: api, learnerState: 'ready', route: 'weak',
    liveProgress: learners.RED.progress, liveGaps: learners.RED.gaps,
    liveDashboard: learners.RED.dashboard, liveConcepts: learners.RED.concepts,
  }, { onLoad: (name, args) => loads.push([name, ...args]) });
  // Rebind openConcept's loader to the recorder -- it is captured by the
  // arrow function, so re-read it from the instance.
  const v = inst.renderVals();
  assert.ok(v.weakGaps.length >= 1, 'the RED learner tagged a gap; the weak list is empty');
  v.weakGaps[0].openConcept();
  assert.equal(inst.state.cpt.id, learners.RED.hf.concept_id,
    'the weak list opened the concept without its id, so the screen cannot fetch it');
  assert.equal(inst.state.cpt.subject, 'cardiology',
    'the weak list passed a hardcoded subject instead of the concept\'s own');
  assert.deepEqual(loads.find((l) => l[0] === 'loadConceptDetail'),
                   ['loadConceptDetail', learners.RED.hf.concept_id]);
});

// ---------------------------------------------------------------------------
// Found while fixing A1: the end-of-session summary
// ---------------------------------------------------------------------------

test('the end-of-session summary shows the session the learner just ran', async () => {
  // A real session: start, take the question, answer it ORANGE, complete.
  api.setToken((await api.login('solo@example.com', 'correct-horse')).token);
  const started = await api.startSession(1, 'adaptive');
  const next = await api.nextQuestion(started.session_id);
  assert.ok(next.question, 'the session served nothing');
  await api.recordAttempt(next.question.question_id, 1, 'ORANGE',
                          { sessionId: started.session_id });      // index 1 = wrong
  const summary = await api.completeSession(started.session_id);
  assert.equal(summary.questions, 1);

  const inst = mountFor('ORANGE', { route: 'dashboard', done: true,
    liveSessionId: started.session_id, liveSummary: summary });
  assert.equal(next.question.question_id, 'q-solo');
  const v = inst.renderVals();
  const byLabel = Object.fromEntries(v.analysis.map((a) => [a.label, a.n]));

  // The design file said 62% / 5 correct / 3 incorrect / 2 red.
  assert.equal(byLabel.Accuracy, '0%', `accuracy shown: ${byLabel.Accuracy}`);
  assert.equal(byLabel.Correct, '0');
  assert.equal(byLabel.Incorrect, '1');
  assert.equal(byLabel.Orange, '1', 'the one ORANGE judgement is not on the summary');
  assert.equal(byLabel.Red, '0');

  const rendered = JSON.stringify({ a: v.analysis, m: v.movement, r: v.readList });
  for (const s of ['Ferroportin', 'Ferritin interpretation', 'BNP interpretation',
                   'Iron handling.pdf', '62%']) {
    assert.ok(!rendered.includes(s), `the summary after a real session showed "${s}"`);
  }
  // The concept the session touched, in the learner's own grade -- and no
  // invented "before" state.
  assert.deepEqual(plain(v.movement.map((m) => [m.name, m.change])),
                   [['Solo concept', 'still Orange']]);
});

test('a live session whose summary failed shows nothing rather than the sample', () => {
  const inst = mountFor('ORANGE', { route: 'dashboard', done: true,
    liveSessionId: 'ses_x', liveSummary: null });
  const v = inst.renderVals();
  assert.deepEqual(plain([v.analysis, v.movement, v.readList]), [[], [], []]);
});
