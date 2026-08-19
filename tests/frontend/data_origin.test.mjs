// The one rule both frontend API modules must obey:
//
//   Fixtures are used ONLY when no backend was ever configured.
//
// A configured-but-failing backend is an outage and must render as one. The
// modules used to fall back to fixtures on any failure, which meant the
// student transparency screen named a real vendor and asserted a real score
// during an outage, and the admin console offered fixture gate results to an
// admin deciding what to promote.
//
// Run directly (`node tests/frontend/data_origin.test.mjs`) or via pytest,
// which shells out to node and skips when node is absent.

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

let failures = 0;
const check = (name, cond) => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + name);
  if (!cond) failures++;
};

// Each import needs a fresh module instance, because the fetch happens once at
// module scope. A cache-busting query gives us that.
const load = async (file, setup) => {
  delete globalThis.window;
  delete globalThis.fetch;
  setup();
  return import('file://' + resolve(ROOT, 'frontend', file) + '?v=' + Math.random());
};

const EVAL = 'quintek-eval-api.js';
const REPORT = 'quintek-report-api.js';

// ---------------------------------------------------------------------------
// quintek-eval-api.js -- feeds the learner-facing Quintek AI Benchmark screen
// ---------------------------------------------------------------------------

let m = await load(EVAL, () => { globalThis.window = {}; });
check('eval: no backend configured -> fixtures render (standalone design file)',
  m.candidates.length > 0 && m.state === 'ok');
check('eval: no backend configured -> not reported as an outage',
  m.isOutage === false && m.loadError === null);

m = await load(EVAL, () => {
  globalThis.window = { __QUINTEK_API__: 'http://127.0.0.1:9' };
  globalThis.fetch = async () => { throw new Error('ECONNREFUSED'); };
});
check('eval: unreachable backend -> state is error', m.state === 'error');
check('eval: unreachable backend -> no candidates invented', m.candidates.length === 0);
check('eval: unreachable backend -> no overview invented',
  !m.overview || Object.keys(m.overview).length === 0);
check('eval: unreachable backend -> no routing invented', m.routing.length === 0);
check('eval: unreachable backend -> no tracks invented', m.tracks.length === 0);
check('eval: unreachable backend -> no fixture vendor names leak',
  !JSON.stringify(m.candidates).toLowerCase().includes('claude'));
check('eval: unreachable backend -> outage flagged and explained',
  m.isOutage === true && /unreachable/.test(m.loadError || ''));

m = await load(EVAL, () => {
  globalThis.window = { __QUINTEK_API__: 'http://live' };
  globalThis.fetch = async () => ({ ok: false, status: 500, statusText: 'Server Error' });
});
check('eval: HTTP 500 is an outage, not a reason to invent data',
  m.state === 'error' && m.candidates.length === 0);

m = await load(EVAL, () => {
  globalThis.window = { __QUINTEK_API__: 'http://live' };
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({
      state: 'ok', candidates: [{ candidateId: 'x', model: 'real/model' }],
      routing: [], tracks: [], overview: { overallScore: 1 }, history: [],
      failures: [], cases: [], runs: [], trackDetail: {}, overallByCandidate: {},
    }),
  });
});
check('eval: healthy backend -> live data used',
  m.candidates.length === 1 && m.candidates[0].model === 'real/model');
check('eval: healthy backend -> isLive true and no outage',
  m.isLive === true && m.isOutage === false);

// An empty archive is a legitimate live answer, and must not be topped up.
m = await load(EVAL, () => {
  globalThis.window = { __QUINTEK_API__: 'http://live' };
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({
      state: 'empty', candidates: [], routing: [], tracks: [], overview: null,
      history: [], failures: [], cases: [], runs: [], trackDetail: {},
      overallByCandidate: {},
    }),
  });
});
check('eval: a live but empty archive stays empty', m.candidates.length === 0);
check('eval: a live but empty archive is not an outage', m.isOutage === false);

// ---------------------------------------------------------------------------
// quintek-report-api.js -- feeds the admin console, where models get promoted
// ---------------------------------------------------------------------------

m = await load(REPORT, () => { globalThis.window = {}; });
const unconfiguredRuns = await m.listRuns();
check('report: no backend configured -> fixtures render', unconfiguredRuns.length > 0);
check('report: no backend configured -> not an outage', m.isOutage() === false);

m = await load(REPORT, () => {
  globalThis.window = { __QUINTEK_API__: 'http://127.0.0.1:9' };
  globalThis.fetch = async () => { throw new Error('ECONNREFUSED'); };
});
let threw = false;
try { await m.listRuns(); } catch (e) { threw = e.name === 'BackendUnavailable'; }
check('report: unreachable backend -> listRuns reports the outage', threw);

threw = false;
try { await m.getRun('any-run'); } catch (e) { threw = e.name === 'BackendUnavailable'; }
check('report: unreachable backend -> getRun reports the outage', threw);
check('report: unreachable backend -> flagged as an outage', m.isOutage() === true);

// The sharpest case: asking for one run must never return another one's data.
m = await load(REPORT, () => { globalThis.window = {}; });
let substituted = false;
try {
  const run = await m.getRun('a-run-id-that-does-not-exist');
  substituted = run.run_id !== 'a-run-id-that-does-not-exist';
} catch (e) {
  substituted = false;
}
check('report: an unknown run id never returns a different run', substituted === false);

console.log(failures ? `\n${failures} FAILED` : '\nall passed');
process.exit(failures ? 1 : 0);
