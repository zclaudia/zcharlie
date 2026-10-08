"""Settings page (settings-plan S5): shell, auth, nav and the pure helpers of settings.js."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

STATIC = Path("src/investment_agent/web/static")
DISCLAIMER = "研究辅助，非投资建议"
KEY = {"X-API-Key": "secret"}


def _login(client) -> None:
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


def test_settings_page_requires_session(api_client) -> None:
    response = api_client.get("/app/settings", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app/login"


def test_settings_page_shell(api_client) -> None:
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
        "/app/static/app.js",
    ):
        assert fragment in html, fragment


def test_nav_links_settings_on_every_page(api_client) -> None:
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
    for endpoint in ("/settings", "/settings/history", "/settings/test/llm", "/settings/test/ifind"):
        assert endpoint in settings, endpoint


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


def test_page_input_hints_match_the_api(api_client) -> None:
    """Every input type the API emits is one settings.js renders."""

    body = api_client.get("/settings", headers=KEY).json()
    types = {item["input"]["type"] for group in body["groups"] for item in group["items"]}
    source = (STATIC / "settings.js").read_text(encoding="utf-8")
    # text / url / timezone share the text input; secrets are keyed on item.secret.
    for kind in types - {"text", "url", "timezone", "secret"}:
        assert f'"{kind}"' in source, kind
