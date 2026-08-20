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

// ---------------------------------------------------------------------------
// quintek-student-api.js -- feeds the wired "write questions" interaction
// ---------------------------------------------------------------------------

const STUDENT = 'quintek-student-api.js';

m = await load(STUDENT, () => { globalThis.window = {}; });
check('student: no backend configured -> reports itself unconfigured', m.configured === false);
let studentThrew = null;
try { await m.notebooks(); } catch (e) { studentThrew = e; }
check('student: an unconfigured call throws rather than returning demo data',
  studentThrew !== null && studentThrew.name === 'BackendError');

m = await load(STUDENT, () => {
  globalThis.window = { __QUINTEK_STUDENT_API__: 'http://127.0.0.1:9' };
  globalThis.fetch = async () => { throw new Error('ECONNREFUSED'); };
});
check('student: a configured backend is reported as configured', m.configured === true);
studentThrew = null;
try { await m.notebooks(); } catch (e) { studentThrew = e; }
check('student: an unreachable backend throws an outage, not demo questions',
  studentThrew !== null && /could not be reached/.test(studentThrew.message));

m = await load(STUDENT, () => {
  globalThis.window = { __QUINTEK_STUDENT_API__: 'http://live' };
  globalThis.fetch = async (url, init) => {
    if (String(url).includes('/notebooks/') && init && init.method === 'POST') {
      return { ok: true, status: 200, text: async () => JSON.stringify(
        { question_ids: ['q1'], count: 1,
          validation: { approved: 1, flagged: 0, skipped: 0, failed: 0 } }) };
    }
    if (String(url).includes('/questions')) {
      return { ok: true, status: 200, text: async () => JSON.stringify({ questions: [
        { id: 'q1', stem: 'A real stem from a real model', options: ['a', 'b'],
          family: 'Clinical vignette', validation_status: 'approved',
          generated_by_candidate_id: 'nvidia:meta/llama-3.1-70b-instruct',
          source_id: 's1', chunk_id: 'c1' }] }) };
    }
    return { ok: true, status: 200, text: async () => '{}' };
  };
});
const generated = await m.generateQuestions('nb1', 1, {});
check('student: generateQuestions returns the engine\'s real questions',
  generated.count === 1 && generated.questions[0].stem === 'A real stem from a real model');
check('student: provenance travels with each question',
  generated.questions[0].validationStatus === 'approved' &&
  /llama/.test(generated.questions[0].generatedBy) &&
  generated.questions[0].chunkId === 'c1');
check('student: the validation summary is carried through',
  generated.validation && generated.validation.approved === 1);

m = await load(STUDENT, () => {
  globalThis.window = { __QUINTEK_STUDENT_API__: 'http://live' };
  globalThis.fetch = async () => ({
    ok: false, status: 503, statusText: 'Service Unavailable',
    text: async () => JSON.stringify({ error: 'no AI services are configured' }),
  });
});
studentThrew = null;
try { await m.generateQuestions('nb1', 1, {}); } catch (e) { studentThrew = e; }
check('student: a 503 from the engine surfaces the engine\'s own reason',
  studentThrew !== null && /no AI services are configured/.test(studentThrew.message));

// ---------------------------------------------------------------------------
// quintek-billing-api.js -- plan, usage and subscription
// ---------------------------------------------------------------------------

const BILLING = 'quintek-billing-api.js';

m = await load(BILLING, () => { globalThis.window = {}; });
check('billing: no backend configured -> reports itself unconfigured',
  m.configured === false);
let billingThrew = null;
try { await m.usage(); } catch (e) { billingThrew = e; }
check('billing: an unconfigured call throws rather than inventing an allowance',
  billingThrew !== null && billingThrew.name === 'BillingError');

m = await load(BILLING, () => {
  globalThis.window = { __QUINTEK_STUDENT_API__: 'http://127.0.0.1:9' };
  globalThis.fetch = async () => { throw new Error('ECONNREFUSED'); };
});
billingThrew = null;
try { await m.usage(); } catch (e) { billingThrew = e; }
check('billing: an unreachable backend is an outage, not a fabricated balance',
  billingThrew !== null && /could not be reached/.test(billingThrew.message));

// Billing lives under its own prefix on the shared origin. `/me` is the
// learner's profile and `/me/usage` is billing; a router that serves both from
// the same namespace is one rename away from a profile edit touching a
// subscription.
let requestedUrl = null;
m = await load(BILLING, () => {
  globalThis.window = { __QUINTEK_STUDENT_API__: 'http://live' };
  globalThis.fetch = async (url) => {
    requestedUrl = url;
    return { ok: true, status: 200, text: async () => '{}' };
  };
});
await m.usage();
check('billing: requests go to the /billing prefix, not the bare origin',
  requestedUrl === 'http://live/billing/me/usage');

m = await load(BILLING, () => {
  globalThis.window = { __QUINTEK_STUDENT_API__: 'http://live/' };
  globalThis.fetch = async (url) => {
    requestedUrl = url;
    return { ok: true, status: 200, text: async () => '{}' };
  };
});
await m.pricing();
check('billing: a trailing slash on the configured origin does not double up',
  requestedUrl === 'http://live/billing/pricing');

// ---------------------------------------------------------------------------
// The 500-question batch, and what happens when 500 is not available.
// ---------------------------------------------------------------------------
m = await load(BILLING, () => { globalThis.window = {}; });

const partial = m.capacityOffer({
  allowed: true, partial: true, granted: 173, requested: 500,
  reason: 'You can generate 173 questions today. Your plan allows up to 500 per session, '
    + 'but your remaining daily allowance is 173.',
  actions: ['generate_available', 'upgrade', 'view_usage'],
  availability: { binding_constraint: 'daily', available_now: 173, daily_remaining: 173,
    monthly_remaining: 3160, session_limit: 500 },
});

check('capacity: the granted number is the server\'s, copied',
  partial.granted === 173 && partial.requested === 500 && partial.shortfall === 327);
check('capacity: the reason is the server\'s sentence, verbatim',
  partial.reason.indexOf('remaining daily allowance is 173') > 0);
check('capacity: a partial offer can be taken now',
  partial.canGenerateNow === true && partial.generateLabel === 'Generate 173 now');
check('capacity: the headline states both numbers',
  partial.headline === 'You asked for 500. You can generate 173 now.');
check('capacity: waiting is only suggested when the DAILY cap bound',
  partial.waitHelps === true);

const monthlyExhausted = m.capacityOffer({
  allowed: false, partial: false, granted: 0, requested: 500,
  reason: 'You have used your monthly allowance.',
  actions: ['upgrade', 'view_usage'],
  availability: { binding_constraint: 'monthly', available_now: 0, daily_remaining: 300,
    monthly_remaining: 0, session_limit: 500 },
});
check('capacity: out of monthly allowance is not told to come back tomorrow',
  monthlyExhausted.waitHelps === false);
check('capacity: nothing available offers no generate button',
  monthlyExhausted.canGenerateNow === false && monthlyExhausted.granted === 0);
check('capacity: a refusal still offers the upgrade path',
  monthlyExhausted.canUpgrade === true);

const full = m.capacityOffer({
  allowed: true, partial: false, granted: 50, requested: 50,
  reason: 'within your current allowance', actions: [],
  availability: { binding_constraint: 'monthly', available_now: 3160 },
});
check('capacity: a full grant is not reported as partial',
  full.partial === false && full.shortfall === 0);

check('capacity: a missing decision does not throw or invent an allowance',
  (() => { const o = m.capacityOffer(undefined);
    return o.granted === 0 && o.canGenerateNow === false && o.reason === ''; })());

// The helper must never improve on the server's answer.
const stingy = m.capacityOffer({
  allowed: true, partial: true, granted: 1, requested: 500,
  reason: 'You can generate 1 question today.', actions: ['generate_available'],
  availability: { binding_constraint: 'daily', available_now: 1, session_limit: 500 },
});
check('capacity: a grant of one is reported as one, not rounded up to something friendlier',
  stingy.granted === 1 && stingy.generateLabel === 'Generate 1 now');

// The formatting helpers must not invent numbers of their own.
m = await load(BILLING, () => { globalThis.window = {}; });
const bars = m.usageBars({
  this_month: { used: 1840, allowance: 5000, remaining: 3160 },
  today: { used: 127, limit: 300, remaining: 173 },
  rollover: 1200, session_limit: 500, available_now: 173,
  binding_constraint: 'daily', monthly_remaining: 3160, daily_remaining: 173,
});
check('billing: usage bars render the server\'s figures verbatim',
  bars.monthLabel === '1,840 / 5,000 questions' &&
  bars.todayLabel === '127 / 300 questions' &&
  bars.availableNow === '173');
check('billing: available-now follows the DAILY figure, not the monthly one',
  bars.availableNow === '173' && bars.monthRemaining === '3,160');
check('billing: the monthly/daily confusion is called out in words',
  /daily cap applies today/.test(bars.bindingNote));

const empty = m.usageBars({});
check('billing: a missing payload renders zeros, not NaN',
  empty.monthPercent === 0 && !/NaN/.test(JSON.stringify(empty)));

const cards = m.planCards({ families: [
  { family: 'free', name: 'Free', intervals: { none: { price_display: '₹0' } },
    monthly_question_allowance: 100, daily_question_limit: 20,
    session_question_limit: 50 },
  { family: 'pro', name: 'Pro',
    intervals: { monthly: { plan_id: 'pro_monthly_v1', price_display: '₹499' },
                 annual: { plan_id: 'pro_annual_v1', price_display: '₹4,990',
                           monthly_equivalent_display: '₹415' } },
    monthly_question_allowance: 5000, daily_question_limit: 300,
    session_question_limit: 500,
    annual_saving: { label: 'Save ~2 months' } },
] }, 'annual');
check('billing: the free plan is not offered as an upgrade card',
  cards.every((c) => c.family !== 'free'));
// ---------------------------------------------------------------------------
// Upgrade and downgrade are different operations with different timing.
// ---------------------------------------------------------------------------
const PRICING = { families: [
  { family: 'free', name: 'Free', monthly_question_allowance: 100,
    daily_question_limit: 20, session_question_limit: 50,
    intervals: { none: { plan_id: 'f', price_minor: 0, price_display: '₹0' } } },
  { family: 'student', name: 'Student', monthly_question_allowance: 2500,
    daily_question_limit: 150, session_question_limit: 500,
    intervals: { monthly: { plan_id: 's', price_minor: 29900, price_display: '₹299',
      monthly_equivalent_minor: 29900, monthly_equivalent_display: '₹299' } } },
  { family: 'pro', name: 'Pro', monthly_question_allowance: 5000,
    daily_question_limit: 300, session_question_limit: 500,
    intervals: { monthly: { plan_id: 'p', price_minor: 49900, price_display: '₹499',
      monthly_equivalent_minor: 49900, monthly_equivalent_display: '₹499' } } },
  { family: 'power', name: 'Power', monthly_question_allowance: 10000,
    daily_question_limit: 500, session_question_limit: 500,
    intervals: { monthly: { plan_id: 'w', price_minor: 79900, price_display: '₹799',
      monthly_equivalent_minor: 79900, monthly_equivalent_display: '₹799' } } },
] };

const onPro = m.planCards(PRICING, 'monthly', 'pro');
const byFamily = Object.fromEntries(onPro.map((c) => [c.family, c]));

check('plans: a dearer plan is an upgrade',
  byFamily.power.direction === 'upgrade' && byFamily.power.actionLabel === 'Upgrade');
check('plans: a cheaper plan is NOT labelled upgrade',
  byFamily.student.direction === 'downgrade' && byFamily.student.actionLabel !== 'Upgrade');
check('plans: the current plan is marked and not sold again',
  byFamily.pro.isCurrent === true && byFamily.pro.direction === 'current');
check('plans: the timing is stated on the card, before the tap',
  byFamily.power.actionNote.indexOf('immediately') >= 0
  && byFamily.student.actionNote.indexOf('next renewal') >= 0);

const anonymous = m.planCards(PRICING, 'monthly', '');
check('plans: with no current plan every card is a plain choice',
  anonymous.every((c) => c.direction === 'choose' && c.actionLabel === 'Choose'));
check('plans: the free plan is never a card, whatever the current plan',
  onPro.every((c) => c.family !== 'free'));

/* Annual against monthly must be compared per month, or every annual plan
 * looks like an upgrade on its sticker price. */
const ANNUAL = { families: [
  { family: 'student', name: 'Student', monthly_question_allowance: 2500,
    daily_question_limit: 150, session_question_limit: 500,
    intervals: { annual: { plan_id: 'sa', price_minor: 299000, price_display: '₹2,990',
      monthly_equivalent_minor: 24916, monthly_equivalent_display: '₹249.16' } } },
  { family: 'pro', name: 'Pro', monthly_question_allowance: 5000,
    daily_question_limit: 300, session_question_limit: 500,
    intervals: { annual: { plan_id: 'pa', price_minor: 499000, price_display: '₹4,990',
      monthly_equivalent_minor: 41583, monthly_equivalent_display: '₹415.83' } } },
] };
const annualCards = m.planCards(ANNUAL, 'annual', 'pro');
check('plans: annual cards rank on the per-month figure, not the sticker price',
  annualCards.find((c) => c.family === 'student').direction === 'downgrade');

check('billing: an annual card shows the per-month equivalent for comparison',
  cards[0].price === '₹4,990' && cards[0].monthlyEquivalent === '₹415/mo' &&
  cards[0].saving === 'Save ~2 months');

console.log(failures ? `\n${failures} FAILED` : '\nall passed');
process.exit(failures ? 1 : 0);
