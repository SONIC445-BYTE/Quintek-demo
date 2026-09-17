/**
 * The component logic parses.
 *
 * WHY THIS EXISTS
 * ---------------
 * `frontend/PG Revision.dc.html` shipped a SyntaxError.
 *
 *     const live = this.learnerView();   // top of renderVals()
 *     ...
 *     const live = !!s.liveQuestionId;   // 113 lines later, same scope
 *
 * A `const` redeclared in one scope is a parse error, not a runtime one, so
 * the class never came into existence and every screen in the student app
 * rendered blank. It went into `frontend/dist/`, into the android assets and
 * into app-debug.apk.
 *
 * Nothing caught it, and the gap was structural rather than bad luck. The
 * other tests in this directory drive `quintek-student-api.js` -- a real ES
 * module Node imports, so a syntax error there fails immediately. The Python
 * tests that "read the built bundles" read them AS TEXT and assert on
 * substrings, which a broken file satisfies exactly as well as a working one.
 * Between the two, nobody ever asked a JavaScript engine to look at the
 * component.
 *
 * WHAT IT CHECKS
 * --------------
 * Every `.dc.html` design source and every built bundle is handed to V8 for
 * compilation. `vm.Script` compiles without executing, which is the whole
 * point: running it would need React, a DOM and a backend, and none of those
 * are what broke.
 *
 * This cannot prove the screens render correctly. It proves the file is
 * JavaScript, which is the floor every other frontend claim stands on and the
 * one nothing was checking.
 */

import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = join(ROOT, 'frontend');
const DIST = join(FRONTEND, 'dist');

/** The `text/x-dc` block, which is where the component class lives.
 *
 *  Returned as a list so a file carrying more than one is fully covered
 *  rather than silently reduced to its first. */
function dcScripts(html) {
  const out = [];
  const re = /<script\b[^>]*\btype="text\/x-dc"[^>]*>([\s\S]*?)<\/script>/g;
  let m;
  while ((m = re.exec(html)) !== null) out.push(m[1]);
  return out;
}

function compiles(source, label) {
  try {
    /* Compile only. `new vm.Script` runs V8's parser over the whole source
     * and throws on a syntax error; it does not execute a single statement. */
    new vm.Script(source, { filename: label });
    return null;
  } catch (err) {
    return err;
  }
}

function check(t, file, html) {
  const blocks = dcScripts(html);
  assert.ok(blocks.length > 0,
    `${file}: no <script type="text/x-dc"> block found. Either the file stopped ` +
    'carrying its component or this test stopped finding it -- both are failures, ' +
    'because a test that silently checks nothing is worse than no test.');

  blocks.forEach((source, i) => {
    const label = blocks.length > 1 ? `${file} [block ${i + 1}]` : file;
    const err = compiles(source, label);
    assert.equal(err, null,
      err ? `${label} does not parse: ${err.message}\n` +
            'The component class will not be constructed and every screen it ' +
            'renders will be blank.' : '');
  });
}

test('every .dc.html design source parses', (t) => {
  const files = readdirSync(FRONTEND).filter((f) => f.endsWith('.dc.html'));
  assert.ok(files.length >= 4,
    `expected the four design sources, found ${files.length}`);
  for (const f of files) check(t, f, readFileSync(join(FRONTEND, f), 'utf8'));
});

test('every built bundle parses', (t) => {
  if (!existsSync(DIST)) {
    /* A skip is not a pass, and this one is loud on purpose: the built
     * bundles are what the APK ships, so "not built yet" must not read the
     * same as "built and fine". */
    t.skip('frontend/dist does not exist -- run tools_build_standalone.py');
    return;
  }
  const files = readdirSync(DIST).filter((f) => f.endsWith('.html'));
  assert.ok(files.length > 0, 'frontend/dist exists but holds no bundles');
  for (const f of files) check(t, f, readFileSync(join(DIST, f), 'utf8'));
});

/**
 * The android assets are what actually runs on a phone.
 *
 * `frontend/dist` and the assets directory are written by the same build, but
 * they are written SEPARATELY, and a rebuild that updates one and not the
 * other is exactly the "did the emulator pick up my change?" failure the
 * build stamp exists for. Checking dist alone would let a stale, broken asset
 * ship behind a green test.
 */
test('every android asset bundle parses', (t) => {
  const assets = join(ROOT, 'android', 'app', 'src', 'main', 'assets');
  if (!existsSync(assets)) {
    t.skip('android assets are not present in this checkout');
    return;
  }
  const files = readdirSync(assets).filter((f) => f.endsWith('.html'));
  assert.ok(files.length > 0, 'the android assets directory holds no bundles');
  for (const f of files) check(t, f, readFileSync(join(assets, f), 'utf8'));
});
