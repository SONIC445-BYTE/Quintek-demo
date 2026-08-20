/* Handing a payment to Razorpay Checkout, and refusing to lie about the result.
 *
 * The one thing this file must never do is tell the user they are on a paid
 * plan. It cannot know. Razorpay's `handler` callback fires when the browser
 * finishes the payment flow, which is not the same as the payment settling,
 * and is definitely not the same as Quintek's backend having heard about it.
 * The subscription becomes ACTIVE when the WEBHOOK says so, and the only
 * honest thing to do here is submit, then ask the backend what it thinks.
 *
 * A client that showed "You're on Pro" on the handler callback would be wrong
 * for as long as the payment took, and permanently wrong whenever it failed
 * afterwards -- with the user's own screen as evidence against you.
 */

const SDK_URL = 'https://checkout.razorpay.com/v1/checkout.js';
const SDK_TIMEOUT_MS = 15000;

export const SUBMITTED = 'SUBMITTED';      // handed to the gateway, not confirmed
export const CONFIRMED = 'CONFIRMED';      // the backend now reports a paid plan
export const DISMISSED = 'DISMISSED';      // the user closed the sheet
export const FAILED = 'FAILED';            // the gateway reported a failure
export const UNAVAILABLE = 'UNAVAILABLE';  // checkout could not run here at all

export class CheckoutError extends Error {
  constructor(state, detail) {
    super(detail);
    this.name = 'CheckoutError';
    this.state = state;
  }
}

/* Load the SDK on demand rather than in the page.
 *
 * The app runs from file:// during design work and from an Android WebView in
 * production, and in both places a hard <script> tag in the head is a request
 * that may simply never resolve. Loading it here means the failure is a value
 * this module returns rather than a page that quietly renders without a
 * checkout button. */
export function loadSdk(doc) {
  /* Already present? Then no document is needed and none is asked for. The
   * SDK survives navigation within the WebView, so re-injecting the script on
   * every checkout would be a second copy of it. */
  if (typeof window !== 'undefined' && window.Razorpay) return Promise.resolve(window.Razorpay);

  const target = doc || (typeof document !== 'undefined' ? document : null);
  if (!target) {
    return Promise.reject(new CheckoutError(UNAVAILABLE,
      'the payment sheet cannot be opened here. Nothing has been charged.'));
  }

  return new Promise((resolve, reject) => {
    const existing = target.querySelector('script[data-quintek-checkout]');
    const script = existing || target.createElement('script');
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new CheckoutError(UNAVAILABLE,
        'the payment sheet could not be loaded. Check your connection and try again; '
        + 'nothing has been charged.'));
    }, SDK_TIMEOUT_MS);

    script.onload = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (typeof window !== 'undefined' && window.Razorpay) resolve(window.Razorpay);
      else reject(new CheckoutError(UNAVAILABLE, 'the payment sheet loaded but did not start'));
    };
    script.onerror = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new CheckoutError(UNAVAILABLE,
        'the payment sheet could not be reached. Nothing has been charged.'));
    };

    if (!existing) {
      script.src = SDK_URL;
      script.async = true;
      script.setAttribute('data-quintek-checkout', '1');
      (target.head || target.body).appendChild(script);
    }
  });
}

/* Open the sheet with the payload the BACKEND produced.
 *
 * Nothing is constructed here beyond display fields. The key id and the
 * subscription id come from the server's checkout response; a client that
 * assembled its own subscription id could pay for the wrong thing, and a
 * client that carried a key of its own would eventually carry the secret. */
export function openCheckout(checkoutPayload, options) {
  const opts = options || {};
  const payload = checkoutPayload || {};
  if (!payload.key || !payload.subscription_id) {
    return Promise.reject(new CheckoutError(UNAVAILABLE,
      'the server did not return a usable checkout session, so nothing was opened.'));
  }

  return loadSdk(opts.document).then((Razorpay) => new Promise((resolve) => {
    let settled = false;
    const finish = (state, detail) => {
      if (settled) return;
      settled = true;
      resolve({ state, detail: detail || '' });
    };

    const instance = new Razorpay({
      key: payload.key,
      subscription_id: payload.subscription_id,
      name: payload.name || 'Quintek',
      description: opts.description || '',
      recurring: payload.recurring,
      prefill: opts.prefill || {},
      /* Reaching the handler means the browser finished, not that the money
       * moved. SUBMITTED, never CONFIRMED. */
      handler: () => finish(SUBMITTED,
        'Payment submitted. Your plan changes once Quintek receives confirmation.'),
      modal: {
        ondismiss: () => finish(DISMISSED,
          'Payment cancelled. Nothing has been charged and your plan is unchanged.'),
      },
    });

    if (typeof instance.on === 'function') {
      instance.on('payment.failed', (event) => {
        const error = (event && event.error) || {};
        finish(FAILED, error.description
          || 'The payment did not go through. Your plan is unchanged.');
      });
    }
    instance.open();
  }));
}

/* Ask the BACKEND whether the plan actually changed.
 *
 * Polls the subscription endpoint for a bounded time. A webhook usually lands
 * in seconds, but "usually" is not a guarantee, so running out of attempts is
 * reported as still-pending rather than as failure -- telling someone their
 * payment failed when it is merely in flight invites a second payment. */
export function confirmWithBackend(billing, options) {
  const opts = options || {};
  const attempts = opts.attempts || 10;
  const waitMs = opts.waitMs || 1500;
  const sleep = opts.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));

  const paid = (sub) => !!sub && sub.plan && sub.plan !== 'free'
    && ['ACTIVE', 'TRIALING'].indexOf(sub.status) >= 0;

  const attempt = (left) => billing.subscription().then((sub) => {
    if (paid(sub)) return { state: CONFIRMED, subscription: sub };
    if (left <= 1) {
      return { state: SUBMITTED, subscription: sub,
        detail: 'Still confirming with your bank. This can take a minute; '
          + 'your plan will update on its own and you have not been charged twice.' };
    }
    return sleep(waitMs).then(() => attempt(left - 1));
  });

  return attempt(attempts);
}
