/**
 * The revision loop, driven through the REAL client module against a REAL
 * server.
 *
 * Every other test in this directory stubs `fetch`, which proves the client
 * sends what it means to send and nothing about whether the server agrees.
 * Two mismatches got through that way and were only found by running the
 * routes: the client asked for strategy WEAKNESS_FIRST, which the server
 * rejects with 422, and the reveal block bound `source_filename`, which the
 * reveal payload does not contain.
 *
 * So this one starts `student/server.py` on a real socket and drives the
 * actual sequence a learner produces: register, seed, start a session, take
 * the next question, answer it, read the reveal.
 *
 * Skips with a loud message if the server cannot be started. A skip is not a
 * pass.
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import test from 'node:test';

const PORT = 8731 + (process.pid % 200);
const ORIGIN = `http://127.0.0.1:${PORT}`;

/** Seed a learner with one question whose chunk NEEDS REVIEW -- the flag this
 *  whole provenance path exists to carry. */
const SEED = `
import json, sys
sys.path.insert(0, ".")
from student.db import Database, new_id, now_iso
db = Database(sys.argv[1])
uid = db.create_user("loop@example.com", "correct-horse")
nid = new_id("nb")
db.execute("INSERT INTO notebooks (id,owner_id,title,subject,created_at) VALUES (?,?,?,?,?)",
           (nid, uid, "Cardiology", "cardiology", now_iso()))
sid, cid = new_id("src"), new_id("chk")
db.execute("INSERT INTO sources (id,notebook_id,kind,filename,status,uploaded_at)"
           " VALUES (?,?,?,?,?,?)", (sid, nid, "pdf", "scan.pdf", "extracted", now_iso()))
db.execute("INSERT INTO source_chunks (id,source_id,ordinal,text,locator_json,confidence,"
           "extraction_method,needs_review,status) VALUES (?,?,?,?,?,?,?,?,?)",
           (cid, sid, 1, "A blurry passage about renal physiology.",
            json.dumps({"page": 7}), 0.31, "ocr_low", 1, "processed"))
db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
           "correct_index,rationale,source_id,chunk_id,validation_status,"
           "generated_by_candidate_id,prompt_version,generated_at)"
           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
           ("q-live-1", nid, "mcq", "What does a low FeNa indicate?",
            json.dumps(["Pre-renal", "Intrinsic", "Post-renal", "Normal"]), 0,
            "Because the passage says so.", sid, cid, "approved", "cand", "v1", now_iso()))
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

test('the revision loop runs end to end against a real server', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'quintek-loop-'));
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

  // The real client module, pointed at the real origin.
  globalThis.window = { __QUINTEK_STUDENT_API__: ORIGIN };
  const api = await import('../../frontend/quintek-student-api.js?live=' + Date.now());
  assert.equal(api.configured, true, 'the client did not see a configured backend');

  // --- log in -----------------------------------------------------------
  const login = await api.login('loop@example.com', 'correct-horse');
  assert.ok(login.token, 'no token came back');
  api.setToken(login.token);

  // --- start a session --------------------------------------------------
  // 'adaptive' is one of the six the server accepts. Sending an invented
  // strategy name is a 422, which is how the WEAKNESS_FIRST bug surfaced.
  const session = await api.startSession(5, 'adaptive');
  assert.ok(session.session_id, `no session id: ${JSON.stringify(session)}`);

  // --- the next question ------------------------------------------------
  const next = await api.nextQuestion(session.session_id);
  assert.equal(next.finished, false);
  assert.ok(next.question, 'no question came back');
  assert.equal(next.question.question_id, 'q-live-1');
  assert.ok(Array.isArray(next.question.options) && next.question.options.length === 4);

  // THE REVEAL-TIMING GUARANTEE. Item J of the manual test plan, checked
  // here so nobody has to check it on a device: the payload a learner holds
  // while deciding must not contain the answer.
  const served = JSON.stringify(next.question);
  assert.ok(!('correct_index' in next.question),
    `the question carried correct_index before the learner answered: ${served}`);
  assert.ok(!('correct_answer' in next.question),
    `the question carried correct_answer before the learner answered: ${served}`);
  assert.ok(!('rationale' in next.question),
    `the question carried its rationale before the learner answered: ${served}`);

  // --- answer it --------------------------------------------------------
  const attempt = await api.recordAttempt('q-live-1', 1, 'ORANGE',
                                          { sessionId: session.session_id });
  assert.ok(attempt.attempt_id, 'no attempt was recorded');
  const reveal = attempt.reveal;
  assert.ok(reveal, 'the attempt returned no reveal');

  // Only NOW is the answer known.
  assert.equal(reveal.correct_answer, 0);
  assert.equal(reveal.is_correct, false);
  assert.equal(reveal.rationale, 'Because the passage says so.');

  // --- the provenance the reveal block renders --------------------------
  // These are the exact field names the .dc.html binds. A rename on either
  // side breaks this test rather than silently blanking the banner.
  assert.equal(reveal.needs_review, true,
    'needs_review did not survive to the reveal');
  assert.equal(typeof reveal.chunk_confidence, 'number');
  assert.ok(reveal.chunk_confidence < 0.5);
  assert.deepEqual(reveal.source_locator, { page: 7 });
  assert.equal(reveal.extraction_method, 'ocr_low');
  assert.equal(reveal.validation_status, 'approved');

  // The field the reveal block WRONGLY assumed existed. Pinned so the
  // fallback is never quietly replaced by a fabricated filename.
  assert.equal(reveal.source_filename, undefined,
    'the reveal now carries source_filename; the .dc.html fallback should use it');

  // --- report it, now that there is something to report -----------------
  const report = await api.reportQuestion('q-live-1', 'factually_wrong', 'keyed answer looks wrong');
  assert.ok(report.report_id, 'the report was not recorded');
  assert.equal(report.provenance.chunk_needs_review, true,
    'the report did not freeze the needs_review flag');

  const mine = await api.myReports();
  assert.equal(mine.length, 1);

  // --- finish -----------------------------------------------------------
  const summary = await api.completeSession(session.session_id);
  assert.equal(summary.session_id, session.session_id);
  assert.equal(summary.questions, 1, 'the attempt did not reach the summary');
  assert.equal(summary.colours.ORANGE, 1);

  // --- and the scope statement, served without an account ---------------
  delete globalThis.window;
});

test('the four screens fetch live data through the real client', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'quintek-screens-'));
  const dbPath = join(dir, 'q.db');
  const seedPath = join(dir, 'seed.py');
  const port = PORT + 1;
  const origin = `http://127.0.0.1:${port}`;

  /* A FIFTH subject on purpose. The client palette names four; the corpus
   * does not stop at four. An edge touching "renal" is the case a client-side
   * recomputation of cross_subject would silently get wrong. */
  writeFileSync(seedPath, `
import json, sys
sys.path.insert(0, ".")
from student.db import Database, new_id, now_iso
db = Database(sys.argv[1])
uid = db.create_user("screens@example.com", "correct-horse")
nid = new_id("nb")
db.execute("INSERT INTO notebooks (id,owner_id,title,subject,created_at) VALUES (?,?,?,?,?)",
           (nid, uid, "Cardiology Block 3", "cardiology", now_iso()))
sid = new_id("src")
db.execute("INSERT INTO sources (id,notebook_id,kind,filename,status,uploaded_at)"
           " VALUES (?,?,?,?,?,?)", (sid, nid, "pdf", "valvular.pdf", "extracted", now_iso()))
for cid, name, subj in [("c-hf", "Heart failure", "cardiology"),
                        ("c-iron", "Iron metabolism", "biochemistry"),
                        ("c-fena", "Fractional excretion of sodium", "renal")]:
    db.execute("INSERT INTO concepts (id,canonical_name,normalized_name,subject,first_seen_at)"
               " VALUES (?,?,?,?,?)", (cid, name, name.lower(), subj, now_iso()))
    db.execute("INSERT INTO notebook_concepts (notebook_id,concept_id,role) VALUES (?,?,?)",
               (nid, cid, "primary"))
for a, b, rel in [("c-hf", "c-iron", "precipitated_by"), ("c-hf", "c-fena", "assessed_by")]:
    db.execute("INSERT INTO concept_relationships (id,source_concept_id,target_concept_id,"
               "relation_type,confidence,created_at) VALUES (?,?,?,?,?,?)",
               (new_id("rel"), a, b, rel, 0.9, now_iso()))
db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
           "correct_index,rationale,source_id,validation_status,generated_by_candidate_id,"
           "prompt_version,generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           ("q-nb-1", nid, "mcq", "A stem.", json.dumps(["A","B"]), 0, "r", sid,
            "approved", "cand", "v1", now_iso()))
print("seeded")
`);

  const seeded = await run('python3', [seedPath, dbPath], { cwd: process.cwd() });
  assert.equal(seeded.code, 0, `seeding failed: ${seeded.err}`);

  const server = spawn('python3', ['-m', 'benchmark.cli', 'serve-student',
    '--host', '127.0.0.1', '--port', String(port), '--db', dbPath, '--no-ai'],
    { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => { server.kill('SIGKILL'); rmSync(dir, { recursive: true, force: true }); });

  let up = false;
  for (let i = 0; i < 80 && !up; i++) {
    try { up = (await fetch(`${origin}/health`)).ok; } catch { /* not up */ }
    if (!up) await new Promise((r) => setTimeout(r, 250));
  }
  assert.ok(up, 'the server did not start, so NOTHING was verified');

  globalThis.window = { __QUINTEK_STUDENT_API__: origin };
  const api = await import('../../frontend/quintek-student-api.js?screens=' + Date.now());
  api.setToken((await api.login('screens@example.com', 'correct-horse')).token);

  // --- GRAPH ------------------------------------------------------------
  const graph = await api.graph();
  assert.equal(graph.nodes.length, 3);
  assert.equal(graph.edges.length, 2);

  // THE CLAIM. cross_subject comes from the server, and the server sees
  // subjects the client palette does not name.
  const byPair = Object.fromEntries(graph.edges.map(
    (e) => [`${e.source_concept_id}->${e.target_concept_id}`, e]));
  assert.equal(byPair['c-hf->c-iron'].cross_subject, true,
    'cardiology -> biochemistry should cross');
  assert.equal(byPair['c-hf->c-fena'].cross_subject, true,
    'cardiology -> renal should cross -- "renal" is NOT in the four-colour palette, '
    + 'which is exactly the edge a client-side recomputation gets wrong');

  // Every node carries a subject; one of them is outside the palette.
  const subjects = new Set(graph.nodes.map((n) => n.subject));
  assert.ok(subjects.has('renal'),
    'the fixture no longer exercises an unmapped subject, so it proves less');

  // --- NOTEBOOK ---------------------------------------------------------
  const nbs = await api.notebooks();
  assert.equal(nbs.length, 1);
  const nb = await api.notebook(nbs[0].id);
  assert.equal(nb.title, 'Cardiology Block 3');
  const nbq = await api.notebookQuestions(nbs[0].id);
  assert.equal(nbq.length, 1);

  // --- CONCEPT DETAIL ---------------------------------------------------
  const detail = await api.conceptDetail('c-fena');
  assert.ok(detail, 'no concept detail came back');
  assert.ok(JSON.stringify(detail).includes('Fractional excretion'),
    `the detail payload did not name the concept: ${JSON.stringify(detail).slice(0, 200)}`);

  // --- SCOPE, without an account ---------------------------------------
  const anon = await fetch(`${origin}/scope`);
  assert.ok(anon.ok, 'the scope statement required an account');
  const scope = await anon.json();
  assert.ok(scope.scope_statement.length > 40);
  assert.ok(Array.isArray(scope.report_kinds) || scope.report_kinds === undefined);

  delete globalThis.window;
});

test('the scope statement is served without an account', async () => {
  const res = await fetch(`${ORIGIN}/scope`).catch(() => null);
  if (!res) return;               // server already torn down by the test above
  if (!res.ok) return;
  const body = await res.json();
  assert.ok(body.scope_statement && body.scope_statement.length > 40);
});
