import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { bootResearchEntry, renderResearchEntryResult } from '../../src/investment_agent/web/static/research_entry.js';

const html = fs.readFileSync(new URL('../../src/investment_agent/web/templates/_research_entry.html', import.meta.url), 'utf8');
const fixture = JSON.parse(fs.readFileSync(new URL('../fixtures/next_iterations/contracts/g0.json', import.meta.url), 'utf8')).preflight;
const identity = {ref:fixture.request.listing_ref, name:'合成公司 <img onerror=alert(1)>', ticker:'600036.SH'};
const task = {id:'00000000-0000-0000-0000-000000000100', status:'queued', stop_reason:null, artifact_refs:[]};
const flush = () => new Promise(resolve => setImmediate(resolve));
async function until(check) { for (let i=0; i<40; i++) { if (check()) return; await flush(); } assert.fail('component did not settle'); }
function mount() {
  document.body.innerHTML = html;
  const root = document.querySelector('#research-entry');
  const field = name => root.querySelector(`[data-entry-${name}]`);
  return {root, field};
}
function memory() {
  const map = new Map();
  return {getItem:key=>map.get(key)||null, setItem:(key,value)=>map.set(key,value)};
}
async function selectAndPreflight(field) {
  field('search').value = '合成';
  field('search-form').dispatchEvent(new Event('submit', {cancelable:true}));
  await until(()=>field('matches').querySelector('button'));
  field('matches').querySelector('button').click();
  field('preflight').click(); await until(()=>!field('coverage').hidden);
}

test('only explicit company research submits; lost acknowledgment reuses exact conversation and message', async () => {
  const storage = memory(), writes = [];
  let lost = true;
  const read = async path => path.includes('?q=') ? [identity] : identity;
  const write = async (path, body) => {
    writes.push({path, body:structuredClone(body)});
    if (path.endsWith('/preflight')) return fixture.response;
    if (path === '/agent/conversations') return {id:'original-conversation'};
    if (lost) { lost=false; throw new Error('lost ack'); }
    return {task};
  };
  let {root, field} = mount();
  bootResearchEntry(root, {get:read, post:write, storage});
  await selectAndPreflight(field);
  assert.equal(writes.length, 1); // preflight itself never starts a task
  assert.equal(field('submit').disabled, true);
  assert.match(root.textContent, /已核验/);
  assert.equal(root.querySelectorAll('img').length, 0); // source identity is untrusted text
  field('consent').checked = true; field('consent').dispatchEvent(new Event('change'));
  field('submit').click(); await until(()=>!field('retry').hidden && !field('retry').disabled);
  const original = writes.at(-1);
  assert.equal(original.path, '/agent/conversations/original-conversation/messages');
  assert.equal(original.body.research_kind, 'company_research');
  assert.ok(original.body.client_message_id);
  assert.equal('idempotency_key' in original.body, false);
  assert.equal(field('question').disabled, true);
  // A page reload recovers the exact unresolved operation before enabling edits.
  ({root, field} = mount());
  bootResearchEntry(root, {get:read, post:write, storage});
  field('retry').click(); await until(()=>!field('result').hidden);
  assert.deepEqual(writes.at(-1), original);
  assert.equal(writes.filter(call=>call.path === '/agent/conversations').length, 1);
  assert.match(field('result').textContent, /尚未完成/);
  assert.match(field('result').querySelector('a').href, /conversation=original-conversation/);
});

test('editing invalidates preflight, missing model blocks send, and stale preflight cannot authorize it', async () => {
  const {root, field} = mount(); let resolvePreflight;
  const writes = [];
  bootResearchEntry(root, {storage:memory(), get:async path=>path.includes('?q=') ? [identity] : identity,
    post:async (path, body)=> { writes.push({path,body}); return new Promise(resolve=>resolvePreflight=resolve); }});
  field('search').value='合成'; field('search-form').dispatchEvent(new Event('submit', {cancelable:true}));
  await until(()=>field('matches').querySelector('button')); field('matches').querySelector('button').click();
  field('preflight').click(); await until(()=>resolvePreflight);
  // Dispatching a programmatic edit also invalidates in-flight checks.
  field('question').value='另一个问题'; field('question').dispatchEvent(new Event('input'));
  resolvePreflight(fixture.response); await flush(); await flush();
  assert.equal(field('coverage').hidden, true); assert.equal(field('submit').disabled, true);
  field('preflight').click(); await flush(); await flush();
  resolvePreflight({...fixture.response, model:{availability:'unavailable', reason:'未配置模型'}});
  await until(()=>!field('coverage').hidden);
  field('consent').checked=true; field('consent').dispatchEvent(new Event('change'));
  assert.equal(field('submit').disabled, true);
  assert.match(root.textContent, /未配置模型/);
  assert.equal(writes.filter(call=>!call.path.endsWith('/preflight')).length, 0);
});

test('result explanations retain real failure, gaps and counter references', () => {
  const root = document.createElement('section');
  renderResearchEntryResult(root, {...task, status:'partial', stop_reason:'evidence_blocked'},
    [{progress:{inferences:['现金来源尚待核对'], gaps:['下一步核查现金流附注'], counter_refs:[], stopped_because:'缺原件'}}]);
  assert.match(root.textContent, /证据不足/); assert.match(root.textContent, /现金流附注/);
  assert.match(root.textContent, /缺原件/); assert.match(root.textContent, /无引用不表示不存在反证/);
  renderResearchEntryResult(root, {...task, status:'failed', stop_reason:'budget_exhausted'});
  assert.match(root.textContent, /未形成正式观点/); assert.match(root.textContent, /额度已用尽/);
  renderResearchEntryResult(root, {...task, status:'succeeded', stop_reason:'answered', artifact_refs:[{kind:'recommendation'}]});
  assert.match(root.textContent, /有正式观点/);
});

test('lost acknowledgment followed by retry 404 and reload retains the original operation', async () => {
  const storage = memory(), messages = [];
  let attempts = 0;
  const read = async path => path.includes('?q=') ? [identity] : identity;
  const write = async (path, body) => {
    if (path.endsWith('/preflight')) return fixture.response;
    if (path === '/agent/conversations') return {id:'uncertain-conversation'};
    messages.push({path,body:structuredClone(body)});
    attempts++;
    if (attempts === 1) throw new Error('lost acknowledgment after possible commit');
    if (attempts === 2) throw Object.assign(new Error('temporary 404'), {status:404});
    return {task};
  };
  let {root,field} = mount();
  bootResearchEntry(root,{storage,get:read,post:write});
  await selectAndPreflight(field);
  field('consent').checked=true; field('consent').dispatchEvent(new Event('change'));
  field('submit').click(); await until(()=>!field('retry').hidden && !field('retry').disabled);
  const original = messages[0];
  field('retry').click(); await until(()=>!field('retry').disabled);
  assert.equal(field('retry').hidden,false);
  assert.equal(field('question').disabled,true);
  assert.equal(field('submit').disabled,true);
  ({root,field}=mount());
  bootResearchEntry(root,{storage,get:read,post:write});
  assert.equal(field('question').disabled,true);
  field('retry').click(); await until(()=>!field('result').hidden);
  assert.equal(messages.length,3);
  for (const message of messages) assert.deepEqual(message,original);
  assert.match(field('result').querySelector('a').href,/conversation=uncertain-conversation/);
});
