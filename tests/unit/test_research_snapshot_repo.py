from datetime import date

from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_change import ResearchChangeRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)


async def test_research_snapshot_persists_versions_and_artifact_manifest(db_session):
    run = await PipelineRunRepository(db_session).create(kind="ifind_research")
    repository = ResearchSnapshotRepository(db_session)
    created = await repository.create(
        pipeline_run_id=run.id,
        symbol="600406",
        market="SH",
        company_name="国电南瑞",
        industry="电力设备",
        as_of=date(2026, 10, 5),
        report_year=2025,
        canonical_schema_version="ifind-canonical.v1",
        scoring_version="v1.2",
        score_schema_version="v2.0",
        score_json={"business_score": 55.22},
        explanation_json={"summary": "fixture"},
        artifact_manifest={"scores": "/tmp/scores.json"},
    )

    loaded = await repository.by_run(run.id)

    assert loaded is not None
    assert loaded.id == created.id
    assert loaded.score_json["business_score"] == 55.22
    assert loaded.artifact_manifest["scores"] == "/tmp/scores.json"
    change_set = await ResearchChangeRepository(db_session).by_run(run.id)
    assert change_set is not None
    assert change_set.baseline is True
    assert change_set.change_count == 0
    assert change_set.diff_json["current_run_id"] == run.id


async def test_research_snapshot_history_is_newest_first(db_session):
    repository = ResearchSnapshotRepository(db_session)
    for day in (4, 5):
        run = await PipelineRunRepository(db_session).create(kind="ifind_research")
        await repository.create(
            pipeline_run_id=run.id,
            symbol="600406",
            market="SH",
            company_name="国电南瑞",
            industry="电力设备",
            as_of=date(2026, 10, day),
            report_year=2025,
            canonical_schema_version="ifind-canonical.v1",
            scoring_version="v1.2",
            score_schema_version="v2.0",
            score_json={"business_score": float(day)},
            explanation_json=None,
            artifact_manifest={},
        )

    history = await repository.history(symbol="600406", market="SH")

    assert [snapshot.as_of.day for snapshot in history] == [5, 4]


def test_score_item_from_payload_handles_both_shapes():
    from investment_agent.storage.repositories.research_snapshot import (
        score_item_from_payload,
    )

    bare = {"investment_score": 66.0, "strategy_label": "稳健质量成长"}
    wrapped = {"items": [bare], "metadata": {"scoring_version": "v1.2"}}
    empty = {"metadata": {}}

    assert score_item_from_payload(bare) is bare
    assert score_item_from_payload(wrapped) is bare
    assert score_item_from_payload(empty) == {}
