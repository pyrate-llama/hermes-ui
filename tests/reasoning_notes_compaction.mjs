#!/usr/bin/env node
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const html = fs.readFileSync(new URL('../hermes-ui.html', import.meta.url), 'utf8');
const start = html.indexOf('const MAX_REASONING_STORED_CHARS');
const end = html.indexOf('function TasksView', start);
assert.ok(start > 0 && end > start, 'reasoning helpers not found');

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(
  html.slice(start, end) + '\nthis.appendReasoningText = appendReasoningText; this.formatReasoningTextForRender = formatReasoningTextForRender; this.MAX_REASONING_STORED_CHARS = MAX_REASONING_STORED_CHARS; this.MAX_REASONING_RENDER_CHARS = MAX_REASONING_RENDER_CHARS;',
  sandbox,
);

let text = '';
for (let i = 1; i <= 300; i += 1) {
  const cumulative = Array.from({ length: i }, (_, j) => `step-${j + 1}`).join(' ');
  text = sandbox.appendReasoningText(text, cumulative, sandbox.MAX_REASONING_STORED_CHARS);
}
assert.ok(text.includes('step-300'), 'latest cumulative reasoning should be retained');
assert.ok(text.length <= sandbox.MAX_REASONING_STORED_CHARS, 'stored reasoning should be capped');
assert.ok(!/step-299 step-1/.test(text), 'cumulative snapshots should not be appended wholesale');

let deltaText = '';
for (let i = 1; i <= 10; i += 1) {
  deltaText = sandbox.appendReasoningText(deltaText, `token${i}`, sandbox.MAX_REASONING_STORED_CHARS);
}
assert.equal(deltaText, 'token1 token2 token3 token4 token5 token6 token7 token8 token9 token10');

const rendered = sandbox.formatReasoningTextForRender('x'.repeat(sandbox.MAX_REASONING_RENDER_CHARS + 500));
assert.ok(rendered.length < sandbox.MAX_REASONING_RENDER_CHARS + 200);
assert.ok(rendered.startsWith('[showing latest reasoning notes; earlier notes were compacted]'));

console.log('Reasoning notes compaction tests passed');
