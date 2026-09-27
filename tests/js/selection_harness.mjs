// Minimal, dependency-free regression harness for the P1 "selection can
// point at a different market after sorting/filtering" bug fixed in
// static/index.html (Phase 1a, see docs/FRONTEND_REVIEW.md finding 1).
//
// It extracts the dashboard's inline <script> and runs it inside a Node
// `vm` context with a stubbed-out `document`/`fetch`/`setInterval` (no
// jsdom, no npm deps — just the Node standard library), then drives the
// real pairKey()/selectedPair() functions with a fake 2-row
// `displayedPairs`: select pair B, reverse the "sort" (by replacing
// displayedPairs with a differently-ordered array, exactly like renderPairs()
// does after a re-sort), and assert selectedPair() still resolves to B.
//
// Run with: node tests/js/selection_harness.mjs   (exits non-zero on failure)

import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const indexPath = path.join(__dirname, '..', '..', 'static', 'index.html');
const html = readFileSync(indexPath, 'utf8');

const scriptMatch = html.match(/<script>([\s\S]*)<\/script>/);
assert.ok(scriptMatch, 'could not find <script> block in static/index.html');
let scriptSrc = scriptMatch[1];

// Expose the internals we need to drive from outside the vm context. This
// closes over the script's own `let`-scoped variables (pairKey, selectedPair,
// displayedPairs, selectedKey), which otherwise aren't reachable as context
// properties the way top-level `function` declarations are.
scriptSrc += `
globalThis.__test = {
  pairKey,
  selectedPair,
  setDisplayedPairs: (v) => { displayedPairs = v; },
  getDisplayedPairs: () => displayedPairs,
  setSelectedKey: (v) => { selectedKey = v; },
  getSelectedKey: () => selectedKey,
};
`;

// ── Stub DOM / browser globals ──────────────────────────────────────────────
// Just enough for the script's top-level init code (checkStatus(),
// setCategory('all'), updateSortIndicators(), etc.) to run without throwing.
// None of it needs to actually render anything for this test.
function makeStubElement() {
  const el = {
    textContent: '',
    innerHTML: '',
    disabled: false,
    style: {},
    dataset: {},
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    appendChild() {},
    setAttribute() {},
  };
  return el;
}

const stubDocument = {
  getElementById() { return makeStubElement(); },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  addEventListener() {},
  activeElement: null,
};

const context = {
  document: stubDocument,
  window: undefined,
  console,
  fetch: async () => { throw new Error('network disabled in test harness'); },
  setInterval() { return 0; },
  setTimeout(fn) { return 0; }, // never fire — we don't need HELP/error auto-clear here
  alert() {},
  Date,
};
context.window = context;
vm.createContext(context);

vm.runInContext(scriptSrc, context, { filename: 'static/index.html (inline script)' });

const t = context.__test;
assert.ok(t, '__test hook was not installed — extraction of internals failed');

// ── The actual regression check ─────────────────────────────────────────────
const pairA = { kalshi_ticker: 'KALSHI-A', poly_token_id: 'POLY-A', poly_title: 'Pair A' };
const pairB = { kalshi_ticker: 'KALSHI-B', poly_token_id: 'POLY-B', poly_title: 'Pair B' };

// Initial render order: [A, B]. Select pair B (index 1, mirroring a click on
// the second row).
t.setDisplayedPairs([pairA, pairB]);
t.setSelectedKey(t.pairKey(pairB));
assert.equal(t.selectedPair(), pairB, 'sanity: selectedPair() should resolve to B before any re-sort');

// Simulate a sort reversal: renderPairs() rebuilds `displayedPairs` in a new
// order after every sort/filter change. The bug was that `selected` used to
// be the numeric index (1), which after this reversal would point at A
// instead of B.
t.setDisplayedPairs([pairB, pairA]);
assert.equal(t.selectedPair(), pairB,
  'FAIL (P1 regression): selectedPair() must still resolve to pair B after the sort reversed the array order');

// And if the selected pair is filtered out entirely, selectedPair() must
// return null (not silently fall back to whatever sits at the old index).
t.setDisplayedPairs([pairA]);
assert.equal(t.selectedPair(), null,
  'selectedPair() must return null when the selected pair has been filtered out of displayedPairs');

// poly_id fallback (fast/legacy scans without a CLOB token id) must still
// produce a stable, distinct key.
const pairC = { kalshi_ticker: 'KALSHI-C', poly_id: 'POLY-ID-C' };
const pairD = { kalshi_ticker: 'KALSHI-C', poly_id: 'POLY-ID-D' };
assert.notEqual(t.pairKey(pairC), t.pairKey(pairD), 'pairKey must distinguish pairs sharing a kalshi_ticker via the poly_id fallback');

console.log('selection_harness: all assertions passed (pairKey/selectedPair survive sort reversal and filtering).');
