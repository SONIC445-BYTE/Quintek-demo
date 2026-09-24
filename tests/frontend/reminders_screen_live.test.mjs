/**
 * The reminder screen (ADR-031), through the real component, the real client
 * and a running server.
 *
 * The component is handed the REAL client module as `studentApi`, so pressing
 * "Add reminder" in these tests goes component -> quintek-student-api.js ->
 * HTTP -> student/api.py -> the database, and the list the test reads back is
 * what the server stored. Nothing between the learner's keystrokes and the row
 * is stubbed.
 *
 * What must hold:
 *   * the words are shown and stored exactly as typed -- a leading space, a
 *     newline, non-Latin text;
 *   * any number, each independent: cancelling one leaves the rest;
 *   * a refusal (a time the clocks skip, a past time) is shown with the
 *     server's reason and the draft is kept;
 *   * with no sender configured the screen says, before anything is relied
 *     on, that delivery is off -- and a reminder whose time came reads
 *     "Not sent" with the reason, never "Sent";
 *   * without a backend the rows are labelled as an example, never as the
 *     learner's list.
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import test from 'node:test';

import { loadComponent, mount, plain } from './render_harness.mjs';

const PORT = 9471 + (process.pid % 300);
const ORIGIN = `http://127.0.0.1:${PORT}`;

let server, dir, db, api, Component;

function run(cmd, args) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '', err = '';
    p.stdout.on('data', (d) => { out += d; });
    p.stderr.on('data', (d) => { err += d; });
    p.on('close', (code) => resolve({ code, out, err }));
  });
}

async function up(tries = 100) {
  for (let i = 0; i < tries; i++) {
    try { if ((await fetch(`${ORIGIN}/health`)).ok) return true; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

test.before(async () => {
  dir = mkdtempSync(join(tmpdir(), 'quintek-reminders-'));
  db = join(dir, 'q.db');
  server = spawn('python3', ['-m', 'benchmark.cli', 'serve-student', '--host', '127.0.0.1',
    '--port', String(PORT), '--db', db, '--no-ai'],
    { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
  if (!await up()) throw new Error('the server did not start, so NOTHING was verified');
  globalThis.window = { __QUINTEK_STUDENT_API__: ORIGIN };
  api = await import('../../frontend/quintek-student-api.js?rem=' + Date.now());
  Component = loadComponent();
});

test.after(() => {
  if (server) server.kill('SIGKILL');
  if (dir) rmSync(dir, { recursive: true, force: true });
  delete globalThis.window;
});

let n = 0;
async function freshLearner() {
  const email = `rem${++n}-${process.pid}@example.com`;
  const out = await api.register(email, 'correct-horse');
  api.setToken(out.token);
  return out;
}

/** A mounted settings screen whose loader really loads. */
function screen(extra = {}) {
  const inst = mount(Component, { route: 'settings', studentApi: api, ...extra });
  // `mount` stubs every loader; this one is the subject, so put the real one back.
  inst.loadReminders = realMethod(inst, 'loadReminders');
  return inst;
}

/** Arrow-function class fields are bound per instance, so the real method is
 *  taken from a fresh instance and re-pointed at `inst`'s state. */
function realMethod(inst, name) {
  const donor = new Component();
  donor.props = inst.props;
  Object.defineProperty(donor, 'state', { get: () => inst.state });
  donor.setState = (p) => inst.setState(p);
  // Any method the donor calls on itself must also reach `inst`.
  for (const k of ['deviceTimezone']) donor[k] = inst[k];
  return donor[name];
}

async function type(inst, { label, date, time }) {
  const v = inst.renderVals();
  if (label !== undefined) v.setRemLabel({ target: { value: label } });
  if (date !== undefined) inst.renderVals().setRemDate({ target: { value: date } });
  if (time !== undefined) inst.renderVals().setRemTime({ target: { value: time } });
}

async function submit(inst) {
  inst.deviceTimezone = () => 'Asia/Kolkata';
  inst.loadReminders = realMethod(inst, 'loadReminders');
  const p = realMethod(inst, 'submitReminder')();
  await p;
}

test('a new learner sees an empty list and is told delivery is off', async () => {
  await freshLearner();
  const inst = screen();
  await inst.loadReminders();
  const v = inst.renderVals();
  assert.equal(v.remListNote, 'You have no reminders yet.');
  assert.deepEqual(plain(v.reminderRows), []);
  assert.equal(v.remDeliveryOff, true,
    'no sender is configured, and the screen does not say so');
  assert.match(v.remDeliveryNote, /Nothing delivers reminders in this browser/);
  assert.equal(v.remSubmitLabel, 'Add reminder');
});

test('the words are stored and shown exactly as typed', async () => {
  await freshLearner();
  const inst = screen();
  const label = '  revise patho\nthen मस्तिष्क';
  await type(inst, { label, date: '2099-01-10', time: '20:00' });
  await submit(inst);
  assert.equal(inst.state.remFormError, '', inst.state.remFormError);
  const rows = inst.renderVals().reminderRows;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].label, label, 'the screen changed the learner\'s words');
  assert.equal(rows[0].when, '2099-01-10 · 20:00 · Asia/Kolkata');
  assert.equal(rows[0].status, 'Scheduled');
  // And the SERVER holds the same text, not just the screen.
  const listed = await api.listReminders();
  assert.equal(listed.reminders[0].label, label);
  // The form cleared after a save.
  assert.equal(inst.state.remLabel, '');
});

test('any number, and cancelling one leaves the others', async () => {
  await freshLearner();
  const inst = screen();
  for (const [label, date] of [['revise micro', '2099-02-01'], ['revise patho', '2099-01-01'],
                               ['revise pharm', '2099-01-15']]) {
    await type(inst, { label, date, time: '07:00' });
    await submit(inst);
  }
  let rows = inst.renderVals().reminderRows;
  assert.deepEqual(rows.map((r) => r.label), ['revise patho', 'revise pharm', 'revise micro'],
    'the list is not in the order the reminders fire');
  assert.equal(inst.renderVals().remListNote, '3 reminders');

  const pharm = rows.find((r) => r.label === 'revise pharm');
  await realMethod(inst, 'cancelReminder')(pharm.id)();
  // `cancelReminder` re-reads through `this.loadReminders`, which the donor
  // resolved to the real one; read the list back explicitly as well.
  await realMethod(inst, 'loadReminders')();
  rows = inst.renderVals().reminderRows;
  const by = Object.fromEntries(rows.map((r) => [r.label, r]));
  assert.equal(by['revise pharm'].status, 'Cancelled');
  assert.equal(by['revise pharm'].pending, false, 'a cancelled reminder still offers CANCEL');
  assert.equal(by['revise patho'].status, 'Scheduled');
  assert.equal(by['revise micro'].status, 'Scheduled');
});

test('an edit changes that reminder and keeps its timezone', async () => {
  await freshLearner();
  const inst = screen();
  await type(inst, { label: 'revise patho', date: '2099-01-10', time: '20:00' });
  await submit(inst);
  const row = inst.renderVals().reminderRows[0];
  row.edit();
  let v = inst.renderVals();
  assert.equal(v.remEditing, true);
  assert.equal(v.remLabel, 'revise patho');
  assert.equal(v.remSubmitLabel, 'Save changes');
  assert.match(v.remTzNote, /Asia\/Kolkata, the timezone this reminder was set in/);
  await type(inst, { label: 'revise patho, chapter 4', time: '21:15' });
  // Even if the device has moved timezone since, an edit does not move it.
  inst.deviceTimezone = () => 'America/New_York';
  await realMethod(inst, 'submitReminder')();
  v = inst.renderVals();
  assert.equal(v.remEditing, false);
  assert.equal(v.reminderRows.length, 1, 'the edit created a second reminder');
  assert.equal(v.reminderRows[0].label, 'revise patho, chapter 4');
  assert.equal(v.reminderRows[0].when, '2099-01-10 · 21:15 · Asia/Kolkata');
});

test('a time the clocks skip is refused with the reason, and the draft is kept', async () => {
  await freshLearner();
  const inst = screen();
  await type(inst, { label: 'revise renal', date: '2099-03-29', time: '01:30' });
  inst.deviceTimezone = () => 'Europe/London';
  inst.loadReminders = realMethod(inst, 'loadReminders');
  await realMethod(inst, 'submitReminder')();
  const v = inst.renderVals();
  assert.match(v.remFormError, /does not exist in Europe\/London/);
  assert.equal(v.remLabel, 'revise renal', 'the draft was thrown away on a refusal');
  assert.equal((await api.listReminders()).reminders.length, 0);
});

test('a time already past is refused with the reason', async () => {
  await freshLearner();
  const inst = screen();
  await type(inst, { label: 'too late', date: '2001-01-01', time: '09:00' });
  await submit(inst);
  assert.match(inst.renderVals().remFormError, /already passed/);
});

test('an incomplete form is not sent', async () => {
  await freshLearner();
  const inst = screen();
  await type(inst, { label: '   ', date: '2099-01-01', time: '09:00' });
  await submit(inst);
  assert.match(inst.renderVals().remFormError, /needs some text, a date and a time/);
  assert.equal((await api.listReminders()).reminders.length, 0);
});

test('a reminder whose time came with no sender reads "Not sent", with the reason', async () => {
  const me = await freshLearner();
  const inst = screen();
  await type(inst, { label: 'revise patho', date: '2099-01-10', time: '20:00' });
  await submit(inst);
  // Run the real scheduler entry point at a moment after the reminder.
  const fired = await run('python3', ['-c', `
import sys; sys.path.insert(0, ".")
from datetime import datetime, timezone
from student.db import Database
from student.reminders import ReminderService
print(ReminderService(Database(sys.argv[1])).run_due(at=datetime(2100,1,1,tzinfo=timezone.utc)))
`, db]);
  assert.equal(fired.code, 0, fired.err);
  await realMethod(inst, 'loadReminders')();
  const row = inst.renderVals().reminderRows[0];
  assert.equal(row.status, 'Not sent — no notification sender is configured');
  assert.equal(row.statusFg, '#BC4C43');
  assert.equal(row.pending, false);
  assert.ok(me.user_id);
});

test('without a backend the rows are labelled as an example', () => {
  const inst = mount(Component, { route: 'settings', studentApi: null });
  const v = inst.renderVals();
  assert.match(v.remListNote, /^Example reminders/);
  assert.equal(v.remDeliveryOff, false,
    'the delivery banner is a statement about a server; with none it has nothing to say');
  assert.ok(v.reminderRows.length > 0);
});

test('a failed load says so rather than showing an empty list', () => {
  const inst = mount(Component, { route: 'settings', studentApi: api,
                                  remState: 'error', remError: 'HTTP 503' });
  const v = inst.renderVals();
  assert.match(v.remListNote, /could not be loaded — HTTP 503/);
  assert.equal(v.remListFg, '#BC4C43');
  assert.equal(v.remDeliveryOff, false);
});

test('the settings screen has no trace of the retired single-reminder model', () => {
  const v = inst0();
  for (const gone of ['triggerTimes', 'channels', 'testNotif', 'lastNotif', 'noteDraft',
                      'nextTrigger']) {
    assert.ok(!(gone in v), `renderVals still exposes ${gone}`);
  }
});

function inst0() {
  return mount(Component, { route: 'settings', studentApi: api }).renderVals();
}
