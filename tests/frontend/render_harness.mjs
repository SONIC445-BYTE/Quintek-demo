/**
 * Compile the REAL student component and call its REAL `renderVals()`.
 *
 * WHY THIS EXISTS
 * ---------------
 * Twice now a screen has been reported wired because its loader worked, while
 * the render function beside it read something else:
 *
 *   * the concept screen fetched `/concepts/<id>` and then rendered the
 *     fixture's colour, source and passage over the top of it;
 *   * the question bank received `last_colour` and never showed it.
 *
 * Tests that drive `quintek-student-api.js` against a server prove the payload
 * is right. They say nothing about what the screen does with it. This harness
 * closes that gap: it compiles the `text/x-dc` block with V8, constructs the
 * class, injects state built from real payloads, and returns what the
 * template would bind.
 *
 * WHAT IT DOES NOT DO
 * -------------------
 * It does not render HTML, run React, or lay anything out. It checks the
 * values handed to the template. A binding typo in the markup itself is out of
 * its reach; `component_parses.test.mjs` and a device run cover the rest.
 */

import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

/** The real client module's pure helpers, so the component computes with the
 *  same functions it would call in the app. */
export async function realClient() {
  return import('../../frontend/quintek-student-api.js?harness=' + Date.now());
}

export function loadComponent() {
  const html = readFileSync(join(ROOT, 'frontend', 'PG Revision.dc.html'), 'utf8');
  const m = /<script\b[^>]*\btype="text\/x-dc"[^>]*>([\s\S]*?)<\/script>/.exec(html);
  if (!m) throw new Error('no text/x-dc block in PG Revision.dc.html');
  const sandbox = {
    console, Math, Date, JSON, String, Number, Array, Object, Boolean,
    isNaN, parseInt, parseFloat, Promise, setTimeout, clearTimeout,
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
    window: { __QUINTEK_STUDENT_API__: 'http://harness.invalid' },
  };
  sandbox.globalThis = sandbox;
  // The runtime's base class, reduced to what construction needs.
  sandbox.DCLogic = class { constructor() { this.props = {}; } setState() {} };
  const ctx = vm.createContext(sandbox);
  new vm.Script(m[1] + '\n;globalThis.__Component = Component;',
                { filename: 'PG Revision.dc.html' }).runInContext(ctx);
  return ctx.__Component;
}

/**
 * Build an instance with `overrides` merged over the component's own initial
 * state. `setState` MERGES into state synchronously, and every call is
 * recorded, so a test can press a control and read what it did.
 */
export function mount(Component, overrides, { onLoad } = {}) {
  const inst = new Component();
  inst.props = { showStreak: true };
  inst.pos = {};
  inst._raf = null;
  inst.calls = [];
  inst.setState = (patch) => {
    const next = typeof patch === 'function' ? patch(inst.state) : patch;
    inst.calls.push(next);
    inst.state = { ...inst.state, ...next };
  };
  inst.state = { ...inst.state, ...overrides };
  // Loaders must not reach the network from a render test.
  for (const name of Object.keys(inst)) {
    if (/^load[A-Z]/.test(name) && typeof inst[name] === 'function') {
      inst[name] = (...args) => { if (onLoad) onLoad(name, args); return null; };
    }
  }
  return inst;
}

/** Values built inside the vm context carry THAT realm's Array and Object
 *  prototypes, so `assert.deepStrictEqual` rejects them against literals from
 *  this realm even when they print identically. Round-trip before comparing
 *  structure; functions are dropped, which is what a structural check wants. */
export function plain(value) {
  return JSON.parse(JSON.stringify(value));
}
