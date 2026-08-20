/* Every {{ binding }} in the design file must have somewhere to come from.
 *
 * An unbound binding is silent: the runtime renders nothing and the screen
 * looks merely empty. That is how a Cancel button ends up with no label and a
 * capacity panel ends up with no explanation -- both of which happened while
 * these screens were being written.
 *
 * This is a static check, so it proves the NAME exists in the script, not that
 * the value is right. It catches typos and forgotten branches, which is the
 * class of defect that actually occurs here.
 */

import { readFileSync } from 'node:fs';

const FILES = ['frontend/PG Revision.dc.html'];
let failures = 0;
let checked = 0;

function check(name, ok) {
  if (!ok) { failures += 1; console.log('FAIL  ' + name); }
  else { checked += 1; console.log('PASS  ' + name); }
}

for (const file of FILES) {
  const src = readFileSync(file, 'utf8');
  const script = (src.match(/<script[^>]*>([\s\S]*?)<\/script>/g) || [])
    .map((b) => b.replace(/<\/?script[^>]*>/g, '')).join('\n');

  /* Loop aliases: `<sc-for list="{{ x }}" as="pc">` makes `pc.*` local. */
  const aliases = new Set();
  for (const m of src.matchAll(/as="([A-Za-z_$][\w$]*)"/g)) aliases.add(m[1]);

  const names = new Set();
  for (const m of src.matchAll(/\{\{\s*([A-Za-z_$][\w$]*)/g)) names.add(m[1]);

  const missing = [];
  for (const name of names) {
    if (aliases.has(name)) continue;
    if (name === 'true' || name === 'false' || name === 'null') continue;
    /* Assigned as an object key, a shorthand, or a class field. */
    const patterns = [
      new RegExp('\\b' + name + '\\s*:'),
      new RegExp('\\b' + name + '\\s*='),
      new RegExp('[,{]\\s*' + name + '\\s*[,}]'),
    ];
    if (!patterns.some((re) => re.test(script))) missing.push(name);
  }

  check(file + ': every binding has a source in the script',
    missing.length === 0 || (console.log('   unbound: ' + missing.join(', ')), false));

  /* The screens added for billing must actually be reachable. */
  for (const flag of ['isBilling', 'isPlans', 'makeCapacity']) {
    check(file + ': ' + flag + ' is both rendered and computed',
      src.indexOf('{{ ' + flag + ' }}') >= 0 && new RegExp('\\b' + flag + '\\s*:').test(script));
  }
}

console.log(failures ? `\n${failures} failed` : `\nall passed (${checked})`);
process.exit(failures ? 1 : 0);
