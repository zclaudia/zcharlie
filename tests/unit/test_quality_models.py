"""QT-005 SQLite constraints are exercised with FK enforcement disabled."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.exc import IntegrityError

from investment_agent.storage.models import Base
from investment_agent.storage.models.research_quality import (
    ResearchAssessment,
    ResearchExperiment,
    ResearchExperimentReceipt,
    ResearchExperimentTask,
    ResearchTrial,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'quality-models.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 0
        yield connection
    engine.dispose()


def _experiment(db):
    key = str(uuid4())
    db.execute(
        insert(ResearchExperiment).values(
            id=key, owner_id="owner", revision=1, state="draft", created_at=datetime.now(UTC)
        )
    )
    return key


def _trial(db, experiment, **updates):
    row = dict(
        id=str(uuid4()), experiment_id=experiment, arm="baseline", repeat_index=1, state="planned"
    )
    row.update(updates)
    db.execute(insert(ResearchTrial).values(**row))
    return row


def test_five_table_indexes_and_trial_unique_keys(db):
    experiment = _experiment(db)
    _trial(db, experiment)
    with pytest.raises(IntegrityError):
        _trial(db, experiment)
    for values in (
        dict(arm="other"),
        dict(repeat_index=0),
        dict(repeat_index=3),
        dict(state="success"),
    ):
        with pytest.raises(IntegrityError):
            _trial(db, experiment, **values)
    _trial(db, experiment, arm="candidate")
    indexes = list(db.execute(text("PRAGMA index_info('ix_research_experiment_owner_created')")))
    assert [row[2] for row in indexes] == ["owner_id", "created_at", "id"]
    assert ResearchExperimentTask.__table__.c.trial_id.unique


def _assessment(db, trial, revision=1, **changes):
    row = dict(
        id=str(uuid4()),
        experiment_id=trial["experiment_id"],
        trial_id=trial["id"],
        reviewer_id="reviewer",
        revision=revision,
        payload_hash="a" * 64,
        payload={"fixture": "synthetic"},
        created_at=datetime.now(UTC),
    )
    row.update(changes)
    db.execute(insert(ResearchAssessment).values(**row))
    return row


def test_assessment_db_requires_continuous_per_reviewer_revision_and_existing_trial(db):
    trial = _trial(db, _experiment(db))
    with pytest.raises(IntegrityError):
        _assessment(db, trial, 2)
    _assessment(db, trial)
    with pytest.raises(IntegrityError):
        _assessment(db, trial, 3)
    _assessment(db, trial, 2)
    _assessment(db, trial, 1, reviewer_id="second")
    with pytest.raises(IntegrityError):
        _assessment(db, trial, 3, experiment_id=_experiment(db))
    with pytest.raises(IntegrityError):
        _assessment(db, dict(id=str(uuid4()), experiment_id=trial["experiment_id"]))


@pytest.mark.parametrize("kind", ["assessment", "binding", "receipt"])
def test_history_rejects_update_delete_replace_including_hidden_rowid(db, kind):
    trial = _trial(db, _experiment(db))
    if kind == "assessment":
        model = ResearchAssessment
        row = _assessment(db, trial)
    elif kind == "binding":
        model = ResearchExperimentTask
        row = dict(
            task_id=str(uuid4()),
            trial_id=trial["id"],
            envelope_artifact_id=str(uuid4()),
            envelope_hash="a" * 64,
            method_binding_hash="b" * 64,
            model_config_hash="c" * 64,
            profile_version="synthetic.v1",
        )
        db.execute(insert(model).values(**row))
    else:
        model = ResearchExperimentReceipt
        row = dict(
            owner_id="owner",
            mutation_id=str(uuid4()),
            request_hash="a" * 64,
            payload={"fixture": "synthetic"},
            created_at=datetime.now(UTC),
        )
        db.execute(insert(model).values(**row))
    table = model.__tablename__
    ddl = db.scalar(text("SELECT sql FROM sqlite_master WHERE name=:table"), dict(table=table))
    assert "WITHOUT ROWID" in ddl
    with pytest.raises(IntegrityError):
        db.execute(insert(model).values(**row).prefix_with("OR REPLACE"))
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                f"UPDATE {table} SET "
                + ("payload=payload" if kind != "binding" else "profile_version=profile_version")
            )
        )
    with pytest.raises(IntegrityError):
        db.execute(text(f"DELETE FROM {table}"))
    # WITHOUT ROWID removes the separate hidden identity that can otherwise
    # replace a UUID row without meeting the UUID/business-key insert guard.
    with pytest.raises(Exception, match="rowid"):
        db.execute(text(f"INSERT OR REPLACE INTO {table}(rowid) VALUES(1)"))
    assert db.scalar(text(f"SELECT count(*) FROM {table}")) == 1
