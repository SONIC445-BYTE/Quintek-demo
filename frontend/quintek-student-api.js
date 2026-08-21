/* Client for the learner backend (student/server.py).
 *
 * This is the seam through which a screen stops being a mock. It follows the
 * same rule as quintek-eval-api.js, for the same reason:
 *
 *   THE SIMULATION RUNS ONLY WHEN NO BACKEND WAS CONFIGURED.
 *
 * With no `window.__QUINTEK_STUDENT_API__`, this module reports
 * `configured: false` and the screen keeps its in-file constants -- correct
 * for a design file opened from disk, and nobody mistakes that for production.
 * With a backend configured, every answer comes from it; a failure is an
 * outage and is reported as one, never papered over with the demo data that
 * happens to be sitting in the same file.
 *
 * Scope is deliberately one interaction. `generateQuestions` is wired; the
 * rest of the app still runs on constants. Wiring a screen at a time keeps the
 * debugging surface small enough to actually debug, which is the whole reason
 * for doing it this way rather than in one pass.
 */

const BASE = (typeof window !== 'undefined' && window.__QUINTEK_STUDENT_API__) || null;

export const configured = BASE !== null;

let token = (typeof window !== 'undefined' && window.__QUINTEK_STUDENT_TOKEN__) || null;

export class BackendError extends Error {
  constructor(status, detail) {
    super(detail || ('HTTP ' + status));
    this.name = 'BackendError';
    this.status = status;
  }
}

async function call(method, path, body) {
  if (!BASE) throw new BackendError(0, 'no learner backend is configured');
  const headers = { accept: 'application/json' };
  if (body !== undefined) headers['content-type'] = 'application/json';
  if (token) headers.authorization = 'Bearer ' + token;

  let res;
  try {
    res = await fetch(BASE + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    /* A network failure is an outage. It is not a reason to show demo data. */
    throw new BackendError(0, 'the learner backend could not be reached (' +
      (e && e.message ? e.message : e) + ')');
  }
  const text = await res.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch (e) { payload = { error: text }; }
  if (!res.ok) throw new BackendError(res.status, payload.error || res.statusText);
  return payload;
}

export async function login(email, password) {
  const out = await call('POST', '/auth/login', { email, password });
  token = out.token;
  return out;
}

export async function register(email, password, name) {
  const out = await call('POST', '/auth/register', { email, password, name });
  token = out.token;
  return out;
}

export function setToken(value) { token = value; }
export function hasToken() { return !!token; }

export async function notebooks() {
  return (await call('GET', '/notebooks')).notebooks || [];
}

export async function createNotebook(title) {
  return call('POST', '/notebooks', { title });
}

/* THE WIRED INTERACTION.
 *
 * Returns the questions the engine actually generated and stored, each with
 * the provenance the screen should be showing anyway: which model wrote it,
 * whether an independent validator approved it, and which source chunk it
 * came from. The simulation could not supply any of that, which is precisely
 * why replacing it is worth doing.
 */
export async function generateQuestions(notebookId, count, opts) {
  const options = opts || {};
  const result = await call('POST', '/notebooks/' + notebookId + '/questions', {
    count: count,
    family: options.family || '',
    difficulty: options.difficulty || '',
    reasoning_depth: options.reasoningDepth || '',
    /* Free-text instructions the learner typed. Passed through verbatim; the
     * generator's grounding rule is applied server-side and cannot be
     * overridden from here. */
    constraints: options.constraints || '',
    /* The billing reservation's id, passed through so what this generation
     * spends on inference is filed against the entitlement that authorised
     * it. Blank when nothing reserved -- the server records the spend against
     * nobody rather than guessing an owner. */
    batch_id: options.batch_id || options.batchId || '',
    /* Style references the learner supplied. The engine reads their SHAPE --
     * stem structure, reasoning depth, distractor strategy -- and is
     * forbidden from reusing any fact from them; that rule lives in
     * `student/generation.py`, not here, because a grounding rule enforced by
     * a client is a grounding rule anyone can turn off. */
    demo_ids: options.demoIds || options.demo_ids || [],
  });

  const ids = result.question_ids || [];
  const bank = await call('GET', '/questions?limit=200');
  const byId = {};
  (bank.questions || []).forEach((q) => { byId[q.id] = q; });

  return {
    count: result.count || ids.length,
    validation: result.validation || null,
    questions: ids.map((id) => byId[id]).filter(Boolean).map((q) => ({
      id: q.id,
      family: q.family || '',
      stem: q.stem,
      options: q.options || [],
      difficulty: q.difficulty || '',
      /* Provenance travels with the question, not in a separate panel. */
      validationStatus: q.validation_status || 'pending',
      generatedBy: q.generated_by_candidate_id || '',
      validatedBy: q.validated_by_candidate_id || '',
      sourceId: q.source_id || null,
      chunkId: q.chunk_id || null,
    })),
  };
}

export async function questionBank(limit) {
  return (await call('GET', '/questions?limit=' + (limit || 50))).questions || [];
}

/* What this deployment is actually running, for the screen that discloses it. */
export async function powering() {
  return call('GET', '/ai/benchmark/powering');
}


/* Save an example question as a style reference.
 *
 * Returns the demonstration's id, which `generateQuestions` takes as
 * `demoIds`. Text only: the backend stores a question's SHAPE, and reading
 * one out of a photograph needs OCR that `student/ingestion.py` reports as
 * unconfigured. Offering an image path here would be a control that looks
 * like it works and cannot.
 */
export async function createDemo(title, question, opts) {
  const options = opts || {};
  return call('POST', '/demos', {
    title: title,
    question: question,
    question_type: options.questionType || '',
    difficulty: options.difficulty || '',
    reasoning_depth: options.reasoningDepth || '',
    /* Free-text instructions the learner typed. Passed through verbatim; the
     * generator's grounding rule is applied server-side and cannot be
     * overridden from here. */
    constraints: options.constraints || '',
    notes: options.notes || '',
  });
}

export async function listDemos() {
  return call('GET', '/demos');
}


/* Which source kinds this deployment can actually read.
 *
 * Unauthenticated, because the source picker is the first screen a new learner
 * sees. Without it the picker offers five kinds with equal prominence and
 * three of them fail the moment they are tried -- the learner finds out after
 * committing a file, not before choosing.
 */
export async function capabilities() {
  return call('GET', '/capabilities');
}


/* Read a File as base64, without the data: prefix.
 *
 * FileReader rather than an ArrayBuffer loop: the loop version is faster to
 * write and blows the call stack on anything past a few megabytes, because
 * `String.fromCharCode.apply` takes the whole array as arguments.
 */
export function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new BackendError(0,
      'the file could not be read from your device'));
    reader.onload = () => {
      const result = String(reader.result || '');
      const comma = result.indexOf(',');
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

/* Add a source to a notebook, sending the FILE when there is one.
 *
 * The picker used to produce a File that went nowhere: `sources` carried a
 * storage_key, ingestion resolved it under storage_dir, and no endpoint ever
 * wrote a byte. A PDF could be chosen and never read.
 */
export async function addSource(notebookId, kind, opts) {
  const options = opts || {};
  const payload = {
    kind: kind,
    filename: options.filename || (options.file && options.file.name) || '',
    mime_type: options.mimeType || (options.file && options.file.type) || '',
    text: options.text || '',
    url: options.url || '',
  };
  if (options.file) {
    payload.content_base64 = await fileToBase64(options.file);
  }
  return call('POST', '/notebooks/' + notebookId + '/sources', payload);
}

/* Poll until extraction finishes. Resolves with the final progress payload.
 *
 * Ingestion is asynchronous, so a caller that returns as soon as the POST
 * answers 202 reports success for a source nothing has read yet. `onProgress`
 * exists so a screen can show what stage it is at instead of a spinner that
 * says nothing.
 */
export async function waitForSource(sourceId, opts) {
  const options = opts || {};
  const attempts = options.attempts || 120;
  const waitMs = options.waitMs || 1000;
  const sleep = options.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));

  for (let i = 0; i < attempts; i += 1) {
    const progress = await call('GET', '/sources/' + sourceId + '/progress');
    if (options.onProgress) options.onProgress(progress);
    const status = (progress && progress.status) || '';
    if (status === 'extracted') return progress;
    if (status === 'failed') {
      throw new BackendError(422, progress.error
        || 'this source could not be read');
    }
    await sleep(waitMs);
  }
  /* Timing out is NOT failure: extraction may still be running. Saying so
   * stops a learner uploading the same chapter a second time. */
  throw new BackendError(0,
    'this source is taking longer than expected to read. It is still being '
    + 'processed; check the notebook shortly rather than uploading it again.');
}
