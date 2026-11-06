"""Settings page (settings-plan S5): shell, auth, nav and the pure helpers of settings.js."""

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
KEY = {"X-API-Key": "secret"}


def _login(client: TestClient) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def _run_settings(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    uri = (STATIC / "settings.js").resolve().as_uri()
    source = f"const s = await import({json.dumps(uri)});\n{script}"
    result = subprocess.run(
        [node, "--input-type=module", "-e", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_settings_page_requires_session(api_client: TestClient) -> None:
    response = api_client.get("/app/settings", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app/login"


def test_settings_page_shell(api_client: TestClient) -> None:
    _login(api_client)
    response = api_client.get("/app/settings")
    assert response.status_code == 200
    html = response.text
    for fragment in (
        'id="settings-form"',
        'data-endpoint="/settings"',
        'id="settings-encryption"',
        'id="settings-savebar"',
        'id="settings-dirty-count"',
        'id="settings-save"',
        'id="settings-discard"',
        'id="settings-note"',
        'id="settings-save-result"',
        'id="settings-history"',
        'data-endpoint="/settings/history?limit=30"',
        'id="settings-bootstrap"',
        'href="/app/settings" class="active" aria-current="page"',
        'data-state="loading"',
        "<noscript>",
        DISCLAIMER,
        static_url("app.js"),
    ):
        assert fragment in html, fragment


def test_nav_links_settings_on_every_page(api_client: TestClient) -> None:
    _login(api_client)
    for path in ("/app", "/app/watchlist", "/app/runs", "/app/candidates", "/app/health"):
        html = api_client.get(path).text
        assert 'href="/app/settings"' in html, path
        assert ">设置<" in html, path


def test_app_boots_the_settings_module() -> None:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'from "./settings.js"' in source
    assert "bootSettings()" in source and "SETTINGS_REGIONS" in source
    settings = (STATIC / "settings.js").read_text(encoding="utf-8")
    catalog = (STATIC / "settings_llm_catalog.js").read_text(encoding="utf-8")
    for endpoint in ("/settings", "/settings/history", "/settings/test/ifind"):
        assert endpoint in settings, endpoint
    assert "/settings/test/llm" in catalog


def test_settings_helpers() -> None:
    out = _run_settings(
        """
const int = {type: "int", min: 1, min_exclusive: false, max: 365, max_exclusive: false};
const rate = {type: "float", min: 0, min_exclusive: true, max: 600, max_exclusive: false};
console.log(JSON.stringify({
  bounds: [s.boundsText(int), s.boundsText(rate), s.boundsText({type: "text"})],
  ints: ["14", "0", "366", "1.5", "", "abc"].map((raw) => s.parseSettingInput(int, raw)),
  rates: ["0", "0.5", "600", "601"].map((raw) => s.parseSettingInput(rate, raw)),
  cron: ["", "0 20 * * sun", "0 20 * *"].map((raw) => s.parseSettingInput({type: "cron", nullable: true}, raw)),
  multi: [s.parseSettingInput({type: "multi", min_items: 1}, []),
          s.parseSettingInput({type: "multi", min_items: 1}, ["CSI300"])],
  select: s.parseSettingInput({type: "select", options: ["auto", "enabled"]}, "nope"),
  url: [s.parseSettingInput({type: "url"}, " http://h:7100 "), s.parseSettingInput({type: "url"}, "h")],
  warn: [s.cronWarning("0 20 * * 0"), s.cronWarning("0 20 * * sun"), s.cronWarning("*/30 * * * *")],
  values: [null, ["CSI300", "SSE50"], "", 3, true].map(s.formatSettingValue),
  issues: s.splitIssues(
    {message: "settings validation failed",
     issues: [{key: "candidate_expiry_days", messages: ["too small"]},
              {key: "schedule.snapshot", messages: ["bad universes"]}]},
    new Set(["candidate_expiry_days"]),
  ),
  secrets: [
    s.secretStatusText({configured: true, hint: "abcd"}),
    s.secretStatusText({configured: false}),
    s.secretStatusText({configured: false, env_fallback: true, hint: "wxyz"}),
    s.secretStatusText({configured: true, decrypt_error: "x"}),
  ],
  apply: [s.APPLY_LABELS.live, s.APPLY_LABELS.next_job, s.APPLY_LABELS.reschedule],
  same: [s.sameValue(["a"], ["a"]), s.sameValue(null, undefined), s.sameValue(1, 2)],
}));
"""
    )
    assert out["bounds"] == ["范围 [1, 365]", "范围 (0, 600]", ""]
    assert out["ints"] == [
        {"value": 14},
        {"error": "超出范围 [1, 365]"},
        {"error": "超出范围 [1, 365]"},
        {"error": "须为整数"},
        {"error": "必填"},
        {"error": "不是有效数字"},
    ]
    assert [("value" in item) for item in out["rates"]] == [False, True, True, False]
    assert out["cron"][0] == {"value": None} and out["cron"][1] == {"value": "0 20 * * sun"}
    assert "error" in out["cron"][2]
    assert "error" in out["multi"][0] and out["multi"][1] == {"value": ["CSI300"]}
    assert out["select"] == {"error": "无效选项"}
    assert out["url"][0] == {"value": "http://h:7100"} and "error" in out["url"][1]
    assert out["warn"][0] and out["warn"][1] == "" and out["warn"][2] == ""
    assert out["values"] == ["（停用 / 空）", "CSI300、SSE50", "（空）", "3", "是"]
    assert out["issues"] == {
        "byKey": {"candidate_expiry_days": ["too small"]},
        "general": ["schedule.snapshot：bad universes"],
    }
    assert out["secrets"][0] == "已设置 ···abcd" and out["secrets"][1] == "未设置"
    assert "wxyz" in out["secrets"][2] and "无法解密" in out["secrets"][3]
    assert out["apply"] == ["立即生效", "下个任务生效", "重新排期"]
    assert out["same"] == [True, True, False]


def test_page_input_hints_match_the_api(api_client: TestClient) -> None:
    """Every input type the API emits is one settings.js renders."""

    body = api_client.get("/settings", headers=KEY).json()
    types = {item["input"]["type"] for group in body["groups"] for item in group["items"]}
    source = (STATIC / "settings.js").read_text(encoding="utf-8")
    # text / url / timezone share the text input; secrets are keyed on item.secret.
    for kind in types - {"text", "url", "timezone", "secret"}:
        assert f'"{kind}"' in source, kind


def test_settings_catalog_covers_every_key(api_client: TestClient) -> None:
    body = api_client.get("/settings", headers=KEY).json()
    keys = {item["key"] for group in body["groups"] for item in group["items"]}
    covered = set(_run_settings(
        "const sections = s.sectionsFor(s.SETTINGS_SECTIONS, " + json.dumps(body["consumers"]) + ");\n"
        "console.log(JSON.stringify(s.settingsCatalogKeys(sections)));"
    ))
    assert keys == covered


def test_provider_editor_supports_lan_http_without_crypto_random_uuid() -> None:
    from tests.integration.test_web_client_contract import FAKE_DOM

    catalog_uri = (STATIC / "settings_llm_catalog.js").resolve().as_uri()
    out = _run_settings(FAKE_DOM + f"""
const {{webcrypto}} = await import('node:crypto');
Object.defineProperty(globalThis, 'crypto', {{configurable: true, value: {{
  getRandomValues: bytes => webcrypto.getRandomValues(bytes),
}}}});
globalThis.document = {{createElement: tag => new FakeNode(tag)}};
const {{catalogControl}} = await import({json.dumps(catalog_uri)});
const editor = catalogControl({{providers: []}}, true);
editor.root.dispatchEvent = () => true;
editor.root.children.find(node => node.textContent === '添加 Provider').click();
console.log(editor.read());
""")
    assert len(out["providers"]) == 1
    from uuid import UUID

    assert UUID(out["providers"][0]["id"]).version == 4
    assert out["providers"][0]["api_key"] is None


def test_review_panel_editor_serializes_the_server_shape() -> None:
    uri = (STATIC / "settings_review_panels.js").resolve().as_uri()
    catalog = {
        "providers": [
            {
                "id": "primary",
                "name": "Primary",
                "models": [
                    {
                        "id": "fast",
                        "name": "Fast",
                        "model": "fast",
                        "enabled": True,
                        "capabilities": ["tools", "json_object"],
                    },
                    {
                        "id": "nojson",
                        "name": "NoJson",
                        "model": "nojson",
                        "enabled": True,
                        "capabilities": ["tools"],
                    },
                ],
            }
        ]
    }
    lenses = [
        {"id": "value", "version": 1, "ref": "value.v1", "name": "价值视角"},
        {"id": "bear", "version": 1, "ref": "bear.v1", "name": "空头视角"},
    ]
    from tests.integration.test_web_client_contract import FAKE_DOM

    out = _run_settings(
        FAKE_DOM + f"""
globalThis.document = {{createElement: (tag) => new FakeNode(tag)}};
const rp = await import({json.dumps(uri)});
const document = {{
  panels: {{
    default: {{
      name: "默认评审组",
      seats: [
        {{seat_id: "bear", lens_id: "bear", model_ref: "primary/fast"}},
        {{seat_id: "value", lens_id: "value", model_ref: "primary/nojson"}},
      ],
    }},
  }},
  default_panel: "default",
}};
const catalog = {json.dumps(catalog)};
const lenses = {json.dumps(lenses, ensure_ascii=False)};
const editor = rp.reviewPanelsControl(document, catalog, lenses);
console.log(JSON.stringify({{
  canonical: rp.canonicalReviewPanels(document),
  control: editor.read(),
  errors: rp.validateReviewPanels(document),
  invalid: rp.validateReviewPanels({{
    default_panel: "missing",
    panels: {{default: {{name: "", seats: []}}}},
  }}),
  summary: rp.reviewPanelSummary(document, catalog),
  empty: rp.canonicalReviewPanels(""),
  seatStatus: rp.reviewSeatModelStatus(catalog, "primary/nojson"),
}}));
"""
    )
    expected = (
        '{"default_panel":"default","panels":{"default":{"name":"默认评审组","seats":['
        '{"lens_id":"bear","model_ref":"primary/fast","seat_id":"bear"},'
        '{"lens_id":"value","model_ref":"primary/nojson","seat_id":"value"}]}}}'
    )
    assert out["canonical"] == expected
    assert out["control"] == expected
    assert out["errors"] == []
    assert out["empty"] == ""
    assert out["seatStatus"]["state"] == "unavailable"
    assert "模型不可用" in out["summary"]
    invalid = " ".join(out["invalid"])
    assert "默认评审组不存在" in invalid
    assert "名称不能为空" in invalid
    assert "1–6" in invalid


def test_settings_navigation_helpers() -> None:
    out = _run_settings(
        """
const sections = s.sectionsFor(s.SETTINGS_SECTIONS, [{
  id: "investor", section_id: "agent", nav_group: "Agent", title: "投资 Agent", blurb: "",
  slots: [], topics: [{id: "switches", title: "开关", keys: []}, {id: "quota", title: "额度", keys: []}], topic_groups: [],
}]);
console.log(JSON.stringify({
  home: s.resolveSettingsPlace(""),
  legacy: s.resolveSettingsPlace("#agent-settings", sections),
  model: s.resolveSettingsPlace("#agent/model", sections),
  llm: s.resolveSettingsPlace("#llm", sections),
  catalog: s.resolveSettingsPlace("#llm/catalog", sections),
  cron: [s.cronSummary(""), s.cronSummary("10 15 * * 1-5"), s.cronSummary("1 2 3 4 5")],
  agent: s.sectionHome(
    s.consumerSection({id: "investor", section_id: "agent", nav_group: "Agent", title: "投资 Agent", blurb: "", slots: [{id: "main", required_capabilities: ["tools"]}], topics: [], topic_groups: []}),
    () => false,
  ),
}));
"""
    )
    assert out["home"] == {"sectionId": None, "topicId": None}
    assert out["legacy"] == {"sectionId": "agent", "topicId": None}
    assert out["model"] == {"sectionId": "llm", "topicId": "catalog"}
    assert out["llm"] == {"sectionId": "llm", "topicId": "catalog"}
    assert out["catalog"] == {"sectionId": "llm", "topicId": "catalog"}
    assert out["cron"] == ["已停用", "工作日 15:10", "1 2 3 4 5"]
    assert out["agent"]["value"] == "已关闭"


def test_gateway_status_text() -> None:
    out = _run_settings(
        """
const show = (status) => s.gatewayStatusText(status);
console.log(JSON.stringify({
  off: show({state: "disabled"}),
  incomplete: show({state: "incomplete", detail: "未填写：注册凭证"}),
  connecting: show({state: "connecting", detail: "AUTH_FAILED"}),
  replaced: show({state: "replaced", detail: "已被同一注册凭证的新连接取代，本实例停止重连"}),
  up: show({state: "connected", backend_id: "abc", namespace: "ns"}),
}));
"""
    )
    assert out["off"] == {"text": "未启用", "ok": False, "neutral": True}
    assert out["incomplete"]["text"] == "已启用但配置不完整：未填写：注册凭证"
    assert out["connecting"] == {"text": "连接中：AUTH_FAILED", "ok": False}
    assert out["replaced"] == {
        "text": "服务身份被接管：已被同一注册凭证的新连接取代，本实例停止重连",
        "ok": False,
    }
    assert out["up"] == {"text": "已连接，服务 ID abc（命名空间 ns）", "ok": True}
