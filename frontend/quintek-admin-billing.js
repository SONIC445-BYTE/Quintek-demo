/* Admin view of the money: what came in, what the AI cost, what is left.
 *
 * This is the ONLY client in the frontend allowed to see provider names,
 * token counts or cost figures. `quintek-billing-api.js` deliberately cannot:
 * a learner buys Quintek, and what a question cost to produce is neither their
 * business nor something to hand a competitor.
 *
 * The rule that matters here is the same one the rest of the codebase keeps:
 * an unmeasured number is reported as unmeasured. A cost-per-500 of "—" means
 * nothing has been costed yet. It does not mean zero, and it must never be
 * rendered as ₹0.00, because ₹0.00 is a number somebody will plan against.
 */

const BASE = (typeof window !== 'undefined' && window.__QUINTEK_STUDENT_API__) || null;
const PREFIX = '/billing/admin';

export const configured = BASE !== null;

let token = (typeof window !== 'undefined' && window.__QUINTEK_ADMIN_TOKEN__)
  || (typeof window !== 'undefined' && window.__QUINTEK_STUDENT_TOKEN__) || null;
export function setToken(value) { token = value; }

export class AdminBillingError extends Error {
  constructor(status, detail) {
    super(detail || ('HTTP ' + status));
    this.name = 'AdminBillingError';
    this.status = status;
  }
}

async function call(path) {
  if (!BASE) throw new AdminBillingError(0, 'no backend is configured');
  const headers = { accept: 'application/json' };
  if (token) headers.authorization = 'Bearer ' + token;
  let res;
  try {
    res = await fetch(BASE.replace(/\/+$/, '') + PREFIX + path, { headers });
  } catch (e) {
    throw new AdminBillingError(0, 'the billing service could not be reached ('
      + (e && e.message ? e.message : e) + ')');
  }
  const text = await res.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch (e) { payload = { error: text }; }
  if (!res.ok) {
    /* 404 on an admin route is how the API tells a non-admin that the surface
     * does not exist. Reporting it as "not found" would send an admin hunting
     * for a broken URL when the real answer is that this session is not an
     * admin one. */
    if (res.status === 404) {
      throw new AdminBillingError(404,
        'this endpoint is not available to your session. Admin economics '
        + 'require an admin account.');
    }
    throw new AdminBillingError(res.status, payload.error || res.statusText);
  }
  return payload;
}

export const daily = (day) => call('/economics' + (day ? '?day=' + encodeURIComponent(day) : ''));
export const perPlan = (since) => call('/economics/plans' + (since ? '?since=' + encodeURIComponent(since) : ''));
export const costPer500 = (since) => call('/economics/cost-per-500' + (since ? '?since=' + encodeURIComponent(since) : ''));
export const perModel = (since) => call('/economics/models' + (since ? '?since=' + encodeURIComponent(since) : ''));

/* ---- presentation ----
 *
 * Formatting only. `dash` is the whole discipline: a value the backend did not
 * measure comes back null, and null renders as an em dash rather than a zero. */
export function dash(value) {
  return (value === null || value === undefined || value === '') ? '—' : value;
}

/* Keys here mirror `EconomicsService.daily()` exactly. A Python test pins that
 * payload's key set so the two cannot drift apart quietly -- a renamed field
 * would otherwise show as an em dash and read as "not measured yet". */
export function economicsTiles(payload) {
  const p = payload || {};
  const negative = typeof p.contribution_minor === 'number' && p.contribution_minor < 0;
  return [
    { label: 'REVENUE (RECOGNISED)', value: dash(p.revenue),
      note: 'annual subscriptions divided across twelve months' },
    { label: 'AI SPEND', value: dash(p.ai_cost),
      note: dash(p.ai_calls) + ' calls' },
    { label: 'GATEWAY FEES', value: dash(p.payment_fees),
      note: 'estimated from recognised revenue, not from settled statements' },
    { label: 'CONTRIBUTION', value: dash(p.contribution),
      note: negative
        ? 'NEGATIVE — the AI costs more than the plans bring in'
        : 'after AI spend and fees',
      alarming: negative },
  ];
}

export function costPer500Row(payload) {
  const p = payload || {};
  const measured = p.accepted > 0;
  return {
    measured,
    /* Not "₹0.00". Nothing accepted means nothing has been measured, and a
     * zero here would be read as "free". */
    value: measured ? dash(p.cost_per_batch_display) : '—',
    accepted: p.accepted || 0,
    produced: p.produced || 0,
    acceptanceRate: (p.acceptance_rate === null || p.acceptance_rate === undefined)
      ? '—' : Math.round(p.acceptance_rate * 100) + '%',
    unpriced: p.unpriced_calls || 0,
    /* An unpriced call is spend the ledger cannot see. Saying so beside the
     * figure stops it being quoted as the cost base. */
    caveat: (p.unpriced_calls || 0) > 0
      ? p.unpriced_calls + ' call(s) had no price on record, so this understates the true cost'
      : (measured ? '' : 'nothing has been accepted yet, so there is no cost per 500 to report'),
  };
}
