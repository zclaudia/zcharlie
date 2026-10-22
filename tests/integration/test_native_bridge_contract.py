"""Exercise shipped JS against old/new APK command surfaces, without Android."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path("src/investment_agent/web/static").resolve()


def run_node(source):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    result = subprocess.run(
        [node, "--input-type=module", "-e", source], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


def test_capability_negotiation_and_legacy_probing():
    uri = json.dumps((STATIC / "native_bridge.js").as_uri())
    run_node(f"""
import assert from 'node:assert/strict';
const uri = {uri};
let calls = 0;
globalThis.__TAURI_INTERNALS__ = {{invoke: async () => {{ calls++; throw 'Command bridge_capabilities not found'; }} }};
const old = await import(uri + '?old');
assert.equal(await old.supportsNative('proxy_request'), true);
assert.equal(await old.supportsNative('stream_task_events'), true);
assert.equal(await old.supportsNative('future_camera_command'), false);
assert.equal(calls, 1);
old.disableNativeCommand('proxy_request');
assert.equal(await old.supportsNative('proxy_request'), false);
globalThis.__TAURI_INTERNALS__.invoke = async () => ({{bridge_version:1, capabilities:['proxy_request']}});
const modern = await import(uri + '?modern');
assert.equal(await modern.supportsNative('proxy_request'), true);
assert.equal(await modern.supportsNative('stream_task_events'), false);
globalThis.__TAURI_INTERNALS__.invoke = async () => ({{bridge_version:2, capabilities:['proxy_request']}});
assert.equal(await (await import(uri + '?future')).supportsNative('proxy_request'), false);
globalThis.__TAURI_INTERNALS__.invoke = async () => null;
assert.equal(await (await import(uri + '?malformed')).supportsNative('proxy_request'), false);
delete globalThis.__TAURI_INTERNALS__;
assert.equal(await (await import(uri + '?browser')).supportsNative('proxy_request'), false);
""")


@pytest.mark.parametrize("surface", ["modern", "legacy", "scheme", "uncertain"])
def test_writes_use_supported_transport_without_replaying_uncertain_results(surface):
    api_uri = json.dumps((STATIC / "api.js").as_uri())
    run_node(f"""
import assert from 'node:assert/strict';
import {{webcrypto}} from 'node:crypto';
Object.defineProperty(globalThis, 'crypto', {{value:webcrypto}});
globalThis.document = {{body:{{dataset:{{embedded:'android'}}}}}};
let nativeWrites = 0, fetched = 0;
const surface = {json.dumps(surface)};
globalThis.__TAURI_INTERNALS__ = {{ invoke: async (command, args) => {{
  if (command === 'bridge_capabilities') {{
    if (surface !== 'modern' && surface !== 'uncertain') throw 'Command bridge_capabilities not found';
    return {{bridge_version:1, capabilities:['proxy_request']}};
  }}
  if (surface === 'scheme') throw 'Command proxy_request not found';
  nativeWrites++;
  assert.equal(JSON.parse(args.body).text, '你好');
  assert.ok(args.idempotencyKey);
  if (surface === 'uncertain') throw 'gateway_unreachable';
  return {{status:200,content_type:'application/json',body:'{{"ok":true}}'}};
}} }};
globalThis.fetch = async (path, options) => {{
  fetched++;
  assert.equal(JSON.parse(Buffer.from(options.headers['X-Zcharlie-Body'], 'base64').toString()).text, '你好');
  return new Response('{{"ok":true}}', {{headers:{{'content-type':'application/json'}}}});
}};
const {{post}} = await import({api_uri});
const {{refreshBlockers}} = await import({json.dumps((STATIC / "refresh_state.js").as_uri())});
if (surface === 'uncertain') {{
  await assert.rejects(post('/example', {{text:'你好'}}), /结果尚待确认/);
  assert.equal(nativeWrites, 1); assert.equal(fetched, 0);
}} else {{
  assert.deepEqual(await post('/example', {{text:'你好'}}), {{ok:true}});
  assert.equal(fetched, surface === 'scheme' ? 1 : 0);
}}
assert.deepEqual(refreshBlockers(), []);
""")
