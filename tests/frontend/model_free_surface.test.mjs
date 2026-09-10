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
  'POST /attempts', 'GET /settings/notifications', 'PUT /settings/notifications',
]);
const SERVED_PREFIXES = [
  'GET /gaps/', 'POST /gaps/', 'GET /concepts/', 'GET /questions/',
  'POST /revision/sessions/',
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
  await api.notificationPrefs();
  await api.setNotificationPrefs({ trigger_time: '19:00' });

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

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
