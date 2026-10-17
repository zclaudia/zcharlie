"""The stock page distinguishes closed controls from unverified sources."""

from __future__ import annotations

from tests.integration.test_api_research_materials import seed_listings
from tests.integration.test_web_stock_financials import _login, _page


async def test_stock_page_explains_source_policy_and_audit_queue(api_client) -> None:
    await seed_listings()
    _login(api_client)
    html = _page(api_client, "00005.HK")

    assert "港股年报来源" in html
    assert "身份绑定：已绑定" in html
    assert "使用许可：未确认" in html
    assert "技术验收：未核验" in html
    assert "当前没有来源通过全部在线采集条件" in html
    assert "不会绕过来源拒绝条件" in html
    assert "来源已关闭" not in html
