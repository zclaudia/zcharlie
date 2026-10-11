"""AG20 isolated real-material preparation and explicitly configured bounded trial.

Preparation never sends network requests. Source is opened SQLite mode=ro;
only a marked isolated copy is migrated. No credentials are retained in copy.
Optional run uses independent agent_* JSON plus AG20_AGENT_API_KEY, never legacy
fallback. Real outputs are candidates; this tool cannot sign human acceptance.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = (("600036", "招商银行"), ("002415", "海康威视"), ("600406", "国电南瑞"))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_stamp(path: Path) -> dict[str, Any]:
    stat = path.stat()
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": sha}


def readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def prepare(source: Path, output: Path, cutoff: datetime) -> None:
    source = source.resolve()
    output = output.resolve()
    if output.exists() or source.parent == output or source.is_relative_to(output):
        raise ValueError("choose a new isolated directory outside source")
    before = source_stamp(source)
    output.mkdir(parents=True, mode=0o700)
    (output / "ag20-isolated").touch(mode=0o600)
    copy = output / "trial.db"
    with readonly(source) as original, sqlite3.connect(copy) as target:
        revision = original.execute("select version_num from alembic_version").fetchone()[0]
        original.backup(target)
        # The trial is deliberately self-contained, never a production settings clone.
        target.execute("delete from app_setting")
        target.execute("delete from app_setting_change")
        target.execute("update app_setting_meta set imported_at=null")
    copy.chmod(0o600)
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{copy}", "API_KEY": "ag20-inert", "LLM_TIER3_API_KEY": "inert"}
    migration = subprocess.run([str(ROOT / ".venv/bin/alembic"), "upgrade", "head"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    if migration.returncode:
        raise RuntimeError("isolated migration failed; source remains read-only")
    (output / "migration.log").write_text(migration.stdout + migration.stderr)
    registry = __import__("yaml").safe_load((ROOT / "config/field_sources.yaml").read_text())
    definitions = {key: item["definition"] for key, item in registry["fields"].items()}
    companies = []
    with readonly(copy) as connection:
        head = connection.execute("select version_num from alembic_version").fetchone()[0]
        for ticker, name in SAMPLES:
            listing = connection.execute("select l.id,l.ticker,l.market_code,c.canonical_name,c.sector_code from listing l join company c on c.id=l.company_id where l.ticker=? and c.canonical_name=?", (ticker, name)).fetchone()
            if listing is None:
                raise ValueError("sample identity unavailable")
            listing_id = listing["id"]
            rows = connection.execute("select * from financial_snapshot where listing_id=? and captured_at<=? and available_from<=? order by report_period desc,version desc,id desc", (listing_id, cutoff.replace(tzinfo=None).isoformat(sep=" "), cutoff.date().isoformat())).fetchall()
            # Freeze latest version per provider/part/period without merging accounting bases.
            seen = set()
            materials = []
            for row in rows:
                fields = json.loads(row["fields"])
                key = (row["provider"], fields.get("part"), row["report_period"])
                if key in seen:
                    continue
                seen.add(key)
                material = dict(row)
                material["fields"] = fields
                material["sha256"] = digest(dict(row))
                material["locator"] = f"sqlite:financial_snapshot/{row['id']}@{row['version']}"
                material["unit_definitions"] = {key: definitions.get(key, "unregistered; do not infer unit") for key in fields if isinstance(fields[key], (int, float)) and not isinstance(fields[key], bool)}
                material["verification"] = "provider_candidate; not annual-report verification"
                materials.append(material)
                if len(materials) >= 18:
                    break
            prices = connection.execute("select * from market_snapshot where listing_id=? and captured_at<=? and trade_date<=? order by trade_date desc,version desc,id desc limit 1", (listing_id, cutoff.replace(tzinfo=None).isoformat(sep=" "), cutoff.date().isoformat())).fetchall()
            market = [{**dict(row), "sha256": digest(dict(row)), "locator": f"sqlite:market_snapshot/{row['id']}@{row['version']}", "unit": "close=CNY/share; shares=shares; PE/PB=ratio; unverified provider candidate"} for row in prices]
            old = connection.execute("select id,as_of,scoring_version,canonical_schema_version from research_snapshot where symbol=? and as_of<=? order by id desc limit 1", (ticker, cutoff.date().isoformat())).fetchone()
            companies.append({"identity": dict(listing), "cutoff": cutoff.isoformat(), "latest_financial_period": max((r["report_period"] for r in materials), default=None), "latest_acquired_at": max((r["captured_at"] for r in materials), default=None), "latest_price_date": market[0]["trade_date"] if market else None, "materials": materials, "market": market, "legacy_explanation_metadata": dict(old) if old else None, "agent_result": None, "stance": "未生成 Agent 观点；资料包不构成公司研究或发布批准", "basis": "供应商快照可定位，原件与关键论点核验仍缺失", "counter_evidence": "待真实研究验证；不以风险套话填充", "next_step": "核验关键事实与反证后按七项研究维度形成有限候选；人工评审", "gaps": ["官方年报原件与公司特定关键事实关系未核验；raw_ref仅保留来源定位，未复制或声称核验原件", "未形成可追踪的真实历史假设证伪对照", "FCFE资格未核验；DCF关闭", "独立模型与人工质量门禁待完成"], "method_version": "company_research.v1 / provider material packet only", "model_version": None, "money_cost_usd": None, "actual_requests": 0, "actual_tokens": None, "latency_seconds": None})
    after = source_stamp(source)
    if before != after:
        raise RuntimeError("source changed during capture; do not treat packet as verified snapshot")
    report = {"schema": "ag20.materials.v1", "status": "materials_only; engineering does not sign semantic quality", "source": {"revision": revision, **before}, "isolated_revision": head, "source_unchanged": before == after, "independent_configuration_missing": ["agent_base_url", "agent_model", "agent_api_key"], "production_enabled": False, "background_enabled": False, "reverse_dcf_enabled": False, "companies": companies}
    report["material_set_hash"] = digest(companies)
    (output / "materials.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    summary = ["# AG20 三公司真实资料包", "", "状态：资料包已冻结，尚无独立 Agent 研究结果、方向观点、真实调用成本或人工质量通过。", "", f"来源库 {revision} → 隔离库 {head}；原库 stat/hash 不变。材料集 `{report['material_set_hash']}`。", "", "|公司|报告期|取得截止|行情日期|旧解释版本|", "|---|---|---|---|---|"]
    for company in companies:
        old = company["legacy_explanation_metadata"]
        summary.append(f"|{company['identity']['canonical_name']} ({company['identity']['ticker']})|{company['latest_financial_period']}|{company['latest_acquired_at']}|{company['latest_price_date']}|{old['scoring_version'] if old else '无旧研究快照'}|")
    for company in companies:
        summary.extend(["", f"## {company['identity']['canonical_name']}", "", company["stance"], "", f"依据：{company['basis']}。反证：{company['counter_evidence']}。下一步：{company['next_step']}。", "", "研究缺口：" + "；".join(company["gaps"]) + "。", "", "引用见 materials.json 的 financial_snapshot/market_snapshot id@version、provider、期间、available_from、captured_at、raw_ref、字段单位定义与内容 hash。供应商候选没有升级为 verified。银行不适用本版 FCFE。"])
    (output / "review.md").write_text("\n".join(summary) + "\n")
    print(json.dumps({"status": report["status"], "output": str(output), "source_unchanged": True, "companies": len(companies)}))


async def run_trial(directory: Path, config_file: Path, *, _test_model: Any = None, _test_transport: Any = None, retry_existing: bool = False, repair_after_unsent: bool = False, conclude_existing: bool = False, task_tokens: int = 50000, validation_retries: int = 0) -> None:
    from investment_agent.agent.contracts import MessageInput
    from investment_agent.agent.conversations import ConversationCreate, ConversationService
    from investment_agent.agent.job import agent_job_handlers
    from investment_agent.agent.model import AgentModelConfig
    from investment_agent.config import Settings
    from investment_agent.investor.common import ObjectRef
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.settings.loader import ensure_imported
    from investment_agent.storage.database import init_engine, session_scope

    prior_file = directory / "actual-results.json"
    predecessor_file = directory / "actual-results-repair-1.json" if repair_after_unsent or conclude_existing else prior_file
    if not 50000 <= task_tokens <= 120000 or not 0 <= validation_retries <= 2:
        raise ValueError("trial limits outside bounded envelope")
    output_file = directory / ("actual-results-conclusion-repair.json" if conclude_existing and repair_after_unsent else "actual-results-conclusion.json" if conclude_existing else "actual-results-repair-2.json" if repair_after_unsent else "actual-results-repair-1.json" if retry_existing else "actual-results.json")
    if conclude_existing and not retry_existing:
        raise ValueError("conclusion requires explicit retry and a completed paid predecessor")
    if repair_after_unsent and not retry_existing:
        raise ValueError("explicit retry-existing required for unsent predecessor")
    if not (directory / "ag20-isolated").exists() or output_file.exists() or (retry_existing and not prior_file.exists()):
        raise ValueError("marked isolation required; refuse repeat paid trial")
    raw = json.loads(await asyncio.to_thread(config_file.read_text))
    if not raw.get("agent_base_url") or not raw.get("agent_model"):
        raise ValueError("explicit independent endpoint/model required")
    if any(not key.startswith("agent_") or key == "agent_api_key" for key in raw):
        raise ValueError("config accepts independent agent_* fields only; key is environment-only")
    settings = Settings(_env_file=None, app_env="dev", data_dir=directory, database_url=f"sqlite+aiosqlite:///{directory / 'trial.db'}", api_key="ag20-inert", llm_tier3_api_key="inert", **{**raw, "agent_api_key": os.environ.get("AG20_AGENT_API_KEY", ""), "agent_max_tokens": 4096, "agent_retries": validation_retries, "agent_timeout_seconds": 45}, investor_agent_enabled=True, investor_agent_background_enabled=False, investor_agent_task_requests=8, investor_agent_daily_requests=24, investor_agent_task_tokens=task_tokens, investor_agent_daily_tokens=3 * task_tokens, investor_agent_task_tool_calls=24, investor_agent_task_seconds=360)
    config = AgentModelConfig.from_settings(settings, 0)
    config.validate_ready()
    packet = json.loads((directory / "materials.json").read_text())
    engine = init_engine(f"sqlite+aiosqlite:///{directory / 'trial.db'}")
    async with session_scope() as session:
        await ensure_imported(session, settings, app_yaml_path=directory / "absent.yaml")
    prior = json.loads(prior_file.read_text()) if retry_existing else None
    if prior is not None and (prior.get("limits", {}).get("tokens_per_company", 50000) != task_tokens or prior.get("limits", {}).get("validation_retries", 0) != validation_retries):
        raise ValueError("historical trial allowance/configuration cannot be enlarged")
    if prior is not None and (prior.get("state") != "bounded trial completed" or len(prior.get("results", [])) != 3 or prior.get("material_set_hash") != packet["material_set_hash"] or prior.get("configuration", {}).get("version") != config.version):
        raise ValueError("complete unchanged prior trial required")
    predecessor = json.loads(predecessor_file.read_text()) if repair_after_unsent or conclude_existing else prior
    lineage = [item["task"]["id"] for item in prior["results"]] if prior else []
    if repair_after_unsent or conclude_existing:
        from sqlalchemy import select

        from investment_agent.storage.models.agent import AgentModelRun
        if predecessor.get("state") != "bounded trial completed" or len(predecessor.get("results", [])) != 3 or predecessor.get("material_set_hash") != packet["material_set_hash"]:
            raise ValueError("complete unchanged unsent predecessor required")
        async with session_scope() as session:
            for index, item in enumerate(predecessor["results"]):
                if item["company"] != prior["results"][index]["company"] or item["task"]["parent_task_id"] != prior["results"][index]["task"]["id"]:
                    raise ValueError("unsent predecessor lineage differs")
                audits = (await session.scalars(select(AgentModelRun).where(AgentModelRun.task_id == item["task"]["id"]))).all()
                if not conclude_existing and (not audits or any(a.http_attempts != 0 or a.http_responses or a.framework_requests != 0 for a in audits)):
                    raise ValueError("predecessor was not proven zero HTTP; paid repeat refused")
        lineage.extend(item["task"]["id"] for item in predecessor["results"])
    unsent_conclusion = None
    if conclude_existing and repair_after_unsent:
        from investment_agent.storage.models.agent import BudgetEntry
        unsent_conclusion = json.loads((directory / "actual-results-conclusion.json").read_text())
        if (unsent_conclusion.get("state") != "bounded trial completed" or len(unsent_conclusion.get("results", [])) != 3 or
                unsent_conclusion.get("configuration", {}).get("version") != config.version or
                unsent_conclusion.get("material_set_hash") != packet["material_set_hash"]):
            raise ValueError("complete unchanged unsent conclusion required")
        async with session_scope() as session:
            for index, item in enumerate(unsent_conclusion["results"]):
                if (item["company"] != predecessor["results"][index]["company"] or
                        item["task"]["parent_task_id"] != predecessor["results"][index]["task"]["id"] or
                        item["task"]["status"] != "failed" or item["task"]["usage"]["requests"] != 0):
                    raise ValueError("unsent conclusion lineage/state differs")
                audits = (await session.scalars(select(AgentModelRun).where(AgentModelRun.task_id == item["task"]["id"]))).all()
                entries = (await session.scalars(select(BudgetEntry).where(BudgetEntry.task_id == item["task"]["id"], BudgetEntry.state != "released"))).all()
                if entries or any(a.http_attempts != 0 or a.http_responses or a.framework_requests != 0 for a in audits):
                    raise ValueError("conclusion was not proven zero HTTP; paid repeat refused")
        lineage.extend(item["task"]["id"] for item in unsent_conclusion["results"])
    results = []
    # Write a reservation marker first: a killed trial is never silently re-run.
    limits = {"requests_per_company": 8, "total_requests": 24, "tokens_per_company": task_tokens, "total_tokens": 3 * task_tokens, "validation_retries": validation_retries, "seconds_per_company": 360, "requested_output_tokens": 4096, "conclusion_only": conclude_existing, "phase_requests_per_company": 1 if conclude_existing else None}
    output_file.write_text(json.dumps({"state": "started", "limits": limits, "configuration": config.snapshot(), "results": []}))
    try:
        for index, company in enumerate(packet["companies"]):
            previous = predecessor["results"][index] if predecessor else None
            company_task_ids = {previous["task"]["id"], prior["results"][index]["task"]["id"]} if previous else set()
            remaining = None
            if previous:
                from sqlalchemy import select

                from investment_agent.settings.loader import store_for
                from investment_agent.storage.models.agent import BudgetEntry
                async with session_scope() as session:
                    rows = (await session.scalars(select(BudgetEntry).where(BudgetEntry.task_id.in_(lineage), BudgetEntry.state != "released"))).all()
                    if any(row.state != "settled" or row.input_tokens is None or row.output_tokens is None for row in rows):
                        raise ValueError("prior usage unresolved; paid retry refused")
                    company_rows = [row for row in rows if row.task_id in company_task_ids]
                    used_requests = sum(row.calls for row in rows if row.kind == "model")
                    used_tokens = sum(row.input_tokens + row.output_tokens for row in rows)
                    requests_left = min(8 - sum(row.calls for row in company_rows if row.kind == "model"), 24 - used_requests)
                    if conclude_existing:
                        requests_left = min(1, requests_left)
                    tokens_left = min(task_tokens - sum(row.input_tokens + row.output_tokens for row in company_rows), 3 * task_tokens - used_tokens)
                    tools_left = 24 - sum(row.calls for row in company_rows if row.kind == "tool")
                    if min(requests_left, tokens_left, tools_left) <= 0:
                        raise ValueError("cumulative authorized allowance exhausted")
                    seconds_left = int(360 - previous["latency_seconds"] - (prior["results"][index]["latency_seconds"] if repair_after_unsent or conclude_existing else 0))
                    if unsent_conclusion:
                        seconds_left -= int(unsent_conclusion["results"][index]["latency_seconds"] + 1)
                    if seconds_left <= 0:
                        raise ValueError("cumulative company time allowance exhausted")
                    remaining = {"requests": requests_left, "tokens": tokens_left, "tools": tools_left, "seconds": seconds_left, "total_used_requests": used_requests, "total_used_tokens": used_tokens}
                    settings = settings.model_copy(update={"investor_agent_task_requests": requests_left, "investor_agent_task_tokens": tokens_left, "investor_agent_task_tool_calls": tools_left, "investor_agent_task_seconds": seconds_left})
                    values = {**{key: getattr(settings, key) for key in raw}, "agent_max_tokens": 4096, "agent_retries": validation_retries, "agent_timeout_seconds": 45, "investor_agent_task_seconds": seconds_left, "investor_agent_enabled": True, "investor_agent_background_enabled": False, "investor_agent_task_requests": requests_left, "investor_agent_task_tokens": tokens_left, "investor_agent_task_tool_calls": tools_left, "investor_agent_daily_requests": 24, "investor_agent_daily_tokens": 3 * task_tokens}
                    if settings.settings_encryption_key:
                        values["agent_api_key"] = settings.agent_api_key
                    await store_for(session, settings).apply(values, actor="ag20-isolated-authorized-repair")
            async with session_scope() as session:
                service = ConversationService(session, settings)
                conversation = await service.create(ConversationCreate(title="AG20 " + company["identity"]["canonical_name"]))
                ref = ObjectRef(kind="listing", id=company["identity"]["id"], revision=1)
                receipt = await service.submit(conversation.id, MessageInput(client_message_id=uuid4(), text="深入研究这家公司。明确立场、关键依据、最可能推翻观点的反证、下一步与研究缺口。供应商候选未核验，银行不得套FCFE，DCF继续关闭，证据不足不要发表方向结论。", context_refs=(ref,), research_kind="company_research", research_at=datetime.fromisoformat(company["cutoff"])))
                if previous:
                    from investment_agent.agent.tasks import TaskService
                    task_service = TaskService(session)
                    row = await task_service._row(receipt.task.id)
                    task = await task_service.get_task(receipt.task.id)
                    await task_service._save(row, task.model_copy(update={"parent_task_id": UUID(previous["task"]["id"])}))
                lineage.append(str(receipt.task.id))
            started = time.monotonic()
            worker = ResearchWorker(settings=settings, worker_id="ag20-explicit-isolated", handlers=agent_job_handlers(settings))
            if _test_model is not None or _test_transport is not None:
                from investment_agent.agent.executor import AgentExecutor
                from investment_agent.agent.job import AgentJobHandler
                from investment_agent.agent.research import ResearchPolicy, registrations
                worker = ResearchWorker(settings=settings, worker_id="ag20-offline-test", handlers=[AgentJobHandler("agent_turn", AgentExecutor(settings, model=_test_model, transport=_test_transport, registrations=registrations(), specification=ResearchPolicy(settings).specification()))])
            from investment_agent.agent.tasks import TaskService
            async with session_scope() as session:
                row = await TaskService(session)._row(receipt.task.id)
                run_id = row.run_id
            try:
                await asyncio.wait_for(worker.run_once(run_id=run_id), timeout=settings.investor_agent_task_seconds + 10)
                error = None
            except Exception as exc:
                error = type(exc).__name__  # never serialize upstream endpoint/credential errors
            async with session_scope() as session:
                task = await TaskService(session).get_task(receipt.task.id)
                messages = await ConversationService(session, settings).messages(conversation.id)
                from sqlalchemy import select

                from investment_agent.storage.models.agent import AgentModelRun, ToolAction
                model_runs = (await session.scalars(select(AgentModelRun).where(AgentModelRun.task_id == str(task.id)))).all()
                material_actions = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task.id), ToolAction.state == "committed"))).all()
                actual_material_receipts = [{"operation_id": a.operation_id, "request": a.parameters, "result": a.result, "receipt_hash": digest(a.result)} for a in material_actions if a.parameters.get("tool") in {"get_snapshot", "capture_snapshot", "get_evidence", "get_event", "historical_valuation_percentile", "reverse_dcf_fcfe"}]
                audit = [{"request_sizes": r.config_snapshot.get("request_sizes", []), "framework_requests": r.framework_requests, "http_attempts": r.http_attempts, "http_responses": r.http_responses, "usage": r.usage, "usage_unknown_reason": r.usage_unknown_reason, "latency_ms": r.latency_ms, "success": r.success, "error_code": r.error_code, "config_version": r.config_version, "framework_version": r.framework_version, "cost_usd": None, "cost_unknown_reason": r.cost_unknown_reason} for r in model_runs]
            results.append({"previous_failed_task_id": previous["task"]["id"] if previous else None, "cumulative_remaining_before_attempt": remaining, "actual_material_receipts": actual_material_receipts, "actual_material_receipts_hash": digest(actual_material_receipts), "model_runs": audit, "actual_binding_cutoff": task.binding.as_of.isoformat(), "packet_is_exhaustive_actual_material_set": False, "actual_artifact_refs": [r.model_dump(mode="json") for r in task.artifact_refs], "company": company["identity"], "task": task.model_dump(mode="json"), "messages": [m.model_dump(mode="json") for m in messages], "latency_seconds": time.monotonic() - started, "error_type": error, "semantic_acceptance": "human pending", "money_cost_usd": None})
            output_file.write_text(json.dumps({"state": "bounded trial completed" if len(results) == 3 else "running", "limits": limits, "configuration": config.snapshot(), "material_set_hash": packet["material_set_hash"], "previous_attempt": str(predecessor_file) if predecessor else None, "unsent_conclusion_attempt": str(directory / "actual-results-conclusion.json") if unsent_conclusion else None, "results": results}, ensure_ascii=False, indent=2))
    finally:
        # The isolated run is never left enabled with provider credentials.
        from sqlalchemy import delete

        from investment_agent.settings.loader import store_for
        from investment_agent.settings.registry import SECRET_KEYS
        from investment_agent.storage.models.app_settings import AppSetting, AppSettingChange
        async with session_scope() as session:
            await store_for(session, settings).apply({"investor_agent_enabled": False, "investor_agent_background_enabled": False}, actor="ag20-trial-cleanup")
            await session.execute(delete(AppSetting).where(AppSetting.key.in_(SECRET_KEYS)))
            await session.execute(delete(AppSettingChange).where(AppSettingChange.key.in_(SECRET_KEYS)))
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--source", type=Path, required=True)
    preparation.add_argument("--output", type=Path, required=True)
    preparation.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    trial = commands.add_parser("run")
    trial.add_argument("--directory", type=Path, required=True)
    trial.add_argument("--model-config", type=Path, required=True)
    trial.add_argument("--retry-existing", action="store_true", help="One explicit repair attempt, retaining original report and cumulative ledger")
    trial.add_argument("--repair-after-unsent", action="store_true", help="Retain zero-HTTP predecessor and allow one final transport validation")
    trial.add_argument("--conclude-existing", action="store_true", help="One synthesis-only request per company from both prior lineages, within unchanged cumulative limits")
    trial.add_argument("--task-tokens", type=int, default=50000, help="Fresh explicitly authorized trial only: 50000–120000 per company")
    trial.add_argument("--validation-retries", type=int, default=0, help="Fresh trial only: 0–2 native corrections within request/token limits")
    args = parser.parse_args()
    if args.command == "prepare":
        if args.as_of.tzinfo is None:
            parser.error("as-of requires timezone")
        prepare(args.source, args.output, args.as_of.astimezone(UTC))
    else:
        asyncio.run(run_trial(args.directory.resolve(), args.model_config, retry_existing=args.retry_existing, repair_after_unsent=args.repair_after_unsent, conclude_existing=args.conclude_existing, task_tokens=args.task_tokens, validation_retries=args.validation_retries))


if __name__ == "__main__":
    main()
