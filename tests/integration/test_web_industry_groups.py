"""Industry-groups page (config-docs-plan §4.6, WP3): shell, auth, nav, the pure
helpers of industry-groups.js (version bump, reference counts, filtering, the
document diff) and the fake-DOM boot flow (edit → required note → PATCH with
the auto-bumped version → coverage quick-assign)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from investment_agent.web.static_assets import static_url

STATIC = Path("src/investment_agent/web/static")
DISCLAIMER = "研究辅助，非投资建议"


def _document() -> dict[str, Any]:
    """A small industry_groups document shaped like the baseline."""

    return {
        "version": "2026-10-07.1",
        "system": "csrc",
        "groups": {
            "banking": "Commercial banks.",
            "consumer": "Food, beverage, retail.",
            "other": "Mixed industries.",
        },
        "codes": {"J66": "banking", "C15": "consumer"},
        "legacy_name_rules": [{"contains": "银行业", "group": "banking"}],
        "shenwan": {
            "system": "sw2021",
            "level1": {"480000": "banking", "340000": "consumer"},
            "overrides": {"480100": "other"},
        },
    }


def _login(client: TestClient) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def _run_industry_groups(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    uri = (STATIC / "industry-groups.js").resolve().as_uri()
    source = f"const s = await import({json.dumps(uri)});\n{script}"
    result = subprocess.run(
        [node, "--input-type=module", "-e", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_node(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_industry_groups_page_requires_session(api_client: TestClient) -> None:
    response = api_client.get("/app/industry-groups", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app/login"


def test_industry_groups_page_shell(api_client: TestClient) -> None:
    _login(api_client)
    response = api_client.get("/app/industry-groups")
    assert response.status_code == 200
    html = response.text
    for fragment in (
        'id="ig-form"',
        'data-endpoint="/config-docs/industry_groups"',
        'id="ig-drift"',
        'id="ig-meta"',
        'id="ig-history"',
        'data-endpoint="/config-docs/industry_groups/history?limit=50"',
        'id="ig-baseline-drawer"',
        'id="ig-baseline-diff"',
        'id="ig-baseline-import"',
        'id="ig-baseline-note"',
        'id="ig-coverage"',
        'id="ig-coverage-run"',
        'id="ig-savebar"',
        'id="ig-dirty-count"',
        'id="ig-save"',
        'id="ig-discard"',
        'id="ig-note"',
        'id="ig-save-result"',
        'href="/app/industry-groups" class="active" aria-current="page"',
        'data-state="loading"',
        "行业分组映射",
        "覆盖率检查",
        "沪深300 成分全覆盖",
        "<noscript>",
        DISCLAIMER,
        static_url("app.js"),
        static_url("pages/industry-groups.css"),
    ):
        assert fragment in html, fragment
    # 变更说明必填（CD7：全部写操作带备注）。
    note_line = next(line for line in html.splitlines() if 'id="ig-note"' in line)
    assert "required" in note_line
    # CSP 基线：页面框架不写内联 style / <style>，脚本全部外链。
    assert " style=" not in html
    assert "<style" not in html
    for chunk in html.split("<script")[1:]:
        assert "src=" in chunk.split(">")[0]


def test_nav_links_industry_groups_on_every_page(api_client: TestClient) -> None:
    _login(api_client)
    for path in ("/app", "/app/watchlist", "/app/runs", "/app/health", "/app/settings"):
        html = api_client.get(path).text
        assert 'href="/app/industry-groups"' in html, path
        assert ">行业分组<" in html, path


def test_app_boots_the_industry_groups_module() -> None:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'from "./industry-groups.js"' in source
    assert "bootIndustryGroups()" in source and "INDUSTRY_GROUP_REGIONS" in source
    module = (STATIC / "industry-groups.js").read_text(encoding="utf-8")
    for endpoint in (
        "/config-docs/industry_groups",
        "/config-docs/industry_groups/history",
        "/config-docs/industry_groups/baseline-import",
        "/config-docs/industry-groups/coverage-check",
    ):
        assert endpoint in module, endpoint


def test_industry_groups_pure_helpers() -> None:
    out = _run_industry_groups(
        f"""
const doc = {json.dumps(_document(), ensure_ascii=False)};
const next = structuredClone(doc);
next.codes.J66 = "other";
next.groups.scratch = "临时";
delete next.shenwan.level1["340000"];
next.shenwan.overrides["630601"] = "consumer";
console.log(JSON.stringify({{
  level: [s.shenwanLevel("480000"), s.shenwanLevel("480100"), s.shenwanLevel("630601"), s.shenwanLevel("48"), s.shenwanLevel("480010")],
  version: [s.nextVersion("2026-10-07.1", "2026-11-01"), s.nextVersion("2026-11-01.3", "2026-11-01"), s.nextVersion("", "2026-11-01"), s.nextVersion("custom", "2026-11-01")],
  refs: Object.fromEntries(s.groupReferenceCounts(doc)),
  rows: {{
    all: s.codeRows(doc),
    search: s.codeRows(doc, {{ search: "j6" }}),
    group: s.codeRows(doc, {{ group: "consumer" }}),
    empty: s.codeRows(doc, {{ search: "zzz" }}),
  }},
  diff: s.diffIndustryDocuments(doc, next).map(s.diffEntryText),
  same: s.sameDoc(doc, structuredClone(doc)),
}}));
"""
    )
    assert out["level"] == [1, 2, 3, 0, 3]
    assert out["version"] == ["2026-11-01.1", "2026-11-01.4", "2026-11-01.1", "2026-11-01.1"]
    # banking 被 codes + legacy + level1 三处引用；consumer 被 codes + level1 两处；
    # other 只被覆写引用；scratch 无引用（不出现在计数里）。
    assert out["refs"] == {"banking": 3, "consumer": 2, "other": 1}
    assert out["rows"] == {
        "all": ["C15", "J66"],
        "search": ["J66"],
        "group": ["C15"],
        "empty": [],
    }
    assert out["diff"] == [
        "新增分组 scratch",
        "映射 J66：banking → other",
        "移除一级默认 340000（原 consumer）",
        "覆写 630601 → consumer（新增）",
    ]
    assert out["same"] is True


def test_industry_groups_boot_flow_edits_save_and_coverage() -> None:
    """用假 DOM + 假 fetch 驱动真实 industry-groups.js：加载 → 改映射（必填备注、
    自动 bump version、expected_revision）→ 覆盖率检查的快速指派 → 分组移除守卫。"""

    from tests.integration.test_web_client_contract import FAKE_DOM

    uri = (STATIC / "industry-groups.js").resolve().as_uri()
    script = (
        FAKE_DOM
        + f"""
const URI = {json.dumps(uri)};
const DOC = {json.dumps(_document(), ensure_ascii=False)};
class FocusNode extends FakeNode {{ focus() {{}} }}
const hosts = new Map();
const host = (id) => {{ if (!hosts.has(id)) hosts.set(id, new FocusNode('#' + id)); return hosts.get(id); }};
globalThis.document = {{
  body: null,
  createElement: (tag) => new FocusNode(tag),
  createTextNode: (text) => Object.assign(new FakeNode('#text'), {{ textContent: text }}),
  getElementById: (id) => host(id),
  querySelector: () => null,
  querySelectorAll: () => [],
}};
let revision = 4;
const calls = [];
const respond = (status, body) => ({{
  status, ok: status >= 200 && status < 300,
  headers: {{ get: () => 'application/json' }},
  json: async () => body, text: async () => JSON.stringify(body),
}});
const detail = (value, drifted) => ({{
  kind: 'industry_groups', title: '行业分组映射', value, baseline_value: DOC,
  revision, updated_at: '2026-11-01T01:02:03Z', updated_by: 'web', imported_at: null, drifted,
}});
const coverage = (ok) => ({{
  ok,
  unmapped_csrc: ok ? [] : [{{ code: 'J69', name: '其他金融业', tickers: ['600999.SH'] }}],
  missing_level1_defaults: [],
  invalid_overrides: [],
  member_count: 1, csrc_code_count: 1,
  summary: ok ? '覆盖完整。' : '覆盖不完整：1 个未显式映射。',
  as_of: '2026-11-01',
}});
globalThis.fetch = async (path, init = {{}}) => {{
  const method = init.method || 'GET';
  calls.push({{ method, path, body: init.body }});
  if (method === 'GET' && path === '/config-docs/industry_groups') return respond(200, detail(DOC, false));
  if (method === 'POST' && path === '/config-docs/industry-groups/coverage-check') {{
    // 覆盖结果由草稿推导：J69 未映射 → 不完整；快速指派后 → 完整。
    const draft = JSON.parse(init.body).value;
    return respond(200, coverage(draft.codes.J69 !== undefined));
  }}
  if (method === 'PATCH') {{
    const body = JSON.parse(init.body);
    if (body.expected_revision !== revision) return respond(409, {{detail: 'stale'}});
    revision += 1;
    return respond(200, detail(body.value, true));
  }}
  throw new Error('unexpected ' + method + ' ' + path);
}};
const m = await import(URI);
m.bootIndustryGroups();
await new Promise((r) => setTimeout(r, 20));
const form = host('ig-form');
const walk = (node) => {{ const out = []; for (const c of node.children) if (c instanceof FakeNode) out.push(...walk(c), c); return out; }};
const all = () => walk(form);
const byLabel = (label) => all().find((n) => n.getAttribute('aria-label') === label);
const buttonByText = (text, root) => (root ? walk(root) : all()).filter((n) => n.tag === 'button').find((n) => n.textContent === text);
const tick = () => new Promise((r) => setTimeout(r, 10));
const out = {{}};
out.sections = all().filter((n) => n.tag === 'h3').map((n) => n.textContent);
out.meta = host('ig-meta').textContent;
out.dirtyBefore = host('ig-dirty-count').textContent;
out.codeCells = all().filter((n) => n.getAttribute('data-label') === 'CSRC 码').map((n) => n.textContent);
// 1) 改映射：J66 → other。
const select = byLabel('J66 所属分组');
select.value = 'other';
select.dispatch('change');
out.dirtyAfterEdit = host('ig-dirty-count').textContent;
// 2) 未填备注 → 保存被拦截，不产生 PATCH。
host('ig-save').click();
await tick();
out.noteRequired = host('ig-save-result').text();
out.patchesBeforeNote = calls.filter((c) => c.method === 'PATCH').length;
// 3) 填备注保存：PATCH 全文档 + expected_revision + 自动 bump 的 version。
host('ig-note').value = 'J66 归入 other';
host('ig-save').click();
await tick();
const patch = JSON.parse(calls.find((c) => c.method === 'PATCH').body);
out.patch = {{ note: patch.note, expected_revision: patch.expected_revision, version: patch.value.version, j66: patch.value.codes.J66 }};
out.savedNotice = host('ig-save-result').text();
out.dirtyAfterSave = host('ig-dirty-count').textContent;
// 4) 覆盖率检查：当前文档缺 J69 → 快速指派回工作文档。
const runCoverage = host('ig-coverage-run');
runCoverage.click();
await tick();
out.coverageSummary = host('ig-coverage').text();
const coverageRegion = host('ig-coverage');
const assignSelect = walk(coverageRegion).find((n) => n.getAttribute('aria-label') === 'J69 指派分组');
assignSelect.value = 'other';
buttonByText('指派', coverageRegion).click();
await tick();
out.dirtyAfterAssign = host('ig-dirty-count').textContent;
out.coverageAfterAssign = host('ig-coverage').text();
// 5) 分组移除守卫：banking 仍有引用（legacy 规则 + level1 + override）→ 按钮禁用。
const bankingRow = all().find((n) => n.getAttribute('data-group') === 'banking');
const bankingRemove = bankingRow.children[3].children[0];
out.bankingRemove = [bankingRemove.disabled, bankingRemove.getAttribute('title')];
const otherRow = all().find((n) => n.getAttribute('data-group') === 'other');
const otherRemove = otherRow.children[3].children[0];
out.otherRemove = [otherRemove.disabled, otherRemove.getAttribute('title')];
console.log(JSON.stringify(out));
"""
    )
    out = _run_node(script)
    assert out["sections"] == [
        "分组定义",
        "映射表（CSRC 大类码 → 分组）",
        "申万 2021（先于 CSRC 表）",
    ]
    assert "版本 2026-10-07.1" in out["meta"] and "修订 4" in out["meta"]
    assert out["dirtyBefore"] == "没有未保存的修改"
    assert out["codeCells"] == ["C15", "J66"]
    assert out["dirtyAfterEdit"] == "有未保存的修改"
    assert "变更说明必填" in out["noteRequired"]
    assert out["patchesBeforeNote"] == 0
    # version 由 nextVersion 按（测试运行当日的）上海日期自动推进。
    expected_version = _run_industry_groups(
        "console.log(JSON.stringify(s.nextVersion('2026-10-07.1')));"
    )
    assert out["patch"] == {
        "note": "J66 归入 other",
        "expected_revision": 4,
        "version": expected_version,
        "j66": "other",
    }
    assert "已保存" in out["savedNotice"] and "立即" in out["savedNotice"]
    assert out["dirtyAfterSave"] == "没有未保存的修改"
    assert "覆盖不完整" in out["coverageSummary"] and "J69" in out["coverageSummary"]
    assert out["dirtyAfterAssign"] == "有未保存的修改"
    assert "覆盖完整" in out["coverageAfterAssign"]
    assert out["bankingRemove"][0] is True and "引用" in out["bankingRemove"][1]
    assert out["otherRemove"][0] is True and "必填分组" in out["otherRemove"][1]
