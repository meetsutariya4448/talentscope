// Run one stage at a time, not an unattended multi-stage ramp to a fixed
// peak — the point of this test is to find where this specific machine
// saturates, watching resource signals between stages, and stop there
// rather than push past it. See evals/load-test.md for the actual results
// and k6/run-stage.sh for how each stage is invoked.
//
// Usage:
//   k6 run --env VUS=10 --env DURATION=30s k6/load_test.js
//
// Traffic mix approximates real usage: search (fts/vector/hybrid) is what
// a user actually does most; analytics endpoints are viewed less
// frequently (e.g. a dashboard load, not every interaction). /qa/ask is
// deliberately excluded from this mix — it calls the real Groq API, and a
// load test that hits an external LLM provider's rate limits would be
// measuring Groq's capacity, not this system's. It's tested separately,
// lightly, in evals/load-test.md's RAG section.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const VUS = parseInt(__ENV.VUS || '5', 10);
const DURATION = __ENV.DURATION || '30s';

export const options = {
  vus: VUS,
  duration: DURATION,
  thresholds: {
    // Not pass/fail gates that abort the run — k6 keeps going regardless
    // (no abortOnFail) — just a marker in the summary for "did this stage
    // already look unacceptable."
    http_req_failed: ['rate<0.05'],
    http_req_duration: ['p(95)<2000'],
  },
};

const errorRate = new Rate('errors');
const searchDuration = new Trend('search_duration', true);
const analyticsDuration = new Trend('analytics_duration', true);

const SEARCH_QUERIES = [
  'python backend engineer',
  'kubernetes distributed systems',
  'machine learning python',
  'react typescript frontend',
  'data engineer spark kafka',
  'golang microservices',
  'aws terraform devops',
  'postgresql database engineer',
];

function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

export default function () {
  const roll = Math.random();

  if (roll < 0.35) {
    // Full-text search — the default/cheapest mode
    const q = pick(SEARCH_QUERIES);
    const res = http.get(`${BASE_URL}/postings/?q=${encodeURIComponent(q)}&mode=fts&page_size=20`, { tags: { name: 'search_fts' } });
    searchDuration.add(res.timings.duration);
    errorRate.add(res.status !== 200);
    check(res, { 'fts: status 200': (r) => r.status === 200 });
  } else if (roll < 0.55) {
    // Vector search — CPU-bound, encodes the query text on every request
    const q = pick(SEARCH_QUERIES);
    const res = http.get(`${BASE_URL}/postings/?q=${encodeURIComponent(q)}&mode=vector&page_size=20`, { tags: { name: 'search_vector' } });
    searchDuration.add(res.timings.duration);
    errorRate.add(res.status !== 200);
    check(res, { 'vector: status 200': (r) => r.status === 200 });
  } else if (roll < 0.75) {
    // Hybrid search — the default the dashboard actually uses; both FTS
    // and vector sub-queries plus RRF fusion
    const q = pick(SEARCH_QUERIES);
    const res = http.get(`${BASE_URL}/postings/?q=${encodeURIComponent(q)}&mode=hybrid&page_size=20`, { tags: { name: 'search_hybrid' } });
    searchDuration.add(res.timings.duration);
    errorRate.add(res.status !== 200);
    check(res, { 'hybrid: status 200': (r) => r.status === 200 });
  } else if (roll < 0.85) {
    const res = http.get(`${BASE_URL}/analytics/skill-demand?window=30d`, { tags: { name: 'analytics_skill_demand' } });
    analyticsDuration.add(res.timings.duration);
    errorRate.add(res.status !== 200);
    check(res, { 'skill-demand: status 200': (r) => r.status === 200 });
  } else if (roll < 0.93) {
    const res = http.get(`${BASE_URL}/analytics/top-companies`, { tags: { name: 'analytics_top_companies' } });
    analyticsDuration.add(res.timings.duration);
    errorRate.add(res.status !== 200);
    check(res, { 'top-companies: status 200': (r) => r.status === 200 });
  } else {
    const res = http.get(`${BASE_URL}/postings/stats`, { tags: { name: 'postings_stats' } });
    analyticsDuration.add(res.timings.duration);
    errorRate.add(res.status !== 200);
    check(res, { 'stats: status 200': (r) => r.status === 200 });
  }

  sleep(Math.random() * 0.5 + 0.1); // 100-600ms "think time" between a real user's requests
}
