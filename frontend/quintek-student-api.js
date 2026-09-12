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

/* ------------------------------------------------------------------------
 * THE MODEL-FREE SURFACE
 *
 * Everything below reads state the engine has already computed. None of it
 * makes an inference call, so none of it depends on a credential, a provider
 * or a budget -- these screens work against a live backend today.
 *
 * Each returns the payload's own shape rather than a normalised one, because
 * a screen that renders "0 due" and a screen that renders "not measured" are
 * different screens, and flattening a missing value to a zero here would take
 * that choice away from the caller. `null` means the engine did not compute
 * it; `0` means it computed zero.
 * --------------------------------------------------------------------- */

export async function me() {
  return call('GET', '/me');
}

export async function progress() {
  return call('GET', '/progress');
}

export async function gaps(opts) {
  const options = opts || {};
  const query = [];
  if (options.colour) query.push('colour=' + encodeURIComponent(options.colour));
  if (options.includeResolved) query.push('include_resolved=1');
  const suffix = query.length ? '?' + query.join('&') : '';
  return (await call('GET', '/gaps' + suffix)).gaps || [];
}

export async function gapEvidence(gapId) {
  return (await call('GET', '/gaps/' + encodeURIComponent(gapId))).evidence || [];
}

export async function gapQuestions(gapId) {
  return (await call('GET', '/gaps/' + encodeURIComponent(gapId) + '/questions'))
    .questions || [];
}

export async function resolveGap(gapId) {
  return call('POST', '/gaps/' + encodeURIComponent(gapId) + '/resolve', {});
}

export async function revisionDashboard() {
  return call('GET', '/revision/dashboard');
}

export async function concepts() {
  return (await call('GET', '/concepts')).concepts || [];
}

export async function conceptDetail(conceptId) {
  return call('GET', '/concepts/' + encodeURIComponent(conceptId));
}

/* --- the two screens that need a per-item fetch on entry ---
 *
 * Concept detail and notebook view were left rendering from constants, and
 * correctly so: the bulk loaders (`concepts()`, `notebooks()`) return list
 * rows, and a detail screen that padded those out with placeholders would be
 * showing a learner numbers nobody measured. What they need is a fetch when
 * the screen opens, which is what these are.
 *
 * Each returns the server's payload unchanged. The four states -- loading,
 * error, empty, ready -- are the caller's to render, and `screenState` below
 * is what decides which, so no screen invents a fifth. */

export async function notebook(notebookId) {
  return call('GET', '/notebooks/' + encodeURIComponent(notebookId));
}

export async function notebookQuestions(notebookId) {
  return (await call('GET', '/notebooks/' + encodeURIComponent(notebookId) +
                     '/questions')).questions || [];
}

/* Saying a question is wrong, and what this app claims to be. Both from
 * Phase 2; without the client calls they were server features nobody could
 * reach. */

export async function reportQuestion(questionId, kind, note) {
  return call('POST', '/questions/' + encodeURIComponent(questionId) + '/reports',
              { kind, note: note || '' });
}

export async function myReports() {
  return (await call('GET', '/reports')).reports || [];
}

export async function scope() {
  return call('GET', '/scope');
}

/* Which of the four states a screen is in.
 *
 * ONE function, because the interesting failure is a screen that invents a
 * fifth state -- most often "error rendered as empty", which tells a learner
 * their notebook is empty when the truth is that nobody could reach the
 * server. `error` wins over everything: if the fetch failed, what came back
 * is not data and its emptiness means nothing.
 *
 * `isEmpty` is passed in rather than guessed, because emptiness differs by
 * screen: a notebook with no sources is empty, a concept with no questions
 * yet is not necessarily. */
export function screenState({ loading, error, data, isEmpty }) {
  if (error) return 'error';
  if (loading) return 'loading';
  if (data === null || data === undefined) return 'loading';
  const empty = typeof isEmpty === 'function' ? isEmpty(data)
              : Array.isArray(data) ? data.length === 0
              : false;
  return empty ? 'empty' : 'ready';
}

export async function graph(notebookId) {
  const suffix = notebookId ? '?notebook=' + encodeURIComponent(notebookId) : '';
  return call('GET', '/graph' + suffix);
}

export async function question(questionId) {
  return call('GET', '/questions/' + encodeURIComponent(questionId));
}

/* --- a revision session, end to end. No inference anywhere in here: every
 * question was generated earlier and is being served from the bank. --- */

export async function startSession(count, strategy) {
  return call('POST', '/revision/sessions', {
    count: count || 20, strategy: strategy || 'adaptive',
  });
}

export async function nextQuestion(sessionId) {
  return call('GET', '/revision/next?session=' + encodeURIComponent(sessionId));
}

/* The answer is revealed by the RESPONSE to this call and is not available
 * before it. That ordering is the product, not an implementation detail:
 * "what you thought" versus "what was correct" is only a real comparison if
 * the second was not visible while deciding the first. */
export async function recordAttempt(questionId, answerIndex, colour, opts) {
  const options = opts || {};
  return call('POST', '/attempts', {
    question_id: questionId,
    user_answer: answerIndex,
    user_colour: colour,
    session_id: options.sessionId || null,
    gaps: options.gaps || [],
  });
}

export async function completeSession(sessionId) {
  return call('POST', '/revision/sessions/' + encodeURIComponent(sessionId) +
              '/complete', {});
}

export async function notificationPrefs() {
  return call('GET', '/settings/notifications');
}

export async function setNotificationPrefs(prefs) {
  return call('PUT', '/settings/notifications', prefs || {});
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
/* Deliberately NOT in the model-free block above. Reading progress needs no
 * model, but it belongs to the ingestion surface, and the guard in
 * tests/test_frontend_model_free.py is blunt on purpose: it forbids `/sources`
 * there outright rather than reasoning about which source calls are safe.
 * Weakening that rule to fit this call would trade a guard for a convenience. */
export async function sourceProgress(sourceId) {
  return call('GET', '/sources/' + encodeURIComponent(sourceId) + '/progress');
}

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
