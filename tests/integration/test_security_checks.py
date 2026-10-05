"""Release security checks (work package E2 / MVP-405).

Consolidates the checks the release checklist depends on: artifact and
notification downloads refuse path traversal, credential-shaped values never
appear in shipped web assets or prompts, and failed logins never reflect the
submitted key back into the page.
"""

import asyncio
from datetime import date

from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_change import (
    ResearchChangeRepository,
)
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)


def _headers():
    return {"X-API-Key": "secret"}


def _seed_run_with_change_set() -> int:
    async def seed() -> int:
        async with session_scope() as session:
            run = await PipelineRunRepository(session).create(
                kind="ifind_research", request_json={"ticker": "600519.SH"}
            )
            await ResearchSnapshotRepository(session).create(
                pipeline_run_id=run.id,
                symbol="600519",
                market="SH",
                company_name="贵州茅台",
                industry="白酒",
                as_of=date(2026, 10, 5),
                report_year=2025,
                canonical_schema_version="ifind-canonical.v1",
                scoring_version="v1.2",
                score_schema_version="v2.0",
                score_json={"needs_agent_review": False},
                explanation_json=None,
                artifact_manifest={
                    "scores": "/etc/passwd",
                    "escape": str(
                        session.bind.url.database if session.bind else "/tmp/escape"
                    ),
                },
            )
            await PipelineRunRepository(session).mark_success(run.id)
            # Point the notification at a file outside data/notifications.
            await ResearchChangeRepository(session).mark_notified(
                run.id, path="/etc/passwd"
            )
            return run.id

    return asyncio.run(seed())


def test_notification_download_refuses_paths_outside_notifications(api_client):
    run_id = _seed_run_with_change_set()
    response = api_client.get(
        f"/research/runs/{run_id}/change-notification", headers=_headers()
    )
    assert response.status_code == 404
    assert "passwd" not in response.text


def test_artifact_download_refuses_traversal_names(api_client):
    run_id = _seed_run_with_change_set()
    for name in ("..%2F..%2Fetc%2Fpasswd", "../../etc/passwd", "scores"):
        response = api_client.get(
            f"/research/runs/{run_id}/artifacts/{name}", headers=_headers()
        )
        assert response.status_code == 404, name
        assert "root:" not in response.text


def test_shipped_assets_and_prompts_carry_no_credential_shapes():
    import re
    from pathlib import Path

    patterns = (
        re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{11,}"),
        re.compile(r"(?i)api[_-]key\s*[:=]\s*['\"][A-Za-z0-9._-]{8,}"),
    )
    targets = [
        Path("src/investment_agent/web"),
        Path("prompts"),
        Path("config"),
    ]
    violations: list[str] = []
    for target in targets:
        if not target.exists():
            continue
        for path in target.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc", ".db"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in patterns:
                if pattern.search(text):
                    violations.append(str(path))
    assert violations == [], f"credential-shaped values found in: {violations}"


def test_failed_login_does_not_reflect_submitted_key(api_client):
    response = api_client.post(
        "/app/login",
        data={"api_key": "sk-totally-secret-value-123", "next": "/app"},
    )
    assert response.status_code == 401
    assert "sk-totally-secret-value-123" not in response.text


def test_api_default_bind_is_loopback():
    """远程暴露必须显式配置；ia api serve 默认只绑定本机。"""

    import inspect

    from investment_agent.cli.commands.api import serve

    signature = inspect.signature(serve)
    assert signature.parameters["host"].default == "127.0.0.1"
