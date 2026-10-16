"""Two-connection WAL test of a P2 current projection during a rule mutation."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from investment_agent.storage.database import init_engine
from investment_agent.storage.models import Base
from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository
from investment_agent.supplemental_rules.service import SupplementalRuleService
from tests.unit.test_supplemental_service import request, seed


async def test_current_read_keeps_registry_rules_and_apps_in_one_wal_snapshot(tmp_path, monkeypatch):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'current-snapshot.sqlite'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            reference, rule = await seed(session)
            service = SupplementalRuleService(session)
            command = request(reference)
            manifest = await service.freeze(command, as_of=datetime.now(UTC))
            await service.save_pair(manifest, manifest.pairs[0], operation_id=command.idempotency_key)
            await session.commit()
        async with sessions() as reader:
            service = SupplementalRuleService(reader)
            original = service.store.get_registry_head
            committed = False

            async def read_then_retire(owner):
                nonlocal committed
                result = await original(owner)
                if not committed:
                    async with sessions() as writer:
                        repository = SupplementalRulesRepository(writer)
                        await repository.reserve_registry(owner)
                        await repository.put_rule_head(owner, rule.id, expected_head_revision=1,
                            active_revision=1, active_state="retired", draft_revision=None, draft_state=None,
                            last_validation_revision=None, last_validation_hash=None, last_validation_payload=None)
                        await repository.advance_registry(owner, rules_delta=1, updated_at=datetime.now(UTC))
                        await writer.commit()
                    committed = True
                return result

            monkeypatch.setattr(service.store, "get_registry_head", read_then_retire)
            current = await service.current(reference, "company_research")
            assert committed
            assert current.coverage_complete  # Entire read sees the original active state.
            assert current.applications[0].current_limits.current_status == "valid"
            await reader.rollback()
        async with sessions() as observer:
            assert (await SupplementalRulesRepository(observer).get_rule_head("local_owner", rule.id)).active_state == "retired"
    finally:
        await engine.dispose()
