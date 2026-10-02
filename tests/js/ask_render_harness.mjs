// Dependency-free harness for the Ask panel's safe markdown / citation
// rendering in static/index.html (Phase 3c). Loads the inline <script> in a
// Node vm with stubbed DOM (same approach as selection_harness.mjs) and
// drives renderAskMarkdown() / renderAskAnswer() with hostile input.
//
// Run with: node tests/js/ask_render_harness.mjs

import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(__dirname, '..', '..', 'static', 'index.html'), 'utf8');
const m = html.match(/<script>([\s\S]*)<\/script>/);
assert.ok(m, 'could not find <script> block');

function stubEl() {
  return {
    textContent: '', innerHTML: '', value: '', disabled: false, style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    appendChild() {}, setAttribute() {},
  };
}
const context = {
  document: {
    getElementById: stubEl, querySelectorAll() { return []; }, querySelector() { return null; },
    addEventListener() {}, activeElement: null, body: stubEl(),
  },
  console,
  fetch: async () => { throw new Error('network disabled in test harness'); },
  setInterval() { return 0; }, setTimeout() { return 0; }, alert() {}, Date,
};
context.window = context;
vm.createContext(context);
const origError = console.error;
console.error = () => {};   // swallow the expected "restore failed" noise
vm.runInContext(m[1], context, { filename: 'static/index.html (inline script)' });
console.error = origError;

const { renderAskMarkdown, renderAskAnswer } = context;
assert.equal(typeof renderAskMarkdown, 'function');
assert.equal(typeof renderAskAnswer, 'function');

const mkCites = list => {
  const c = { byId: new Map(), order: [] };
  list.forEach(x => c.byId.set(x, { id: x }));
  return c;
};

// 1. Raw HTML is escaped.
let out = renderAskMarkdown('hello <img src=x onerror=alert(1)> **b** <script>x</script>', mkCites([]), 'p');
assert.ok(!out.includes('<img'), 'raw <img leaked: ' + out);
assert.ok(!out.includes('<script'), 'raw <script leaked: ' + out);
assert.ok(out.includes('&lt;img src=x onerror=alert(1)&gt;'), out);
assert.ok(out.includes('<strong>b</strong>'), out);

// Hostile text inside code, tables, lists, headings-ish lines also escaped.
out = renderAskMarkdown('`<b>x</b>`\n\n```\n<img src=x onerror=1>\n```\n\n- <i>a</i>\n\n| h |\n|---|\n| <u>c</u> |', mkCites([]), 'p');
assert.ok(!/<(img|b|i|u)[ >]/.test(out), 'unescaped tag in: ' + out);

// Markdown links never become anchors.
out = renderAskMarkdown('[click](javascript:alert(1)) and [x](https://evil.example)', mkCites([]), 'p');
assert.ok(!out.includes('<a'), out);

// 2. Citation markers -> numbered sups, in order of first appearance; unknown stay plain.
const cites = mkCites(['b', 'a']);
out = renderAskMarkdown('First [^a] then [^b] then [^a] again and [^zzz].', cites, 'pre');
const sups = [...out.matchAll(/<sup><button[^>]*data-src="([^"]+)"[^>]*>\[(\d+)\]<\/button><\/sup>/g)];
assert.deepEqual(sups.map(s => s[2]), ['1', '2', '1']);
assert.deepEqual(sups.map(s => s[1]), ['pre-1', 'pre-2', 'pre-1']);
assert.ok(out.includes('[^zzz]'), 'unknown citation should render as plain text');
assert.deepEqual(cites.order, ['a', 'b']);

// Markers inside code spans are not converted.
out = renderAskMarkdown('`[^a]`', mkCites(['a']), 'p');
assert.ok(!out.includes('<sup>') && out.includes('<code>[^a]</code>'), out);

// 3. Rule citation URL handling.
function ruleHtml(source_url) {
  return renderAskAnswer({
    status: 'ok', answer_markdown: 'x [^r1]',
    citations: [{ id: 'r1', kind: 'rule', venue: 'kalshi', excerpt: '<img src=x onerror=alert(1)>', source_url }],
    tool_calls: [], scan: null, model: 'm',
  }, 0, Date.now());
}
for (const bad of ['javascript:alert(1)', 'https://evil.example', 'https://kalshi.com.evil.example/x', 'http://kalshi.com/x', ' https://kalshi.com/x', 'data:text/html,<script>1</script>']) {
  const h = ruleHtml(bad);
  if (bad === 'https://kalshi.com.evil.example/x') {
    // Prefix check includes the trailing slash, so this lookalike host must not link.
    assert.ok(!h.includes('<a '), 'lookalike host produced a link: ' + h);
  }
  assert.ok(!h.includes('<a '), 'unsafe URL produced a link: ' + bad + ' -> ' + h);
  assert.ok(!h.includes('<img'), 'excerpt not escaped for ' + bad);
}
for (const good of ['https://kalshi.com/markets/x', 'https://polymarket.com/event/y', 'https://www.kalshi.com/z']) {
  const h = ruleHtml(good);
  assert.ok(h.includes('<a href="' + good + '" rel="noopener noreferrer" target="_blank">open source</a>'), h);
}

// 4. Tables / lists / code / paragraphs render.
out = renderAskMarkdown('Intro line\nsecond line\n\n- one\n- two\n\n1. a\n2. b\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```\ncode <x>\n```', mkCites([]), 'p');
assert.ok(out.includes('<p>Intro line<br>second line</p>'), out);
assert.ok(out.includes('<ul><li>one</li><li>two</li></ul>'), out);
assert.ok(out.includes('<ol><li>a</li><li>b</li></ol>'), out);
assert.ok(out.includes('<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>'), out);
assert.ok(out.includes('<pre><code>code &lt;x&gt;</code></pre>'), out);

// 5. Status / provenance / tools.
const full = renderAskAnswer({
  status: 'stale', answer_markdown: 'a',
  citations: [{ id: 's', kind: 'scan', scan_id: 7 }, { id: 'p', kind: 'pair', pair_key: 'K|T' }],
  tool_calls: [{ name: 'q', ok: true, summary: 'ok' }, { name: 'bad', ok: false, summary: '<b>x</b>' }],
  scan: { id: 7, finished_at: new Date(Date.now() - 5 * 60000).toISOString(), mode: 'full' }, model: 'claude-x',
}, 3, Date.now());
assert.ok(full.includes('ask-pill stale') && full.includes('Stale data'));
assert.ok(full.includes('Based on scan #7 · 5m ago · full'));
assert.ok(full.includes('Tools used (2)') && full.includes('✗') && !full.includes('<b>x</b>'));
assert.ok(full.includes('(not in current scan)'), 'pair missing from allPairs should say so');
assert.ok(full.includes('scan #7'));

console.log('ask_render_harness: all assertions passed');
