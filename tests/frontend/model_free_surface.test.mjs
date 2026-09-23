// Every screen that needs no inference must work against a live backend.
//
// The exit condition for this phase is that the app is fully usable for
// everything that does not require a model call. That splits into three
// claims, and this file tests all three:
//
//   1. the calls EXIST and hit the endpoints the server actually serves
//   2. an outage is reported as an outage, never as empty data
//   3. "no data yet" and "not measured" stay distinguishable
//
// (3) is the one worth spelling out. `mastery_pct` is null until a learner has
// answered something, and 0 once they have answered and got everything wrong.
// A client that coerced null to 0 would render "0% mastery" to someone who has
// not started, which is a claim about them that nothing measured.
//
// Run directly (`node tests/frontend/model_free_surface.test.mjs`) or via
// pytest, which shells out to node and skips when node is absent.

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

let failures = 0;
const check = (name, cond) => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + name);
  if (!cond) failures++;
};

// The endpoints student/api.py actually routes. Kept here so a client call to
// a path the server does not serve fails loudly rather than at runtime.
const SERVED = new Set([
  'GET /me', 'GET /progress', 'GET /gaps', 'GET /concepts', 'GET /graph',
  'GET /revision/dashboard', 'GET /revision/next', 'POST /revision/sessions',
  'POST /attempts', 'GET /reminders', 'POST /reminders',
  'GET /reports', 'GET /scope',
]);
const SERVED_PREFIXES = [
  'GET /gaps/', 'POST /gaps/', 'GET /concepts/', 'GET /questions/',
  'POST /revision/sessions/', 'GET /notebooks/', 'GET /sources/',
  'POST /questions/', 'PUT /reminders/', 'DELETE /reminders/',
];

const load = async (setup) => {
  delete globalThis.window;
  delete globalThis.fetch;
  setup();
  return import(resolve(ROOT, 'frontend/quintek-student-api.js') + '?v=' + Math.random());
};

const backend = (handler) => {
  const seen = [];
  globalThis.window = { __QUINTEK_STUDENT_API__: 'http://backend.test' };
  globalThis.fetch = async (url, init) => {
    const path = String(url).replace('http://backend.test', '');
    const method = (init && init.method) || 'GET';
    seen.push(method + ' ' + path);
    return handler(method, path);
  };
  return seen;
};

const ok = (payload) => ({
  ok: true, status: 200, statusText: 'OK',
  text: async () => JSON.stringify(payload),
});

// ---------------------------------------------------------------------------
// 1. the calls reach endpoints the server serves
// ---------------------------------------------------------------------------

{
  let seen;
  const api = await load(() => {
    seen = backend(() => ok({
      gaps: [], concepts: [], questions: [], evidence: [],
      nodes: [], edges: [], colour_counts: {},
    }));
  });

  await api.me();
  await api.progress();
  await api.gaps();
  await api.gaps({ colour: 'RED', includeResolved: true });
  await api.gapEvidence('gap_1');
  await api.gapQuestions('gap_1');
  await api.resolveGap('gap_1');
  await api.revisionDashboard();
  await api.concepts();
  await api.conceptDetail('cpt_1');
  await api.graph();
  await api.graph('nb_1');
  await api.question('q_1');
  await api.startSession(5, 'adaptive');
  await api.nextQuestion('ses_1');
  await api.recordAttempt('q_1', 2, 'RED', { sessionId: 'ses_1', gaps: ['x'] });
  await api.completeSession('ses_1');
  await api.listReminders();
  await api.createReminder({ label: 'revise patho', localDate: '2099-01-01',
                             localTime: '19:00', timezone: 'Asia/Kolkata' });
  await api.updateReminder('rem_1', { localTime: '19:30' });
  await api.cancelReminder('rem_1');

  const unserved = seen.filter((call) => {
    const bare = call.split('?')[0];
    if (SERVED.has(bare)) return false;
    return !SERVED_PREFIXES.some((p) => bare.startsWith(p));
  });
  check('every call targets an endpoint student/api.py serves',
        unserved.length === 0 || (console.log('   unserved:', unserved), false));

  check('a gap id is url-encoded into the path',
        seen.includes('GET /gaps/gap_1'));
  check('query filters are sent, not dropped',
        seen.some((c) => c.includes('colour=RED') && c.includes('include_resolved=1')));
  check('the session id travels as a query parameter on /revision/next',
        seen.includes('GET /revision/next?session=ses_1'));
  check('completing a session posts to its own id',
        seen.includes('POST /revision/sessions/ses_1/complete'));
}

// ---------------------------------------------------------------------------
// 2. an outage is an outage
// ---------------------------------------------------------------------------

{
  const api = await load(() => {
    backend(() => { throw new Error('ECONNREFUSED'); });
  });
  for (const [name, fn] of [
    ['progress', () => api.progress()],
    ['gaps', () => api.gaps()],
    ['revisionDashboard', () => api.revisionDashboard()],
    ['concepts', () => api.concepts()],
    ['graph', () => api.graph()],
  ]) {
    let threw = false;
    try { await fn(); } catch (e) { threw = e.name === 'BackendError'; }
    check(`${name} reports an unreachable backend instead of returning empty`, threw);
  }
}

{
  const api = await load(() => {
    backend(() => ({
      ok: false, status: 500, statusText: 'Server Error',
      text: async () => JSON.stringify({ error: 'the database is unavailable' }),
    }));
  });
  let message = '';
  try { await api.progress(); } catch (e) { message = e.message; }
  check('a 500 surfaces the server\'s own reason',
        message === 'the database is unavailable');
}

// ---------------------------------------------------------------------------
// 3. "none yet" and "not measured" stay different
// ---------------------------------------------------------------------------

{
  const api = await load(() => {
    backend(() => ok({
      colour_counts: { RED: 0, ORANGE: 0, GREEN: 0 },
      concepts_tracked: 0, mastery_pct: null,
      attempts_total: 0, attempts_correct: 0, accuracy_pct: null,
    }));
  });
  const p = await api.progress();
  check('an unmeasured mastery stays null and is not coerced to zero',
        p.mastery_pct === null);
  check('an unmeasured accuracy stays null', p.accuracy_pct === null);
  check('a real zero stays zero', p.attempts_total === 0);
}

{
  const api = await load(() => { backend(() => ok({})); });
  check('an absent gaps list reads as empty, not undefined',
        Array.isArray(await api.gaps()));
  check('an absent concepts list reads as empty',
        Array.isArray(await api.concepts()));
}

// ---------------------------------------------------------------------------
// with no backend configured, nothing pretends
// ---------------------------------------------------------------------------

{
  const api = await load(() => { delete globalThis.window; });
  check('configured is false with no backend', api.configured === false);
  let threw = false;
  try { await api.progress(); } catch (e) { threw = e.name === 'BackendError'; }
  check('an unconfigured backend refuses rather than inventing data', threw);
}


// ---------------------------------------------------------------------------
// the two screens that needed a per-item fetch on entry
//
// Concept detail and notebook view rendered from constants. They were left
// that way deliberately -- the bulk loaders return list rows, and padding
// those out with placeholders would show a learner numbers nobody measured --
// so the fix is a fetch on screen entry, not a richer list payload.
// ---------------------------------------------------------------------------

{
  let seen;
  const api = await load(() => {
    seen = backend((method, path) => {
      if (path.endsWith('/questions')) return ok({ questions: [{ id: 'q1' }] });
      if (path.startsWith('/notebooks/')) {
        return ok({ id: 'nb1', title: 'Renal', sources: [], concepts: [] });
      }
      if (path.startsWith('/sources/')) return ok({ status: 'extracted', chunks: 4 });
      if (path.startsWith('/concepts/')) return ok({ id: 'c1', canonical_name: 'AKI' });
      if (path === '/scope') return ok({ scope_statement: 'a revision aid', report_kinds: [] });
      if (path === '/reports') return ok({ reports: [] });
      return ok({});
    });
  });

  await api.notebook('nb1');
  await api.notebookQuestions('nb1');
  await api.sourceProgress('src1');
  await api.conceptDetail('c1');
  await api.reportQuestion('q1', 'factually_wrong', 'the key is wrong');
  await api.myReports();
  await api.scope();

  const served = (call) => SERVED.has(call) ||
    SERVED_PREFIXES.some((p) => call.startsWith(p));
  check('every screen-entry call hits a route the server serves',
        seen.every(served));
  check('notebook view fetches the notebook itself',
        seen.some((c) => c === 'GET /notebooks/nb1'));
  check('notebook view fetches its questions separately',
        seen.some((c) => c === 'GET /notebooks/nb1/questions'));
  check('concept detail fetches the concept itself',
        seen.some((c) => c === 'GET /concepts/c1'));
  check('a question can be reported from the client',
        seen.some((c) => c === 'POST /questions/q1/reports'));
  check('the scope statement is fetched rather than hard-coded',
        seen.some((c) => c === 'GET /scope'));
}

// ---------------------------------------------------------------------------
// four states, and never a fifth
//
// The failure worth testing for is "error rendered as empty", which tells a
// learner their notebook is empty when the truth is nobody could reach the
// server. There is no partial render: a screen is loading, failed, empty or
// ready.
// ---------------------------------------------------------------------------

{
  const api = await load(() => { backend(() => ok({})); });
  const s = api.screenState;

  check('an error is an error even when the data is empty',
        s({ error: new BackendErrorish(), data: [] }) === 'error');
  check('an error is an error even when data arrived',
        s({ error: new BackendErrorish(), data: [1, 2] }) === 'error');
  check('no data yet is loading, not empty',
        s({ data: null }) === 'loading');
  check('undefined is loading, not empty',
        s({ data: undefined }) === 'loading');
  check('an empty list is empty', s({ data: [] }) === 'empty');
  check('a populated list is ready', s({ data: [1] }) === 'ready');
  check('emptiness is the screen\'s own definition',
        s({ data: { sources: [] }, isEmpty: (d) => d.sources.length === 0 }) === 'empty');
  check('a notebook with sources is ready',
        s({ data: { sources: [1] }, isEmpty: (d) => d.sources.length === 0 }) === 'ready');

  const states = new Set(['loading', 'error', 'empty', 'ready']);
  const sampled = [
    s({ loading: true }), s({ error: 1 }), s({ data: [] }), s({ data: [1] }),
    s({ data: {} }), s({ loading: true, data: [1] }),
  ];
  check('there is no fifth state', sampled.every((x) => states.has(x)));
}

function BackendErrorish() { this.name = 'BackendError'; }

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
