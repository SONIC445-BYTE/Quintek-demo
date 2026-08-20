/* Razorpay Checkout handoff.
 *
 * Every test here is about a claim the client must NOT make. The gateway's
 * success callback fires when the browser finishes, which is not when the
 * money settles and is definitely not when Quintek's backend has heard about
 * it. A screen that says "You're on Pro" at that moment is wrong for as long
 * as the payment takes and permanently wrong if it fails.
 */

import { readFileSync } from 'node:fs';

let failures = 0;
let passes = 0;
function check(name, ok) {
  if (ok) { passes += 1; console.log('PASS  ' + name); }
  else { failures += 1; console.log('FAIL  ' + name); }
}

const url = new URL('../../frontend/quintek-checkout.js', import.meta.url).href;
const m = await import(url + '?t=' + Date.now());

/* ---- a fake Razorpay SDK ---- */
function installSdk(behaviour) {
  const handlers = {};
  globalThis.window = globalThis.window || {};
  globalThis.window.Razorpay = function (config) {
    this.config = config;
    this.on = (event, fn) => { handlers[event] = fn; };
    this.open = () => behaviour(config, handlers);
  };
  return handlers;
}

/* ---- the honest outcome ---- */

installSdk((config) => config.handler({ razorpay_payment_id: 'pay_1' }));
let result = await m.openCheckout({ key: 'rzp_test_x', subscription_id: 'sub_1' });
check('checkout: a completed sheet is SUBMITTED, never CONFIRMED',
  result.state === m.SUBMITTED && result.state !== m.CONFIRMED);
check('checkout: the wording does not claim the plan has changed',
  /once Quintek receives confirmation/.test(result.detail));

installSdk((config) => config.modal.ondismiss());
result = await m.openCheckout({ key: 'k', subscription_id: 's' });
check('checkout: a dismissed sheet says nothing was charged',
  result.state === m.DISMISSED && /Nothing has been charged/.test(result.detail));

installSdk((config, handlers) =>
  handlers['payment.failed']({ error: { description: 'card declined' } }));
result = await m.openCheckout({ key: 'k', subscription_id: 's' });
check('checkout: a gateway failure carries the gateway\'s own reason',
  result.state === m.FAILED && result.detail === 'card declined');

/* Only the first outcome counts: a sheet that both fails and dismisses must
 * not resolve twice with contradictory states. */
installSdk((config, handlers) => {
  handlers['payment.failed']({ error: { description: 'declined' } });
  config.modal.ondismiss();
  config.handler({});
});
result = await m.openCheckout({ key: 'k', subscription_id: 's' });
check('checkout: the first outcome wins and later ones cannot overwrite it',
  result.state === m.FAILED);

/* ---- refusing to start ---- */

let threw = null;
try { await m.openCheckout({ subscription_id: 'only-half' }); } catch (e) { threw = e; }
check('checkout: an incomplete session is refused rather than opened',
  threw && threw.state === m.UNAVAILABLE);

threw = null;
try { await m.openCheckout(null); } catch (e) { threw = e; }
check('checkout: a missing session does not throw a TypeError at the user',
  threw && threw.name === 'CheckoutError');

/* ---- the backend has the last word ---- */

const paidSub = { plan: 'pro', status: 'ACTIVE' };
const freeSub = { plan: 'free', status: 'ACTIVE' };

let calls = 0;
result = await m.confirmWithBackend(
  { subscription: async () => { calls += 1; return calls < 3 ? freeSub : paidSub; } },
  { attempts: 5, sleep: async () => {} });
check('checkout: CONFIRMED only once the BACKEND reports a paid plan',
  result.state === m.CONFIRMED && result.subscription.plan === 'pro');
check('checkout: it waits for the webhook rather than giving up on the first look',
  calls === 3);

result = await m.confirmWithBackend(
  { subscription: async () => freeSub },
  { attempts: 3, sleep: async () => {} });
check('checkout: running out of attempts is still-pending, not failure',
  result.state === m.SUBMITTED);
check('checkout: a pending payment is not reported as one to retry',
  /have not been charged twice/.test(result.detail));

result = await m.confirmWithBackend(
  { subscription: async () => ({ plan: 'pro', status: 'PENDING' }) },
  { attempts: 2, sleep: async () => {} });
check('checkout: a PENDING subscription is not treated as paid',
  result.state === m.SUBMITTED);

/* ---- no secret, ever ---- */

const source = readFileSync(new URL('../../frontend/quintek-checkout.js', import.meta.url), 'utf8');
check('checkout: the client never mentions a key secret',
  !/key_secret|keySecret/.test(source));

installSdk((config) => config.handler({}));
let seen = null;
globalThis.window.Razorpay = function (config) {
  seen = config;
  this.on = () => {};
  this.open = () => config.handler({});
};
await m.openCheckout({ key: 'rzp_test_x', subscription_id: 'sub_9', name: 'Quintek' });
check('checkout: the key and subscription id come from the server payload, unaltered',
  seen.key === 'rzp_test_x' && seen.subscription_id === 'sub_9');

console.log(failures ? `\n${failures} failed` : `\nall passed (${passes})`);
process.exit(failures ? 1 : 0);
