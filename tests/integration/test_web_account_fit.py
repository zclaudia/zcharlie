"""Account-fit and candidate-ranking browser surfaces and request recovery."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path("src/investment_agent/web/static")


def _login(client) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def test_candidate_page_has_an_independent_account_ranking_root(api_client) -> None:
    _login(api_client)
    response = api_client.get("/app/candidates")
    assert response.status_code == 200
    assert 'id="candidate-ranking-root"' in response.text
    assert 'id="candidate-ranking-content"' in response.text
    assert "/app/static/app.js" in response.text
    assert "/app/static/pages/account_fit.css" in response.text


def test_stock_page_has_account_fit_root_and_reads_existing_listing_id(api_client) -> None:
    _login(api_client)
    response = api_client.get("/app/stocks/600406.SH")
    assert response.status_code == 200
    assert 'id="account-fit-root"' in response.text
    assert 'data-listing-id="' in response.text
    assert 'id="account-fit-content"' in response.text
    assert "/app/static/app.js" in response.text


def test_ranking_confirmation_is_not_prechecked_and_history_is_closed_by_default() -> None:
    source = (STATIC / "candidate_ranking.js").read_text(encoding="utf-8")
    assert 'type: "checkbox", name: "price_confirmation"' in source
    assert "priceConfirmation.checked" in source
    assert 'open: expand' in source
    assert "idempotency: false" in source
    assert 'source_policy: { template_ids: context.source_templates' in source


def test_modules_import_and_pending_body_round_trips_before_send_state() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    fit_uri = (STATIC / "account_fit.js").resolve().as_uri()
    ranking_uri = (STATIC / "candidate_ranking.js").resolve().as_uri()
    script = f"""
import assert from 'node:assert/strict';
const fit = await import({json.dumps(fit_uri)});
await import({json.dumps(ranking_uri)});
const storage = {{ data: new Map(), setItem(k,v) {{ this.data.set(k,String(v)); }}, getItem(k) {{ return this.data.get(k) ?? null; }} }};
const body = {{ operation_id: 'stable-op', account_id: 'account', items: [{{listing_ref:{{kind:'listing',id:'L1',revision:1}}, action:'hold'}}] }};
const record = fit.operationRecord(body.operation_id, body, {{basis:'frozen'}}, 'prepared');
fit.saveOperation(storage, 'pending', record);
const restored = fit.readOperation(storage, 'pending');
assert.equal(restored.operation_id, 'stable-op');
assert.deepEqual(restored.body, body);
assert.equal(restored.phase, 'prepared');
restored.phase = 'accepted';
restored.accepted = {{run_ref:{{kind:'pipeline_run',id:12,revision:1}}}};
fit.saveOperation(storage, 'pending', restored);
assert.equal(fit.readOperation(storage, 'pending').accepted.run_ref.id, 12);
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
