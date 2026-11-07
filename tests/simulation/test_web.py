"""Simulation shell authentication, CSP and exact browser formatting contracts."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.script = None
        self.inline_scripts = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.tags.append((tag, values))
        if tag == "script":
            self.script = values

    def handle_data(self, data):
        if self.script is not None and not self.script.get("src") and data.strip():
            self.inline_scripts.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.script = None


def login(client):
    return client.post("/app/login", data={"api_key": "secret", "next": "/app/simulations"})


def test_simulation_pages_require_session_and_never_render_credentials(api_client):
    for path in ("/app/simulations", "/app/simulations/experiment-demo"):
        response = api_client.get(path, follow_redirects=False)
        assert response.status_code == 302
        assert "/app/login" in response.headers["location"]
    login(api_client)
    response = api_client.get("/app/simulations")
    assert response.status_code == 200
    assert 'id="sim-create-form"' in response.text
    assert 'href="/app/simulations" class="active"' in response.text
    assert "secret" not in response.text
    assert "model_snapshot" not in response.text
    detail = api_client.get("/app/simulations/experiment-demo")
    assert detail.status_code == 200
    assert 'data-experiment-id="experiment-demo"' in detail.text
    for text in (
        "净值与排名",
        "账户与持仓",
        "决策与证据",
        "订单与成交",
        "复盘与成本",
        "资料与运行",
        "导入交易证据",
    ):
        assert text in detail.text
    assert api_client.get("/app/simulations/invalid%20id").status_code == 400


@pytest.mark.parametrize("path", ["/app/simulations", "/app/simulations/experiment-demo"])
def test_simulation_templates_respect_csp_and_accessibility(api_client, path):
    login(api_client)
    response = api_client.get(path)
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "style-src 'self'" in response.headers["content-security-policy"]
    markup = Markup()
    markup.feed(response.text)
    assert not markup.inline_scripts
    assert not [
        (tag, attrs)
        for tag, attrs in markup.tags
        if "style" in attrs or any(key.startswith("on") for key in attrs)
    ]
    assert any(
        tag == "script" and "simulations.js" in attrs.get("src", "") for tag, attrs in markup.tags
    )
    assert any(
        tag == "link" and "simulations.css" in attrs.get("href", "") for tag, attrs in markup.tags
    )
    ids = [attrs["id"] for _, attrs in markup.tags if "id" in attrs]
    assert len(ids) == len(set(ids))
    for _, attrs in markup.tags:
        if attrs.get("role") == "tab":
            assert attrs["aria-controls"] in ids
            assert attrs["aria-selected"] in {"true", "false"}


def test_simulation_assets_are_served_and_decimal_display_is_exact(api_client):
    assert api_client.get("/app/static/simulations.js").status_code == 200
    assert api_client.get("/app/static/simulations.css").status_code == 200
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    module = Path("src/investment_agent/web/static/simulations.js").resolve().as_uri()
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            f"""
import assert from 'node:assert/strict';
const {{decimalText,percentText}} = await import({json.dumps(module)});
assert.equal(decimalText('1000000.000000000000000001'),'1,000,000.000000000000000001');
assert.equal(decimalText('1e-18'),'0.000000000000000001');
assert.equal(decimalText('-0'),'0');
assert.equal(percentText('0.000000000000000001'),'0.0000000000000001%');
assert.equal(percentText(null),'不可计算');
assert.equal(decimalText('1e999999999'),'数值超出展示范围');
console.log('exact simulation strings verified');
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_browser_fixture_seed_is_real_http_safe_and_has_all_display_states(
    api_client, tmp_path, monkeypatch
):
    from datetime import UTC, datetime

    from investment_agent.api.auth import get_settings
    from investment_agent.api.routes import simulation as api
    from investment_agent.config import Settings
    from investment_agent.scheduler.trading_calendar import load_trading_calendar
    from investment_agent.storage.database import session_scope
    from tests.browser.simulations_fixture_server import CATALOG, seed

    settings = Settings(
        _env_file=None, api_key="secret", data_dir=tmp_path, llm_catalog=CATALOG, model_slots="{}"
    )
    calendar = load_trading_calendar(Path("config/calendars/a_share.yaml"))
    clock = [datetime(2026, 11, 9, 8, tzinfo=UTC)]
    monkeypatch.setattr(api, "_now", lambda: clock[0])
    monkeypatch.setattr(api, "_calendar", lambda _: calendar)
    api_client.app.dependency_overrides[get_settings] = lambda: settings
    async with session_scope() as session:
        identities = await seed(session, tmp_path, calendar, settings, clock)
    login(api_client)
    base = f"/api/simulations/{identities['demo_id']}"
    detail = api_client.get(base)
    assert detail.status_code == 200, detail.text
    assert len(detail.json()["participants"]) == 4
    assert detail.json()["rounds"][0]["status"] == "settled"
    assert len(api_client.get(f"{base}/fills").json()) == 3
    assert any(item["status"] == "observing" for item in api_client.get(f"{base}/reviews").json())
    costs = api_client.get(f"{base}/costs").json()["records"]
    assert any(item["estimated"] for item in costs)
    assert any(item["status"] == "unknown" for item in costs)
    availability = api_client.get(f"{base}/availability?trade_date=2026-11-10")
    assert availability.status_code == 200 and availability.json()["ready"]
    blocked = api_client.get(
        f"/api/simulations/{identities['waiting_id']}/availability?trade_date=2026-11-09"
    )
    assert blocked.status_code == 200 and not blocked.json()["ready"]
    assert any(round_["missing"] for round_ in blocked.json()["rounds"])
    exported = api_client.get(f"{base}/export")
    assert exported.status_code == 200
    for forbidden in (
        "synthetic-model-key",
        "simulation-fixture.invalid",
        "model_snapshot",
        "artifact_path",
    ):
        assert forbidden not in exported.text
    api_client.app.dependency_overrides.clear()


def test_record_table_builds_realistic_dom_without_chaining_append_return():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    module = Path("src/investment_agent/web/static/simulations.js").resolve().as_uri()
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            f"""
import assert from 'node:assert/strict';
class FakeNode {{
  constructor(tag) {{this.tag=tag;this.children=[];this.attrs={{}};this.textContent='';}}
  setAttribute(k,v) {{this.attrs[k]=v;}}
  append(...nodes) {{this.children.push(...nodes);}} // Native append returns undefined.
}}
class Cell extends FakeNode {{}}
globalThis.Node=FakeNode;
globalThis.HTMLTableCellElement=Cell;
globalThis.document={{querySelector:()=>null,createElement:tag=>tag==='td'?new Cell(tag):new FakeNode(tag),createTextNode:text=>{{const n=new FakeNode('#text');n.textContent=text;return n;}}}};
const {{table}} = await import({json.dumps(module)});
const wrap=table(['参赛者','精确金额'],[['模型 A','1000000.000000000000000001']]);
const actual=wrap.children[0];
assert.equal(actual.tag,'table');assert.equal(actual.children[0].tag,'thead');
assert.equal(actual.children[0].children[0].children.length,2);
assert.equal(actual.children[1].children[0].children[1].children[0].textContent,'1000000.000000000000000001');
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def run_node(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def dom_harness(*, saved=None, status=200):
    module = Path("src/investment_agent/web/static/simulations.js").resolve().as_uri()
    return f"""
import assert from 'node:assert/strict';
class FakeNode {{
  constructor() {{this.children=[];this.dataset={{}};this.handlers={{}};this.disabled=false;this.value='';}}
  append(...items) {{this.children.push(...items);}}
  replaceChildren(...items) {{this.children=items;}}
  setAttribute(key,value) {{this[key]=String(value);}}
  addEventListener(name,handler) {{this.handlers[name]=handler;}}
  querySelectorAll() {{return [];}}
}}
const nodes=new Map();
function node(key) {{if(!nodes.has(key))nodes.set(key,new FakeNode());return nodes.get(key);}}
globalThis.document={{body:{{dataset:{{}}}},hidden:false,
  querySelector:key=>key==='#simulations-workbench'?null:node(key),querySelectorAll:()=>[],
  createElement:()=>new FakeNode(),createTextNode:text=>text,addEventListener:()=>{{}}}};
const saved={json.dumps(saved)};
let persisted=saved ? JSON.stringify(saved) : null;
globalThis.sessionStorage={{getItem:()=>persisted,setItem:(_,v)=>persisted=v,removeItem:()=>persisted=null}};
globalThis.setTimeout=()=>0;globalThis.clearTimeout=()=>{{}};
const writes=[],reads=[];
globalThis.fetch=async(path,options)=>{{
  if(options.method==='POST'){{writes.push({{path,body:JSON.parse(options.body),headers:options.headers}});
    if({status}===0)throw Error('response lost');
    return new Response(JSON.stringify({status}>=400 ? {{detail:{{code:'revision_conflict',message:'Revision changed'}}}} : {{id:'result'}}),
      {{status:{status}||200,headers:{{'content-type':'application/json'}}}});}}
  reads.push(path);
  if(path.includes('/options/stocks'))return new Response('[]',{{headers:{{'content-type':'application/json'}}}});
  if(path.includes('/availability?'))return new Response('{{}}',{{headers:{{'content-type':'application/json'}}}});
  return new Promise(()=>{{}});
}};
const {{bootSimulations}}=await import({json.dumps(module)});
const root=new FakeNode();root.dataset={{view:'detail',experimentId:'experiment-b'}};
node('#sim-availability-form').elements={{namedItem:()=>({{value:'2026-11-09'}})}};
node('#sim-evidence-form').elements={{namedItem:()=>({{value:'',checked:false}})}};
bootSimulations(root);
const flush=async()=>{{for(let i=0;i<60;i++)await Promise.resolve();}};
"""


def test_availability_submit_captures_date_before_event_current_target_is_cleared():
    run_node(
        dom_harness()
        + """
const form=node('#sim-availability-form');
const event={currentTarget:form,preventDefault(){}};
form.handlers.submit(event);
event.currentTarget=null; // Actual DOM dispatch clears currentTarget after returning.
await flush();
assert.ok(reads.includes('/api/simulations/experiment-b/availability?trade_date=2026-11-09'));
"""
    )


def test_pending_request_cannot_cross_experiment_pages_or_escape_write_routes():
    pending = {
        "path": "/api/simulations/experiment-a/pause",
        "body": {"expected_revision": 3, "idempotency_key": "original-key"},
        "description": "暂停实验",
    }
    run_node(
        dom_harness(saved=pending)
        + """
await flush();
assert.equal(node('#sim-retry-write').disabled,true);
node('#sim-retry-write').handlers.click();await flush(); // Even programmatic dispatch cannot bypass scope.
assert.equal(writes.length,0);assert.notEqual(persisted,null);
assert.ok(node('#sim-pending-text').children.some(item=>item?.href==='/app/simulations/experiment-a'));
"""
    )
    module = Path("src/investment_agent/web/static/simulations.js").resolve().as_uri()
    run_node(f"""
import assert from 'node:assert/strict';
const {{pendingDestination,pendingMatchesView,availableActions}}=await import({json.dumps(module)});
const original={json.dumps(pending)};
assert.equal(pendingMatchesView(original,'detail','experiment-a'),true);
assert.equal(pendingMatchesView(original,'detail','experiment-b'),false);
for(const path of ['/api/simulations/../../settings','/api/simulations/a/../pause','/api/simulations/../pause','/api/simulations/./pause',
  '/api/simulations/a/participants/b/corporate-actions','/api/simulations/a%2Fb/pause','https://evil.test/api/simulations/a/pause'])
  assert.equal(pendingDestination({{...original,path}}),null);
assert.equal(pendingMatchesView({{...original,path:'/api/simulations'}},'list'),true);
assert.equal(pendingMatchesView({{...original,path:'/api/simulations'}},'detail','experiment-a'),false);
const ended={{status:'completed',config:{{end_date:'2026-11-10'}}}};
assert.equal(availableActions(ended,[],'2026-11-11')['data-refresh'],false);
assert.equal(availableActions(ended,[{{status:'locked'}}],'2026-11-11')['data-refresh'],true);
assert.equal(availableActions({{...ended,status:'paused'}},[],'2026-11-11').start,false);
assert.equal(availableActions({{...ended,status:'paused'}},[],'2026-11-09')['run-now'],false);
""")


@pytest.mark.parametrize(
    "status,retained",
    [(200, False), (0, True), (503, True), (401, True), (409, False), (403, False)],
)
def test_original_request_replay_preserves_body_and_handles_confirmed_rejections(status, retained):
    pending = {
        "path": "/api/simulations/experiment-b/pause",
        "body": {"expected_revision": 3, "idempotency_key": "original-key"},
        "description": "暂停实验",
    }
    run_node(
        dom_harness(saved=pending, status=status)
        + f"""
node('#sim-retry-write').handlers.click();await flush();
assert.equal(writes.length,1);
assert.equal(writes[0].path,saved.path);assert.deepEqual(writes[0].body,saved.body);
assert.equal('Idempotency-Key' in writes[0].headers,false);
assert.equal(persisted!==null,{str(retained).lower()});
"""
    )
