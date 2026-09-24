/**
 * Reminders delivered by the phone (ADR-031, on-device scheduling).
 *
 * The Android side (android/.../Reminders.kt) holds alarms and shows
 * notifications; it cannot be executed in this repository's build (no device,
 * no offline JUnit). So the decision of WHAT the phone schedules lives in
 * `reminderSyncPlan`, tested here directly, and the component is driven with a
 * FAKE phone bridge -- the same four methods `ReminderBridge` exposes -- against
 * a real server, so what reaches the bridge is what the server stored.
 *
 * What must hold:
 *   * the phone is handed exactly the pending, future reminders, with each
 *     label byte-for-byte and the server's UTC instant;
 *   * a cancel or an edit changes what the phone holds on the next sync;
 *   * notifications off is said, with a button, and the prompt's answer is
 *     reflected without a reload;
 *   * what the phone did (shown / blocked) is what the row says, and a
 *     reminder whose time passed unseen says so rather than "Scheduled".
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { loadComponent, mount, plain } from './render_harness.mjs';

// Not UTC. On a UTC machine a local-time parse and a UTC parse agree, and a
// plan that scheduled by the phone's clock instead of the server's instant
// would pass. Node honours a runtime change to TZ.
process.env.TZ = 'Asia/Kolkata';
assert.notEqual(new Date(0).getTimezoneOffset(), 0, 'the timezone did not take effect');

const PORT = 9591 + (process.pid % 200);
const ORIGIN = `http://127.0.0.1:${PORT}`;
let server, dir, api, Component;

async function up(tries = 100) {
  for (let i = 0; i < tries; i++) {
    try { if ((await fetch(`${ORIGIN}/health`)).ok) return true; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

test.before(async () => {
  dir = mkdtempSync(join(tmpdir(), 'quintek-device-'));
  server = spawn('python3', ['-m', 'benchmark.cli', 'serve-student', '--host', '127.0.0.1',
    '--port', String(PORT), '--db', join(dir, 'q.db'), '--no-ai'],
    { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
  if (!await up()) throw new Error('the server did not start, so NOTHING was verified');
  globalThis.window = { __QUINTEK_STUDENT_API__: ORIGIN };
  api = await import('../../frontend/quintek-student-api.js?device=' + Date.now());
  Component = loadComponent();
});

test.after(() => {
  if (server) server.kill('SIGKILL');
  if (dir) rmSync(dir, { recursive: true, force: true });
  delete globalThis.window;
});

/** The four methods `ReminderBridge` exposes, recording what it was given. */
function fakePhone({ permission = 'granted', outcomes = {}, fail = null } = {}) {
  const phone = {
    synced: [], asked: 0, permissionNow: permission, outcomesNow: outcomes,
    sync(json) {
      if (fail) throw new Error(fail);
      const plan = JSON.parse(json);
      phone.synced.push(plan);
      return JSON.stringify({ scheduled: plan.length, permission: phone.permissionNow });
    },
    permission() { return phone.permissionNow; },
    requestPermission() { phone.asked += 1; },
    outcomes() { return JSON.stringify(phone.outcomesNow); },
  };
  return phone;
}

/** The settings screen with its real loader and sync, on a given phone. */
function screen(phone, now) {
  const inst = mount(Component, { route: 'settings', studentApi: api });
  const donor = new Component();
  donor.props = inst.props;
  Object.defineProperty(donor, 'state', { get: () => inst.state });
  donor.setState = (p) => inst.setState(p);
  donor.deviceBridge = inst.deviceBridge = () => phone;
  if (now) donor.nowMs = inst.nowMs = () => now;
  donor.syncDevice = inst.syncDevice = donor.syncDevice;
  inst.loadReminders = donor.loadReminders;
  inst.requestNotificationPermission = donor.requestNotificationPermission;
  return inst;
}

let n = 0;
async function learner() {
  const out = await api.register(`dev${++n}-${process.pid}@example.com`, 'correct-horse');
  api.setToken(out.token);
}

const add = (label, local_date, local_time = '20:00') => api.createReminder(
  { label, localDate: local_date, localTime: local_time, timezone: 'Asia/Kolkata' });

// ---------------------------------------------------------------------------
// The decision: reminderSyncPlan
// ---------------------------------------------------------------------------

test('the plan is pending, future reminders, labels verbatim, the server instant', () => {
  const now = Date.parse('2030-01-01T00:00:00Z');
  const label = '  revise patho\nthen मस्तिष्क  ';
  const plan = api.reminderSyncPlan([
    { id: 'a', label, fire_at: '2030-01-02T14:30:00Z', status: 'pending' },
    { id: 'b', label: 'cancelled', fire_at: '2030-01-02T14:30:00Z', status: 'cancelled' },
    { id: 'c', label: 'fired', fire_at: '2030-01-02T14:30:00Z', status: 'fired' },
    { id: 'd', label: 'failed', fire_at: '2030-01-02T14:30:00Z', status: 'failed' },
    { id: 'e', label: 'passed', fire_at: '2029-12-31T23:59:00Z', status: 'pending' },
    { id: 'f', label: 'exactly now', fire_at: '2030-01-01T00:00:00Z', status: 'pending' },
    { id: 'g', label: 'garbled time', fire_at: 'tomorrow', status: 'pending' },
    { id: 7, label: 'numeric id', fire_at: '2030-02-01T00:00:00Z', status: 'pending' },
  ], now);
  assert.deepEqual(plain(plan), [
    { id: 'a', label, at: Date.parse('2030-01-02T14:30:00Z') },
    { id: '7', label: 'numeric id', at: Date.parse('2030-02-01T00:00:00Z') },
  ]);
});

test('an empty or missing list is an empty plan, which disarms everything', () => {
  assert.deepEqual(plain(api.reminderSyncPlan(null, 0)), []);
  assert.deepEqual(plain(api.reminderSyncPlan([], 0)), []);
});

// ---------------------------------------------------------------------------
// The screen and the phone
// ---------------------------------------------------------------------------

test('opening Reminders hands the phone exactly what the server holds', async () => {
  await learner();
  const label = '  revise patho\n  chapter 4 ';
  const a = await add(label, '2099-01-10');
  const b = await add('revise micro', '2099-01-11');
  await api.cancelReminder(b.id);
  const phone = fakePhone();
  const inst = screen(phone);
  await inst.loadReminders();
  assert.equal(phone.synced.length, 1, 'the phone was not synced when the list loaded');
  assert.deepEqual(phone.synced[0], [
    { id: a.id, label, at: Date.parse(a.fire_at) }]);
  assert.equal(Date.parse(a.fire_at), Date.parse('2099-01-10T14:30:00Z'),
    '20:00 Asia/Kolkata is 14:30 UTC; the phone would ring at the wrong time');
  const v = inst.renderVals();
  assert.match(v.remDeliveryNote, /This phone shows your reminders at their time/);
  assert.equal(v.remAskPermission, false);
});

test('a cancel and an edit change what the phone holds on the next sync', async () => {
  await learner();
  const a = await add('one', '2099-01-10');
  const b = await add('two', '2099-01-11');
  const phone = fakePhone();
  const inst = screen(phone);
  await inst.loadReminders();
  assert.deepEqual(phone.synced.at(-1).map((x) => x.id), [a.id, b.id]);

  await api.cancelReminder(a.id);
  await api.updateReminder(b.id, { localTime: '21:15' });
  await inst.loadReminders();
  const last = phone.synced.at(-1);
  assert.deepEqual(last.map((x) => x.id), [b.id], 'the cancelled reminder is still armed');
  assert.equal(last[0].at, Date.parse('2099-01-11T15:45:00Z'), 'the edit did not move the alarm');
});

test('notifications off is said, with a button that asks the phone', async () => {
  await learner();
  await add('revise renal', '2099-01-10');
  const phone = fakePhone({ permission: 'prompt' });
  const inst = screen(phone);
  await inst.loadReminders();
  let v = inst.renderVals();
  assert.match(v.remDeliveryNote, /Notifications are off for Quintek on this phone/);
  assert.equal(v.remDeliveryFg, '#BC4C43');
  assert.equal(v.remAskPermission, true);
  v.remRequestPermission();
  assert.equal(phone.asked, 1, 'the button did not ask the phone');

  // The phone answers yes and calls back into the page.
  phone.permissionNow = 'granted';
  inst.syncDevice(inst.state.liveReminders);
  v = inst.renderVals();
  assert.equal(v.remAskPermission, false);
  assert.match(v.remDeliveryNote, /This phone shows your reminders/);
});

test('what the phone did is what each row says', async () => {
  await learner();
  const shown = await add('shown', '2099-01-10');
  const blocked = await add('blocked', '2099-01-11');
  const unseen = await add('unseen', '2099-01-12');
  const future = await add('future', '2099-06-01');
  const phone = fakePhone({ outcomes: { [shown.id]: 'delivered', [blocked.id]: 'blocked' } });
  // A moment after the first three were due, before the fourth.
  const inst = screen(phone, Date.parse('2099-02-01T00:00:00Z'));
  await inst.loadReminders();
  const by = Object.fromEntries(inst.renderVals().reminderRows.map((r) => [r.label, r]));
  assert.equal(by.shown.status, 'Shown on this phone');
  assert.equal(by.blocked.status, 'Not shown — notifications were off on this phone');
  assert.equal(by.unseen.status, 'Time passed — this phone did not show it');
  assert.equal(by.future.status, 'Scheduled');
  for (const k of ['shown', 'blocked', 'unseen']) {
    assert.equal(by[k].pending, false, `${k}: edit/cancel offered after its time passed`);
  }
  assert.equal(by.future.pending, true);
  // And the passed ones are not re-armed to fire late.
  assert.deepEqual(phone.synced.at(-1).map((x) => x.id), [future.id]);
  void unseen; void shown; void blocked;
});

test('a phone that fails to schedule says so, in red', async () => {
  await learner();
  await add('x', '2099-01-10');
  const inst = screen(fakePhone({ fail: 'AlarmManager unavailable' }));
  await inst.loadReminders();
  const v = inst.renderVals();
  assert.match(v.remDeliveryNote, /could not schedule your reminders — AlarmManager unavailable/);
  assert.equal(v.remDeliveryFg, '#BC4C43');
});

test('in a browser there is no phone: nothing is synced and the page says so', async () => {
  await learner();
  await add('browser one', '2099-01-10');
  const inst = screen(null, Date.parse('2099-02-01T00:00:00Z'));
  await inst.loadReminders();
  const v = inst.renderVals();
  assert.match(v.remDeliveryNote, /Nothing delivers reminders in this browser/);
  assert.equal(v.remAskPermission, false);
  assert.equal(v.reminderRows[0].status, 'Time passed — nothing in this browser delivers it');
});

test('the how-to says who delivers, phone or browser', async () => {
  const onPhone = screen(fakePhone());
  onPhone.setState({ route: 'today' });
  const phoneNote = onPhone.renderVals().guideItems.find((g) => g.title === 'Reminders').note;
  assert.match(phoneNote, /This phone shows them/);

  const inBrowser = screen(null);
  inBrowser.setState({ route: 'today', remState: 'ready', remDelivery: false });
  const browserNote = inBrowser.renderVals().guideItems.find((g) => g.title === 'Reminders').note;
  assert.match(browserNote, /Nothing delivers them in a browser/);
});

test('the page registers the hook the phone calls after the prompt', () => {
  // A SOURCE check, stated as one: the component's `window` is the render
  // sandbox's, which this file cannot reach, and componentDidMount also starts
  // module imports the harness does not provide. The behaviour behind the hook
  // (re-sync, banner updates) is tested above through `syncDevice`; this pins
  // that the name WebScreenActivity.kt calls is the name the page assigns.
  const page = Component.toString();
  assert.match(page, /window\.__quintekNotificationPermissionChanged\s*=\s*\n?\s*\(\) => this\.syncDevice/);
  const kotlin = readFileSync('android/app/src/main/java/com/quintek/app/WebScreenActivity.kt', 'utf8');
  assert.ok(kotlin.includes('window.__quintekNotificationPermissionChanged()'),
    'the Android side calls a different name than the page registers');
});

// ---------------------------------------------------------------------------
// Deeper cases (2026-09-24)
// ---------------------------------------------------------------------------

test('two reminders at the same minute are both handed to the phone', async () => {
  await learner();
  const a = await add('first at 20:00', '2099-03-01', '20:00');
  const b = await add('second at 20:00', '2099-03-01', '20:00');
  const phone = fakePhone();
  const inst = screen(phone);
  await inst.loadReminders();
  const plan = phone.synced.at(-1);
  assert.deepEqual(plan.map((x) => x.id).sort(), [a.id, b.id].sort());
  assert.equal(plan[0].at, plan[1].at, 'same minute, same instant');
  assert.notEqual(plan[0].id, plan[1].id, 'the phone keys alarms by id; one would replace the other');
});

test('an edit after the phone has it supersedes the old alarm, text and time', async () => {
  await learner();
  const r = await add('old words', '2099-03-01', '20:00');
  const phone = fakePhone();
  const inst = screen(phone);
  await inst.loadReminders();
  await api.updateReminder(r.id, { label: 'new words', localTime: '06:45' });
  await inst.loadReminders();
  const plan = phone.synced.at(-1);
  assert.equal(plan.length, 1, 'the phone was left holding both the old and the new alarm');
  assert.deepEqual(plan[0], { id: r.id, label: 'new words',
                              at: Date.parse('2099-03-01T01:15:00Z') });
});

test('a cancel after the phone has it leaves the phone holding nothing', async () => {
  await learner();
  const r = await add('cancel me', '2099-03-01', '20:00');
  const phone = fakePhone();
  const inst = screen(phone);
  await inst.loadReminders();
  assert.equal(phone.synced.at(-1).length, 1);
  await api.cancelReminder(r.id);
  await inst.loadReminders();
  assert.deepEqual(phone.synced.at(-1), [],
    'an empty plan is what tells the phone to disarm; anything else leaves the alarm set');
});

test('a device timezone change does not move a reminder', async () => {
  // Set in Kolkata. The phone then travels. The reminder still means 20:00 in
  // Kolkata -- the zone it was set in, which the list shows -- so the instant
  // handed to the phone must not change.
  await learner();
  const r = await add('set in Kolkata', '2099-03-01', '20:00');
  const phone = fakePhone();
  const kolkata = screen(phone);
  await kolkata.loadReminders();
  const before = phone.synced.at(-1)[0].at;
  const saved = process.env.TZ;
  try {
    process.env.TZ = 'America/New_York';
    assert.equal(new Date(0).getTimezoneOffset(), 300, 'the timezone change did not take');
    const travelled = screen(phone);
    travelled.deviceTimezone = () => 'America/New_York';
    await travelled.loadReminders();
    assert.equal(phone.synced.at(-1)[0].at, before, 'the alarm moved with the phone');
    const v = travelled.renderVals();
    assert.equal(v.reminderRows[0].when, '2099-03-01 · 20:00 · Asia/Kolkata');
    assert.match(v.remTzNote, /America\/New_York, this device’s timezone/,
      'a NEW reminder should be in the device’s current zone, and say so');
  } finally {
    process.env.TZ = saved;
  }
  void r;
});

test('a reminder a year and more ahead is handed over exactly', async () => {
  await learner();
  await add('a year out', '2027-09-24', '20:00');
  await add('far out', '2099-12-31', '23:59');
  const phone = fakePhone();
  const inst = screen(phone);
  await inst.loadReminders();
  assert.deepEqual(phone.synced.at(-1).map((x) => x.at), [
    Date.parse('2027-09-24T14:30:00Z'), Date.parse('2099-12-31T18:29:00Z')]);
});

test('a missed reminder says it was missed, is not re-armed, and cannot be edited', async () => {
  await learner();
  const r = await add('missed one', '2099-03-01', '20:00');
  const phone = fakePhone({ outcomes: { [r.id]: 'missed' } });
  const inst = screen(phone, Date.parse('2099-03-02T00:00:00Z'));
  await inst.loadReminders();
  const row = inst.renderVals().reminderRows[0];
  assert.equal(row.status,
    'Missed — this phone was off or asleep when it was due, so it was not shown late');
  assert.equal(row.pending, false);
  assert.deepEqual(phone.synced.at(-1), [], 'a passed reminder was handed back to fire late');
});

test('the app syncs the phone when it opens, not only on the Reminders screen', () => {
  // A cancel made in a browser would otherwise leave the phone's alarm set
  // until the learner happened to open Reminders on the phone. componentDidMount
  // loads the reminders, and loading them is what syncs (tested above).
  const page = Component.toString();
  const mount = page.slice(page.indexOf('componentDidMount()'),
                           page.indexOf('componentDidMount()') + 4000);
  assert.match(mount, /this\.loadReminders\(api\)/);
});
