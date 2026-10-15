// Check real answer rendering without model calls or database writes.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('examples/frontend/index.html', 'utf8');
const start = source.indexOf('function globalAnswerHtml(');
const end = source.indexOf('function globalStepBody(', start);
const context = vm.createContext({esc: value => String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')});
vm.runInContext(source.slice(start, end), context);
const url = '/api/v1/agent/conversations/b8d964cdc2e34b3c8a4bd2e8b2709fd8/reports/bdd37d782192_%E9%85%8D';
for (const answer of [url, `报告链接：${url}`, `[报告](${url})`, url.replace('_', '\\_')]) {
  const html = context.globalAnswerHtml(answer);
  assert.ok(html.includes(`href="${url}"`));
  assert.ok(html.includes('查看报告 ↗</a>'));
  assert.ok(!html.includes(`>${url}<`));
}
assert.ok(!context.globalAnswerHtml('[坏链接](javascript:alert(1))').includes('<a '));
assert.ok(!context.globalAnswerHtml('<img src=x onerror=alert(1)>').includes('<img'));
assert.ok(context.globalAnswerHtml('**核心结论**\n- 总计 5').includes('<strong>核心结论</strong>'));
console.log('Report answer links: passed');
vm.runInContext(source.slice(source.indexOf('function answerTimestampHtml('), source.indexOf('async function api(')), context);
assert.ok(context.answerTimestampHtml({completed_at:'2026-10-15T01:55:00Z'}).includes('2026-10-15T01:55:00.000Z'));
assert.equal(context.answerTimestampHtml({completed_at:'invalid'}), '');
vm.runInContext(source.match(/^function renderMessage\(message\).*$/m)[0], context);
for (const name of ['第一轮.xlsx', '第二轮.xlsx']) {
  const html=context.renderMessage({role:'ai',content:'结果',meta:{attachments:[{display_name:name,version:7}],timing:{completed_at:'2026-10-15T01:55:00Z'}}});
  assert.ok(html.includes(name));
  assert.ok(!html.includes('v7'));
  assert.ok(html.indexOf(name)<html.indexOf('<time '));
  assert.equal((html.match(/<footer/g)||[]).length,1);
}
console.log('Per-answer attachments without versions, timestamp on the right: passed');
for (const role of ['user', 'ai']) {
  const html = context.renderMessage({role, content:'测试内容'});
  assert.ok(html.includes(role === 'user' ? 'message-user' : 'message-assistant'));
  assert.ok(!html.includes('message-label'));
  assert.ok(!html.includes('assistant-mark'));
}
assert.ok(!source.includes('<div class="message-label">'));
const styles = fs.readFileSync('examples/frontend/attachments.css', 'utf8');
assert.ok(!styles.includes("content:'你'"));
console.log('Message alignment without visible role labels: passed');
assert.ok(!source.includes('authSettings'));
assert.ok(!source.includes('authHeaders'));
assert.ok(!source.includes('excel-api-key'));
console.log('No connection credential UI or request headers: passed');
