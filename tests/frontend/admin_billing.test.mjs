/* The admin economics client.
 *
 * One rule, applied everywhere: an unmeasured number renders as an em dash,
 * never as zero. ₹0.00 is a figure somebody will plan against; "—" is not.
 */

let failures = 0, passes = 0;
function check(name, ok) {
  if (ok) { passes += 1; console.log('PASS  ' + name); }
  else { failures += 1; console.log('FAIL  ' + name); }
}

globalThis.window = {};
const url = new URL('../../frontend/quintek-admin-billing.js', import.meta.url).href;
const m = await import(url + '?t=' + Date.now());

check('admin: with no backend the module reports itself unconfigured',
  m.configured === false);

let threw = null;
try { await m.daily(); } catch (e) { threw = e; }
check('admin: an unconfigured call throws rather than returning sample figures',
  threw && threw.name === 'AdminBillingError');

/* ---- the em dash discipline ---- */

check('admin: null renders as an em dash', m.dash(null) === '—');
check('admin: undefined renders as an em dash', m.dash(undefined) === '—');
check('admin: an empty string renders as an em dash', m.dash('') === '—');
check('admin: a real zero string is NOT swallowed', m.dash('₹0.00') === '₹0.00');
check('admin: the number zero survives', m.dash(0) === 0);

/* ---- the tiles ---- */

const EMPTY_DAILY = {
  day: '2026-08-20', revenue: '₹0.00', ai_cost: '₹0.00', payment_fees: '₹0.00',
  direct_infra: '₹0.00', contribution: '₹0.00', contribution_minor: 0,
  users: {}, paying_users: 0, ai_calls: 0, ai_by_model: [], warnings: [],
  fee_note: 'payment fees are computed from recognised revenue',
};

let tiles = m.economicsTiles(EMPTY_DAILY);
check('admin: the tiles read the payload\'s real keys',
  tiles[0].value === '₹0.00' && tiles[1].value === '₹0.00'
  && tiles[2].value === '₹0.00' && tiles[3].value === '₹0.00');
check('admin: a healthy contribution is not flagged',
  tiles[3].alarming !== true);

tiles = m.economicsTiles({ ...EMPTY_DAILY, contribution: '-₹4,200.00',
  contribution_minor: -420000 });
check('admin: a negative contribution is called out in words, not just a minus sign',
  tiles[3].alarming === true && /NEGATIVE/.test(tiles[3].note));

tiles = m.economicsTiles({});
check('admin: a missing payload dashes every tile rather than showing zeros',
  tiles.every((t) => t.value === '—'));

/* ---- cost per 500 ---- */

let row = m.costPer500Row({ accepted: 0, produced: 0, cost_per_batch_display: '—' });
check('admin: nothing accepted is unmeasured, not free',
  row.measured === false && row.value === '—');
check('admin: and it says why',
  /nothing has been accepted/.test(row.caveat));

row = m.costPer500Row({ accepted: 500, produced: 625, acceptance_rate: 0.8,
  cost_per_batch_display: '₹1.46', unpriced_calls: 0 });
check('admin: a measured figure is shown as the server formatted it',
  row.measured === true && row.value === '₹1.46' && row.acceptanceRate === '80%');
check('admin: a clean measurement carries no caveat', row.caveat === '');

row = m.costPer500Row({ accepted: 500, produced: 625, acceptance_rate: 0.8,
  cost_per_batch_display: '₹1.46', unpriced_calls: 12 });
check('admin: unpriced calls are named beside the figure, since they understate it',
  /12 call\(s\) had no price/.test(row.caveat) && /understates/.test(row.caveat));

row = m.costPer500Row({ accepted: 10, produced: 10, acceptance_rate: null,
  cost_per_batch_display: '₹2.00' });
check('admin: an unknown acceptance rate is a dash, not 0%',
  row.acceptanceRate === '—');

console.log(failures ? `\n${failures} failed` : `\nall passed (${passes})`);
process.exit(failures ? 1 : 0);
