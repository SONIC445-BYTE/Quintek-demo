/* Client for the billing backend (billing/api.py).
 *
 * Same origin rule as the other clients, and here it matters most:
 *
 *   THE FRONTEND NEVER DETERMINES ENTITLEMENT.
 *
 * Everything below DISPLAYS what the backend returned. No function in this
 * file computes a remaining count, decides whether a user may generate, or
 * derives an allowance from a plan name. When the app wants to generate, it
 * asks the server, and the server's answer is the answer.
 *
 * With no `window.__QUINTEK_STUDENT_API__` the module reports itself
 * unconfigured and the screens fall back to their design-file constants --
 * correct for a design file opened from disk. A CONFIGURED backend that fails
 * is an outage and is reported as one; it never degrades into showing an
 * invented allowance, because a user told they have 3,000 questions left when
 * the server thinks otherwise will hit a refusal they cannot explain.
 */

const BASE = (typeof window !== 'undefined' && window.__QUINTEK_STUDENT_API__) || null;

export const configured = BASE !== null;

let token = (typeof window !== 'undefined' && window.__QUINTEK_STUDENT_TOKEN__) || null;
export function setToken(value) { token = value; }

export class BillingError extends Error {
  constructor(status, detail, payload) {
    super(detail || ('HTTP ' + status));
    this.name = 'BillingError';
    this.status = status;
    this.payload = payload || {};
  }
}

async function call(method, path, body) {
  if (!BASE) throw new BillingError(0, 'no billing backend is configured');
  const headers = { accept: 'application/json' };
  if (body !== undefined) headers['content-type'] = 'application/json';
  if (token) headers.authorization = 'Bearer ' + token;

  let res;
  try {
    res = await fetch(BASE + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new BillingError(0, 'the billing service could not be reached (' +
      (e && e.message ? e.message : e) + ')');
  }
  const text = await res.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch (e) { payload = { error: text }; }
  if (!res.ok) throw new BillingError(res.status, payload.error || res.statusText, payload);
  return payload;
}

/* ---- public ---- */

export async function pricing() {
  return call('GET', '/pricing');
}

/* ---- the usage dashboard ---- */

export async function usage() {
  return call('GET', '/me/usage');
}

export async function entitlements() {
  return call('GET', '/me/entitlements');
}

/* Ask whether a request would be allowed, WITHOUT consuming anything.
 *
 * This is what the 500-question screen calls before showing its confirmation.
 * The reply carries `granted`, which may be lower than what was asked for,
 * and `reason`, which names the limit that bound -- so the partial-capacity
 * message is the server's words, not a sentence the client assembled from
 * numbers it guessed at. */
export async function check(questions) {
  return call('POST', '/me/usage/check', { questions });
}

/* Reserve capacity before generation starts. Only `questions` is sent: any
 * remaining count this client believes in is irrelevant and the server
 * ignores it. */
export async function reserve(questions, options) {
  const opts = options || {};
  return call('POST', '/me/usage/reserve', {
    questions,
    question_type: opts.questionType || 'mcq',
    allow_partial: opts.allowPartial !== false,
  });
}

export async function commitReservation(reservationId, actualUnits) {
  return call('POST', '/me/usage/reservations/' + reservationId + '/commit',
    actualUnits === undefined ? {} : { actual_units: actualUnits });
}

export async function releaseReservation(reservationId, reason) {
  return call('POST', '/me/usage/reservations/' + reservationId + '/release',
    { reason: reason || '' });
}

/* ---- the Billing screen, reached from More ---- */

export async function subscription() {
  return call('GET', '/me/subscription');
}

export async function beginCheckout(planId) {
  return call('POST', '/me/subscription/checkout', { plan_id: planId });
}

export async function cancel() {
  return call('POST', '/me/subscription/cancel');
}

export async function downgrade(planId) {
  return call('POST', '/me/subscription/downgrade', { plan_id: planId });
}

/* ---- presentation helpers ----
 *
 * Formatting only. Each takes numbers the server produced and returns strings;
 * none of them decides anything. */

export function usageBars(payload) {
  const month = payload.this_month || {};
  const today = payload.today || {};
  const pct = (used, total) => (!total ? 0 : Math.min(100, Math.round((used / total) * 100)));
  return {
    monthLabel: (month.used || 0).toLocaleString() + ' / ' +
                (month.allowance || 0).toLocaleString() + ' questions',
    monthPercent: pct(month.used, month.allowance),
    monthRemaining: (month.remaining || 0).toLocaleString(),
    todayLabel: (today.used || 0).toLocaleString() + ' / ' +
                (today.limit || 0).toLocaleString() + ' questions',
    todayPercent: pct(today.used, today.limit),
    todayRemaining: (today.remaining || 0).toLocaleString(),
    rollover: payload.rollover ? '+' + payload.rollover.toLocaleString() + ' questions' : '',
    sessionLimit: (payload.session_limit || 0).toLocaleString(),
    availableNow: (payload.available_now || 0).toLocaleString(),
    /* The one sentence that keeps monthly and daily from being confused. */
    bindingNote: payload.binding_constraint === 'daily' && payload.monthly_remaining >
      payload.daily_remaining
      ? 'Your monthly allowance has more left, but the daily cap applies today.'
      : '',
  };
}

export function planCards(pricingPayload, interval) {
  const wanted = interval === 'annual' ? 'annual' : 'monthly';
  return (pricingPayload.families || [])
    .filter((f) => f.family !== 'free')
    .map((f) => {
      const chosen = f.intervals[wanted] || f.intervals.monthly || {};
      return {
        family: f.family,
        name: f.name.toUpperCase(),
        planId: chosen.plan_id,
        price: chosen.price_display,
        /* An annual card shows the per-month equivalent so the toggle
         * compares like with like rather than 4,990 against 499. */
        priceSuffix: wanted === 'annual' ? '/year' : '/mo',
        monthlyEquivalent: wanted === 'annual' ? chosen.monthly_equivalent_display + '/mo' : '',
        allowance: (f.monthly_question_allowance || 0).toLocaleString() + ' Q',
        daily: (f.daily_question_limit || 0).toLocaleString() + '/day',
        session: (f.session_question_limit || 0).toLocaleString() + '/session',
        saving: (f.annual_saving && wanted === 'annual') ? f.annual_saving.label : '',
      };
    });
}
