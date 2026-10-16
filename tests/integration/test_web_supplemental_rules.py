"""P2 management and read-only stock panels keep their boundaries visible."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Company, Listing, Market

STATIC = Path("src/investment_agent/web/static")


def _login(client) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def test_management_page_has_structured_controls_and_p2_boundary(api_client) -> None:
    _login(api_client)
    response = api_client.get("/app/supplemental-rules")
    assert response.status_code == 200
    assert 'id="supplemental-rules-root"' in response.text
    assert 'id="supplemental-rule-form"' in response.text
    assert 'id="supplemental-event-form"' in response.text
    assert "不会改变基础分数" in response.text
    assert "不会标为“已核验”" in response.text
    assert "/app/static/pages/supplemental_rules.css" in response.text
    assert "/app/static/app.js" in response.text


@pytest.mark.asyncio
async def test_stock_page_uses_read_only_supplemental_panel_without_p1_changes(api_client) -> None:
    _login(api_client)
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        session.add(Company(id=700406, canonical_name="Synthetic stock"))
        session.add(Listing(id=600406, company_id=700406, market_code="SSE", ticker="600406", is_primary=True))
        await session.flush()
    response = api_client.get("/app/stocks/600406.SH")
    assert response.status_code == 200
    assert 'id="supplemental-items-root"' in response.text
    assert '/app/static/pages/supplemental_rules.css' in response.text
    assert 'data-context="company_research"' in response.text
    assert "不改变评分、账户适配或研究结论" in response.text
    assert "/app/static/app.js" in response.text


def test_management_module_import_and_request_recovery_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    module = (STATIC / "supplemental_rules.js").resolve().as_uri()
    script = f"""
import assert from 'node:assert/strict';
const ui = await import({json.dumps(module)});
const store = {{ value: null, setItem(key, value) {{ this.value = String(value); }}, getItem() {{ return this.value; }}, removeItem() {{ this.value = null; }} }};
const body = {{ idempotency_key: 'same-operation', definition: {{name:'synthetic'}} }};
let calls = 0;
await assert.rejects(ui.submitWithRecovery({{path:'/supplemental-rules', body, operation:'create', storage:store, send:async () => {{ calls += 1; throw new Error('dropped response'); }}}}), /dropped response/);
assert.equal(calls, 1);
const pending = JSON.parse(store.value);
assert.equal(pending.phase, 'uncertain');
assert.deepEqual(pending.body, body);
let replay;
const result = await ui.retryPending({{storage:store, send:async (path, options) => {{ replay = {{path,body:options.body}}; return {{ok:true}}; }}}});
assert.equal(replay.path, '/supplemental-rules');
assert.deepEqual(replay.body, body, 'lost-response retry must send the exact same request body');
assert.equal(result.record.body.idempotency_key, body.idempotency_key);
assert.equal(JSON.parse(store.value).phase, 'accepted');
assert.equal(ui.clearAcceptedPending(store), null);
assert.equal(store.value, null);
const acceptedStore = {{ value: null, setItem(key, value) {{ this.value = String(value); }}, getItem() {{ return this.value; }}, removeItem() {{ throw new Error('cleanup unavailable'); }} }};
const acceptedResult = {{run_ref: {{kind:'pipeline_run', id: 42, revision: 1}}, source_manifest_hash: 'a'.repeat(64)}};
let acceptedCalls = 0;
const accepted = await ui.submitWithRecovery({{path:'/supplemental-rules/42/decisions', body, operation:'approve', storage:acceptedStore, send:async () => {{ acceptedCalls += 1; return acceptedResult; }}}});
assert.equal(accepted.result, acceptedResult, 'accepted response must remain available after cleanup storage failure');
assert.equal(JSON.parse(acceptedStore.value).phase, 'accepted');
assert.deepEqual(JSON.parse(acceptedStore.value).result, acceptedResult);
assert.match(ui.clearAcceptedPending(acceptedStore), /服务器已接收/);
const acceptedRecovery = await ui.retryPending({{storage:acceptedStore, send:async () => {{ throw new Error('accepted requests must never be resent in memory'); }}}});
assert.equal(acceptedRecovery.accepted, true);
assert.equal(acceptedRecovery.result, acceptedResult);
assert.equal(acceptedCalls, 1);
const writeFailBody = {{idempotency_key:'accepted-write-failure', definition:{{name:'synthetic'}}}};
const writeFailStore = {{
  value:null, writes:0,
  setItem(key, value) {{ this.writes += 1; if (this.writes > 1) throw new Error('quota exceeded'); this.value = String(value); }},
  getItem() {{ return this.value; }}, removeItem() {{ this.value = null; }},
}};
const writeFailResult = {{run_ref: {{kind:'pipeline_run', id:43, revision:1}}}};
const writeFail = await ui.submitWithRecovery({{path:'/research-events', body:writeFailBody, operation:'event report', storage:writeFailStore, send:async () => writeFailResult}});
assert.equal(writeFail.result, writeFailResult, 'accepted receipt must remain in memory when writing its post-success copy fails');
assert.match(writeFail.recoveryWarning, /无法保存回执/);
let resendCount = 0;
const writeFailRetry = await ui.retryPending({{storage:writeFailStore, send:async () => {{ resendCount += 1; throw new Error('known accepted command must not resend'); }}}});
assert.equal(writeFailRetry.result, writeFailResult);
assert.equal(resendCount, 0);
ui.clearAcceptedPending(writeFailStore);
const recoveryPending = {{phase:'prepared', operation:'event report', path:'/research-events', body:{{idempotency_key:'recovery-key', event:{{title:'x'}}}}}};
const exactRun = {{run_ref:{{kind:'pipeline_run', id:44, revision:1}}, operation:'event report', path:'/research-events', body:{{event:{{title:'x'}},idempotency_key:'recovery-key'}}, operation_id:'recovery-key', source_manifest_hash:'a'.repeat(64)}};
assert.equal(ui.findTrackedRunForPending(recoveryPending, [exactRun]), exactRun, 'matching durable run proves this prepared command was already accepted');
assert.equal(ui.findTrackedRunForPending(recoveryPending, [{{...exactRun, operation:'rule approval'}}]), null, 'recovery must not cross operation kinds');
assert.equal(ui.findTrackedRunForPending(recoveryPending, [{{...exactRun, path:'/research-events/other'}}]), null, 'recovery must not cross command targets');
assert.equal(ui.findTrackedRunForPending(recoveryPending, [{{...exactRun, body:{{...exactRun.body,event:{{title:'changed'}}}}}}]), null, 'recovery must not cross bodies');
assert.equal(ui.findTrackedRunForPending(recoveryPending, [{{...exactRun, operation_id:'other-key'}}]), null, 'recovery must not cross operation IDs');
const runView = {{operation_id:'recovery-key', manifest_hash:'a'.repeat(64)}};
assert.equal(ui.matchesTrackedRunIdentity(runView, exactRun), true, 'server readback must confirm the exact operation and manifest');
assert.equal(ui.matchesTrackedRunIdentity({{...runView, operation_id:'other-key'}}, exactRun), false, 'server readback must reject a different operation');
assert.equal(ui.matchesTrackedRunIdentity({{...runView, manifest_hash:'b'.repeat(64)}}, exactRun), false, 'server readback must reject a different manifest');
const progress = ui.summarizeRunProgress({{
  status:'partial', completed_count:3, declared_count:8, coverage_complete:false, failure_code:'item_failures',
  members:[
    {{ordinal:0,state:'pending'}}, {{ordinal:1,state:'pending'}}, {{ordinal:2,state:'saved'}},
    {{ordinal:3,state:'item_failed'}}, {{ordinal:4,state:'pending'}}, {{ordinal:5,state:'pending'}},
  ],
  scan_diagnostics:['rule_scan_truncated'], unbound_event_refs:[{{kind:'research_event',id:'e-1',revision:2}}],
}});
assert.equal(progress.terminal, true);
assert.deepEqual(progress.pending, ['0–1','4–5']);
assert.deepEqual(progress.failed, ['3']);
assert.equal(progress.failureCode, 'item_failures');
assert.match(progress.diagnostics.join(' '), /rule_scan_truncated/);
assert.match(progress.diagnostics.join(' '), /research_event:e-1@2/);
const blocked = {{ setItem() {{ throw new Error('storage unavailable'); }}, getItem() {{ return null; }} }};
await assert.rejects(ui.submitWithRecovery({{path:'/supplemental-rules', body, operation:'create', storage:blocked, send:async () => {{ throw new Error('must never send'); }}}}), /storage unavailable/);
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_ui_does_not_use_shared_api_client_or_full_json_editor() -> None:
    source = (STATIC / "supplemental_rules.js").read_text(encoding="utf-8")
    template = Path("src/investment_agent/web/templates/supplemental_rules.html").read_text(encoding="utf-8")
    assert 'from "./api.js"' not in source
    assert 'name="definition_json"' not in template
    assert 'type="checkbox" name="effect"' in template
