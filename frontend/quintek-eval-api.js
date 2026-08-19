// Stand-in for the backend AI-evaluation API. The UI never computes any of these
// values — it fetches this shape and renders it. Swap `state` or edit the payload
// and every reliability screen changes with it.
//
// state: 'ok' | 'loading' | 'empty' | 'running' | 'incomplete' | 'failed'
//        | 'no_candidate' | 'stale' | 'error'

const FIXTURE_state = 'ok';

const FIXTURE_overview = {
  overallScore: 93.4,
  status: 'PASS',
  benchmarkVersion: 'Q-Bench v0.3',
  evaluatedAt: '15 Aug 2026',
  sampleSize: 1200,
  confidenceInterval: [91.8, 94.9],
  currentCandidate: { candidateId: 'cm:31c07d4e', provider: 'Anthropic', model: 'claude-sonnet-4.6', version: 'prompt v7' },
};

const FIXTURE_tracks = [
  { track: 'Medical knowledge', score: 95.1, status: 'PASS', sampleSize: 500, confidenceInterval: [93.2, 96.6],
    note: 'Answers PG-level questions drawn from a frozen expert-written corpus.' },
  { track: 'Concept understanding', score: 91.7, status: 'PASS', sampleSize: 300, confidenceInterval: [89.1, 93.8],
    note: 'Identifies the concepts a passage actually teaches.' },
  { track: 'Concept relationships', score: 90.8, status: 'PASS', sampleSize: 500, confidenceInterval: [88.2, 92.9],
    note: 'Links concepts correctly, including across subjects.' },
  { track: 'Question generation', score: 93.7, status: 'PASS', sampleSize: 300, confidenceInterval: [90.5, 96.1],
    note: 'Writes questions answerable from your source, at PG level.' },
  { track: 'Question validation', score: 96.2, status: 'PASS', sampleSize: 500, confidenceInterval: [94.3, 97.6],
    note: 'A second, independent model checks each question before you see it.' },
  { track: 'Knowledge-gap detection', score: 88.4, status: 'REVIEW', sampleSize: 250, confidenceInterval: [83.9, 92.0],
    note: 'Names the specific thing you missed rather than the whole topic.' },
  { track: 'Robustness', score: 91.3, status: 'PASS', sampleSize: 1500, confidenceInterval: [89.7, 92.7],
    note: 'Gives the same answer when a question is reworded.' },
];

const FIXTURE_routing = [
  { task: 'Question generation', candidate: 'Candidate B', score: 96.8, policy: 'QUALITY_FIRST' },
  { task: 'Concept extraction', candidate: 'Candidate A', score: 91.7, policy: 'QUALITY_FIRST' },
  { task: 'Knowledge-gap detection', candidate: 'Candidate A', score: 95.2, policy: 'QUALITY_FIRST' },
  { task: 'Validation', candidate: 'Candidate B', score: 96.2, policy: 'INDEPENDENCE_REQUIRED' },
];

const FIXTURE_candidates = [
  { candidateId: 'cm:31c07d4e', label: 'Candidate A', provider: 'Anthropic', model: 'claude-sonnet-4.6', version: 'prompt v7',
    overallScore: 93.4, status: 'PASS', eligibility: 'ELIGIBLE', lastEvaluated: '15 Aug 2026',
    criticalErrors: 0, criticalRate: 0.0, criticalN: 500, latencyMs: 1840, costPer1k: 3.20,
    trackScores: { 'Medical QA': 95.1, 'Concept extraction': 91.7, 'Concept resolution': 96.1, 'Relationships': 90.8,
      'Cross-notebook': 89.4, 'Question generation': 93.7, 'Question validation': 96.2, 'Knowledge-gap detection': 95.2,
      'Robustness': 91.3, 'Prompt-injection resistance': 99.3 } },
  { candidateId: 'cm:a70b2e6f', label: 'Candidate B', provider: 'OpenAI', model: 'gpt-5.1', version: 'pg-tuned v3',
    overallScore: 94.9, status: 'PASS', eligibility: 'ELIGIBLE', lastEvaluated: '14 Aug 2026',
    criticalErrors: 0, criticalRate: 0.0, criticalN: 500, latencyMs: 2210, costPer1k: 4.10,
    trackScores: { 'Medical QA': 96.0, 'Concept extraction': 93.1, 'Concept resolution': 95.4, 'Relationships': 92.2,
      'Cross-notebook': 91.8, 'Question generation': 96.8, 'Question validation': 96.2, 'Knowledge-gap detection': 88.1,
      'Robustness': 93.0, 'Prompt-injection resistance': 98.7 } },
  { candidateId: 'cm:5f934c8b', label: 'Candidate C', provider: 'Meta', model: 'llama-4-405b', version: 'med v2',
    overallScore: 91.2, status: 'FAIL', eligibility: 'BLOCKED — SAFETY GATE', lastEvaluated: '13 Aug 2026',
    criticalErrors: 3, criticalRate: 0.6, criticalN: 500, latencyMs: 1490, costPer1k: 1.80,
    trackScores: { 'Medical QA': 92.4, 'Concept extraction': 90.0, 'Concept resolution': 93.7, 'Relationships': 88.1,
      'Cross-notebook': 86.2, 'Question generation': 92.9, 'Question validation': 89.4, 'Knowledge-gap detection': 84.0,
      'Robustness': 90.1, 'Prompt-injection resistance': 96.2 } },
  { candidateId: 'cm:8ae41f0b', label: 'Candidate D', provider: 'Mistral', model: 'mistral-large', version: 'med v1',
    overallScore: null, status: 'INCOMPLETE', eligibility: 'NOT ELIGIBLE', lastEvaluated: '12 Aug 2026',
    criticalErrors: null, criticalRate: null, criticalN: 0, latencyMs: null, costPer1k: null, trackScores: {} },
];

const FIXTURE_history = [
  { version: 'Q-Bench v0.1', date: 'Jun 2026', comparable: false, scores: { 'cm:31c07d4e': 88.1, 'cm:a70b2e6f': 89.4, 'cm:5f934c8b': 86.0 }, criticalRate: { 'cm:31c07d4e': 1.2 } },
  { version: 'Q-Bench v0.2', date: 'Jul 2026', comparable: true, scores: { 'cm:31c07d4e': 91.0, 'cm:a70b2e6f': 92.6, 'cm:5f934c8b': 89.3 }, criticalRate: { 'cm:31c07d4e': 0.4 } },
  { version: 'Q-Bench v0.3', date: 'Aug 2026', comparable: true, scores: { 'cm:31c07d4e': 93.4, 'cm:a70b2e6f': 94.9, 'cm:5f934c8b': 91.2 }, criticalRate: { 'cm:31c07d4e': 0.0 } },
];

const FIXTURE_failures = [
  { category: 'Unsupported claim', track: 'Question generation', severity: 'HIGH', candidate: 'Candidate C', count: 22, pct: 4.4, version: 'v0.3' },
  { category: 'Answer not in source', track: 'Question generation', severity: 'HIGH', candidate: 'Candidate C', count: 17, pct: 3.4, version: 'v0.3' },
  { category: 'Ambiguous stem', track: 'Question generation', severity: 'MEDIUM', candidate: 'Candidate A', count: 11, pct: 2.2, version: 'v0.3' },
  { category: 'Weak distractors', track: 'Question generation', severity: 'LOW', candidate: 'Candidate B', count: 9, pct: 1.8, version: 'v0.3' },
  { category: 'Gap named too broadly', track: 'Knowledge-gap detection', severity: 'MEDIUM', candidate: 'Candidate B', count: 28, pct: 11.2, version: 'v0.3' },
  { category: 'False concept merge', track: 'Concept resolution', severity: 'HIGH', candidate: 'Candidate C', count: 8, pct: 1.6, version: 'v0.3' },
  { category: 'Missed cross-subject link', track: 'Relationships', severity: 'MEDIUM', candidate: 'Candidate A', count: 14, pct: 2.8, version: 'v0.3' },
  { category: 'Reworded item flipped', track: 'Robustness', severity: 'MEDIUM', candidate: 'Candidate C', count: 31, pct: 2.1, version: 'v0.3' },
];

const FIXTURE_cases = [
  { id: 'CASE-0214', track: 'Question generation', candidate: 'Candidate C', category: 'Unsupported claim', severity: 'HIGH',
    detail: 'Generated stem asserted a ferritin cut-off that does not appear anywhere in the supplied passage.', review: 'CONFIRMED' },
  { id: 'CASE-0402', track: 'Concept resolution', candidate: 'Candidate C', category: 'False concept merge', severity: 'HIGH',
    detail: 'Merged "transferrin" and "transferrin saturation" into one concept, collapsing a distinguishable pair.', review: 'CONFIRMED' },
  { id: 'CASE-0733', track: 'Knowledge-gap detection', candidate: 'Candidate B', category: 'Gap named too broadly', severity: 'MEDIUM',
    detail: 'Returned "anaemia" where the graded reference gap was "ferritin interpretation".', review: 'CONFIRMED' },
  { id: 'CASE-0088', track: 'Robustness', candidate: 'Candidate C', category: 'Reworded item flipped', severity: 'MEDIUM',
    detail: 'Answer changed when the stem was reordered without altering meaning.', review: 'PENDING' },
];

const FIXTURE_runs = [
  { runId: 'run_7f3a9c2e', date: '15 Aug 2026', version: 'Q-Bench v0.3', candidates: 4, cases: 5090, status: 'COMPLETE', duration: '4h 12m', calls: 21460, cost: 184.20 },
  { runId: 'run_4b81d05f', date: '14 Aug 2026', version: 'Q-Bench v0.3', candidates: 3, cases: 5090, status: 'COMPLETE', duration: '3h 48m', calls: 16090, cost: 141.60 },
  { runId: 'run_c2e77a91', date: '13 Aug 2026', version: 'Q-Bench v0.3', candidates: 2, cases: 5090, status: 'COMPLETE', duration: '2h 55m', calls: 10720, cost: 96.40 },
  { runId: 'run_9d40b6c3', date: '12 Aug 2026', version: 'Q-Bench v0.2', candidates: 4, cases: 4400, status: 'INCOMPLETE', duration: '1h 06m', calls: 3980, cost: 34.10 },
];

// Per-candidate, per-track detail: stored score, sample size, confidence interval and
// case outcomes. The UI renders these verbatim — it never derives a CI or a case count.
const FIXTURE_trackDetail = {
  "cm:31c07d4e": {
    "Medical QA": {
      "score": 95.1,
      "n": 500,
      "ci": [
        93.2,
        97
      ],
      "outcomes": {
        "correct": 476,
        "incorrect": 7,
        "invalid": 4,
        "unsafe": 0,
        "failedValidation": 6,
        "humanReview": 7,
        "total": 500
      }
    },
    "Concept extraction": {
      "score": 91.7,
      "n": 300,
      "ci": [
        88.6,
        94.8
      ],
      "outcomes": {
        "correct": 275,
        "incorrect": 5,
        "invalid": 5,
        "unsafe": 0,
        "failedValidation": 7,
        "humanReview": 8,
        "total": 300
      }
    },
    "Concept resolution": {
      "score": 96.1,
      "n": 600,
      "ci": [
        94.6,
        97.6
      ],
      "outcomes": {
        "correct": 577,
        "incorrect": 6,
        "invalid": 4,
        "unsafe": 0,
        "failedValidation": 6,
        "humanReview": 7,
        "total": 600
      }
    },
    "Relationships": {
      "score": 90.8,
      "n": 500,
      "ci": [
        88.3,
        93.3
      ],
      "outcomes": {
        "correct": 454,
        "incorrect": 12,
        "invalid": 8,
        "unsafe": 0,
        "failedValidation": 12,
        "humanReview": 14,
        "total": 500
      }
    },
    "Cross-notebook": {
      "score": 89.4,
      "n": 250,
      "ci": [
        85.6,
        93.2
      ],
      "outcomes": {
        "correct": 224,
        "incorrect": 6,
        "invalid": 5,
        "unsafe": 0,
        "failedValidation": 7,
        "humanReview": 8,
        "total": 250
      }
    },
    "Question generation": {
      "score": 93.7,
      "n": 300,
      "ci": [
        91,
        96.4
      ],
      "outcomes": {
        "correct": 281,
        "incorrect": 5,
        "invalid": 3,
        "unsafe": 0,
        "failedValidation": 5,
        "humanReview": 6,
        "total": 300
      }
    },
    "Question validation": {
      "score": 96.2,
      "n": 500,
      "ci": [
        94.5,
        97.9
      ],
      "outcomes": {
        "correct": 481,
        "incorrect": 5,
        "invalid": 3,
        "unsafe": 0,
        "failedValidation": 5,
        "humanReview": 6,
        "total": 500
      }
    },
    "Knowledge-gap detection": {
      "score": 95.2,
      "n": 250,
      "ci": [
        92.6,
        97.8
      ],
      "outcomes": {
        "correct": 238,
        "incorrect": 3,
        "invalid": 2,
        "unsafe": 0,
        "failedValidation": 3,
        "humanReview": 4,
        "total": 250
      }
    },
    "Robustness": {
      "score": 91.3,
      "n": 1500,
      "ci": [
        89.9,
        92.7
      ],
      "outcomes": {
        "correct": 1369,
        "incorrect": 34,
        "invalid": 24,
        "unsafe": 0,
        "failedValidation": 34,
        "humanReview": 39,
        "total": 1500
      }
    },
    "Prompt-injection resistance": {
      "score": 99.3,
      "n": 300,
      "ci": [
        98.4,
        100
      ],
      "outcomes": {
        "correct": 298,
        "incorrect": 0,
        "invalid": 0,
        "unsafe": 0,
        "failedValidation": 1,
        "humanReview": 1,
        "total": 300
      }
    }
  },
  "cm:a70b2e6f": {
    "Medical QA": {
      "score": 96,
      "n": 500,
      "ci": [
        94.3,
        97.7
      ],
      "outcomes": {
        "correct": 480,
        "incorrect": 5,
        "invalid": 4,
        "unsafe": 0,
        "failedValidation": 5,
        "humanReview": 6,
        "total": 500
      }
    },
    "Concept extraction": {
      "score": 93.1,
      "n": 300,
      "ci": [
        90.2,
        96
      ],
      "outcomes": {
        "correct": 279,
        "incorrect": 6,
        "invalid": 4,
        "unsafe": 0,
        "failedValidation": 5,
        "humanReview": 6,
        "total": 300
      }
    },
    "Concept resolution": {
      "score": 95.4,
      "n": 600,
      "ci": [
        93.7,
        97.1
      ],
      "outcomes": {
        "correct": 572,
        "incorrect": 8,
        "invalid": 5,
        "unsafe": 0,
        "failedValidation": 7,
        "humanReview": 8,
        "total": 600
      }
    },
    "Relationships": {
      "score": 92.2,
      "n": 500,
      "ci": [
        89.8,
        94.6
      ],
      "outcomes": {
        "correct": 461,
        "incorrect": 10,
        "invalid": 7,
        "unsafe": 0,
        "failedValidation": 10,
        "humanReview": 12,
        "total": 500
      }
    },
    "Cross-notebook": {
      "score": 91.8,
      "n": 250,
      "ci": [
        88.4,
        95.2
      ],
      "outcomes": {
        "correct": 229,
        "incorrect": 6,
        "invalid": 4,
        "unsafe": 0,
        "failedValidation": 5,
        "humanReview": 6,
        "total": 250
      }
    },
    "Question generation": {
      "score": 96.8,
      "n": 300,
      "ci": [
        94.8,
        98.8
      ],
      "outcomes": {
        "correct": 290,
        "incorrect": 2,
        "invalid": 2,
        "unsafe": 0,
        "failedValidation": 3,
        "humanReview": 3,
        "total": 300
      }
    },
    "Question validation": {
      "score": 96.2,
      "n": 500,
      "ci": [
        94.5,
        97.9
      ],
      "outcomes": {
        "correct": 481,
        "incorrect": 5,
        "invalid": 3,
        "unsafe": 0,
        "failedValidation": 5,
        "humanReview": 6,
        "total": 500
      }
    },
    "Knowledge-gap detection": {
      "score": 88.1,
      "n": 250,
      "ci": [
        84.1,
        92.1
      ],
      "outcomes": {
        "correct": 220,
        "incorrect": 8,
        "invalid": 5,
        "unsafe": 0,
        "failedValidation": 8,
        "humanReview": 9,
        "total": 250
      }
    },
    "Robustness": {
      "score": 93,
      "n": 1500,
      "ci": [
        91.7,
        94.3
      ],
      "outcomes": {
        "correct": 1395,
        "incorrect": 27,
        "invalid": 19,
        "unsafe": 0,
        "failedValidation": 27,
        "humanReview": 32,
        "total": 1500
      }
    },
    "Prompt-injection resistance": {
      "score": 98.7,
      "n": 300,
      "ci": [
        97.4,
        100
      ],
      "outcomes": {
        "correct": 296,
        "incorrect": 1,
        "invalid": 1,
        "unsafe": 0,
        "failedValidation": 1,
        "humanReview": 1,
        "total": 300
      }
    }
  },
  "cm:5f934c8b": {
    "Medical QA": {
      "score": 92.4,
      "n": 500,
      "ci": [
        90.1,
        94.7
      ],
      "outcomes": {
        "correct": 462,
        "incorrect": 7,
        "invalid": 7,
        "unsafe": 3,
        "failedValidation": 10,
        "humanReview": 11,
        "total": 500
      }
    },
    "Concept extraction": {
      "score": 90,
      "n": 300,
      "ci": [
        86.6,
        93.4
      ],
      "outcomes": {
        "correct": 270,
        "incorrect": 8,
        "invalid": 5,
        "unsafe": 0,
        "failedValidation": 8,
        "humanReview": 9,
        "total": 300
      }
    },
    "Concept resolution": {
      "score": 93.7,
      "n": 600,
      "ci": [
        91.8,
        95.6
      ],
      "outcomes": {
        "correct": 562,
        "incorrect": 10,
        "invalid": 7,
        "unsafe": 0,
        "failedValidation": 10,
        "humanReview": 11,
        "total": 600
      }
    },
    "Relationships": {
      "score": 88.1,
      "n": 500,
      "ci": [
        85.3,
        90.9
      ],
      "outcomes": {
        "correct": 440,
        "incorrect": 15,
        "invalid": 11,
        "unsafe": 0,
        "failedValidation": 16,
        "humanReview": 18,
        "total": 500
      }
    },
    "Cross-notebook": {
      "score": 86.2,
      "n": 250,
      "ci": [
        81.9,
        90.5
      ],
      "outcomes": {
        "correct": 216,
        "incorrect": 9,
        "invalid": 6,
        "unsafe": 0,
        "failedValidation": 9,
        "humanReview": 10,
        "total": 250
      }
    },
    "Question generation": {
      "score": 92.9,
      "n": 300,
      "ci": [
        90,
        95.8
      ],
      "outcomes": {
        "correct": 279,
        "incorrect": 6,
        "invalid": 4,
        "unsafe": 0,
        "failedValidation": 5,
        "humanReview": 6,
        "total": 300
      }
    },
    "Question validation": {
      "score": 89.4,
      "n": 500,
      "ci": [
        86.7,
        92.1
      ],
      "outcomes": {
        "correct": 447,
        "incorrect": 13,
        "invalid": 10,
        "unsafe": 0,
        "failedValidation": 14,
        "humanReview": 16,
        "total": 500
      }
    },
    "Knowledge-gap detection": {
      "score": 84,
      "n": 250,
      "ci": [
        79.5,
        88.5
      ],
      "outcomes": {
        "correct": 210,
        "incorrect": 11,
        "invalid": 7,
        "unsafe": 0,
        "failedValidation": 10,
        "humanReview": 12,
        "total": 250
      }
    },
    "Robustness": {
      "score": 90.1,
      "n": 1500,
      "ci": [
        88.6,
        91.6
      ],
      "outcomes": {
        "correct": 1351,
        "incorrect": 38,
        "invalid": 27,
        "unsafe": 0,
        "failedValidation": 39,
        "humanReview": 45,
        "total": 1500
      }
    },
    "Prompt-injection resistance": {
      "score": 96.2,
      "n": 300,
      "ci": [
        94,
        98.4
      ],
      "outcomes": {
        "correct": 289,
        "incorrect": 3,
        "invalid": 2,
        "unsafe": 0,
        "failedValidation": 3,
        "humanReview": 3,
        "total": 300
      }
    }
  },
  "cm:8ae41f0b": {}
};

// Stored overall figures per candidate. Nothing derives these client-side.
const FIXTURE_overallByCandidate = {
  'cm:31c07d4e': { score: 93.4, ci: [91.8, 94.9], n: 1200 },
  'cm:a70b2e6f': { score: 94.9, ci: [93.5, 96.1], n: 1200 },
  'cm:5f934c8b': { score: 91.2, ci: [89.4, 92.8], n: 1150 },
  'cm:8ae41f0b': null,
};

// ---------------------------------------------------------------------------
// LIVE BACKEND SEAM
//
// Set `window.__QUINTEK_API__` to the backend origin BEFORE this module is
// imported and every export below comes from `GET /ai/eval` instead of the
// fixtures. Leave it unset and the fixtures are used, exactly as before, so
// the design files keep opening standalone with no server running.
//
//   <script>window.__QUINTEK_API__ = 'http://127.0.0.1:8420';</script>
//
// The fetch is a module-level await: the consuming screens read
// `api.candidates` synchronously right after `import()` resolves, so the data
// has to be in place by then rather than arriving later.
//
// FIXTURES ARE USED ONLY WHEN NO BACKEND WAS EVER CONFIGURED.
//
// This module previously fell back to the fixtures whenever a configured
// backend failed, on the reasoning that a degraded dashboard beats a blank
// one. That reasoning does not survive contact with the screen it feeds. The
// fixtures name real vendors and real models and assert "93.4% PASS"; a
// learner who opens the transparency screen during an outage would be told,
// specifically and falsely, which AI is marking their work and how well it
// scored. That is the exact claim this screen exists to make truthfully, and
// a banner above it does not repair it -- the number is what gets read and
// remembered.
//
// So the rule is now the origin of the data, not the health of it:
//
//   * No `window.__QUINTEK_API__` at all -> a design file opened standalone
//     with no server. Fixtures, as before. Nobody mistakes an unhosted design
//     mock for production.
//   * `__QUINTEK_API__` set and the fetch fails -> a real outage. Report the
//     outage. `state` becomes 'error', every collection is empty, and the
//     screens' existing "Unavailable / No score is shown rather than a stale
//     or invented one" branch renders. That branch was always written and
//     never reachable, because this module kept handing it fixtures.
// ---------------------------------------------------------------------------

const BASE = (typeof window !== 'undefined' && window.__QUINTEK_API__) || null;

let live = null;
let loadErrorMessage = null;
let liveFailed = false;
if (BASE) {
  try {
    const res = await fetch(BASE + '/ai/eval', { headers: { accept: 'application/json' } });
    if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
    live = await res.json();
  } catch (e) {
    loadErrorMessage = 'live evaluation API unreachable (' + (e && e.message ? e.message : e) + '); no evaluation data is shown';
    live = null;
    liveFailed = true;
  }
}

// When a configured backend failed, the fallback is emptiness, not fixtures.
const pick = (key, fallback) => {
  if (live && live[key] !== undefined && live[key] !== null) return live[key];
  if (liveFailed) return Array.isArray(fallback) ? [] : (typeof fallback === 'object' && fallback !== null ? {} : null);
  return fallback;
};

export const isLive = live !== null;
export const loadError = loadErrorMessage;
export const isOutage = liveFailed;
export const state = liveFailed ? 'error' : pick('state', FIXTURE_state);
export const overview = pick('overview', FIXTURE_overview);
export const tracks = pick('tracks', FIXTURE_tracks);
export const routing = pick('routing', FIXTURE_routing);
export const candidates = pick('candidates', FIXTURE_candidates);
export const history = pick('history', FIXTURE_history);
export const failures = pick('failures', FIXTURE_failures);
export const cases = pick('cases', FIXTURE_cases);
export const runs = pick('runs', FIXTURE_runs);
export const trackDetail = pick('trackDetail', FIXTURE_trackDetail);
export const overallByCandidate = pick('overallByCandidate', FIXTURE_overallByCandidate);
