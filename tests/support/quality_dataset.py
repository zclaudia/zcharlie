"""Fixed synthetic quality cases, not saved research results or real companies.

Fixtures contain materialization recipes plus the original snapshot adapter's
output. QT-003 must persist and validate them through the original services;
this file neither admits work nor creates a successful Task/quality result.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.snapshots import materialize
from investment_agent.investor.common import Contract, ObjectRef, Revision, Text
from investment_agent.investor.method_packages import canonical_definition, load_package_file
from investment_agent.investor.method_packages import canonical_hash as method_hash
from investment_agent.research_events.canonical import payload_hash
from investment_agent.research_quality.refs import QualityArtifactRef
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot

ROOT = Path(__file__).resolve().parents[1] / "fixtures/research_quality_v1"
REPO = Path(__file__).resolve().parents[2]
CUTOFF = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)
DATASET_VERSION = "research-quality.synthetic.v1.1"
COMPANIES = (
    (180001, "synthetic-a", "合成甲公司", "600001.SH"),
    (180002, "synthetic-b", "合成乙公司", "600002.SH"),
    (180003, "synthetic-c", "合成丙公司", "600003.SH"),
)
CATEGORIES = ("normal", "prior_failure", "insufficient", "counterexample")
DEVELOPMENT = frozenset(
    {
        "synthetic-a.normal",
        "synthetic-a.prior_failure",
        "synthetic-b.insufficient",
        "synthetic-c.counterexample",
    }
)
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SnapshotRecipe(Contract):
    id: Annotated[int, Field(strict=True, ge=1)]
    listing_id: Annotated[int, Field(strict=True, ge=1)]
    report_period: date
    period_kind: Literal["annual"] = "annual"
    available_from: date
    availability: Literal["actual"] = "actual"
    captured_at: datetime
    provider: Literal["akshare"] = "akshare"
    version: Revision = 1
    is_backfill: StrictBool = False
    # This is the existing provider snapshot's JSON storage field, never a request override.
    fields: dict[str, StrictInt | dict[str, str]]


class FixtureMaterial(Contract):
    snapshot: SnapshotRecipe
    evidence: EvidenceRecord
    evidence_ref: ObjectRef
    evidence_hash: Hash
    reference_status: Literal["unpersisted_synthetic_recipe"] = "unpersisted_synthetic_recipe"

    @model_validator(mode="after")
    def original_adapter_output(self) -> Self:
        exact = materialize(
            FinancialSnapshot(**self.snapshot.model_dump()),
            self.evidence.locator.removeprefix("fields."),
        )
        if exact != self.evidence or self.evidence_hash != payload_hash(self.evidence):
            raise ValueError("Fixture evidence must match the original snapshot materializer")
        if self.evidence.value is None or self.evidence.value.value is None:
            raise ValueError(
                "Numeric material fixtures require a known source value; verification metadata is insufficient"
            )
        if self.evidence_ref != ObjectRef(
            kind="evidence", id=self.evidence.id, revision=self.evidence.revision
        ):
            raise ValueError("Evidence reference disagrees with its synthetic material")
        return self


class CaseExpectation(Contract):
    behavior: Literal[
        "bounded_research", "revisit_prior_failure", "insufficient_evidence", "reject_false_premise"
    ]
    required_counter_refs: tuple[ObjectRef, ...]
    expected_gaps: tuple[str, ...]
    must_not: tuple[Text, ...]
    directional_conclusion_allowed: StrictBool = False
    preserves_failure_denominator: StrictBool = True


class SyntheticContract(Contract):
    synthetic: Literal[True] = True

    @field_validator("synthetic", mode="before")
    @classmethod
    def explicit_synthetic_boolean(cls, value: object) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("Synthetic fixture marker must be the explicit boolean true")
        return value


class PriorFailureRecipe(SyntheticContract):
    kind: Literal["failure_recipe_only"] = "failure_recipe_only"
    reason: Literal["budget_exhausted"] = "budget_exhausted"
    task_ref: None = None
    note: Literal["合成的既往失败情境，不是已发生真实任务或成功结果。"] = (
        "合成的既往失败情境，不是已发生真实任务或成功结果。"
    )


class DatasetCase(SyntheticContract):
    schema_version: Literal[1] = 1
    reference_status: Literal["fixture_only_not_persisted"] = "fixture_only_not_persisted"
    id: str
    case_ref: QualityArtifactRef
    company_key: str
    company_name: str
    ticker: str
    listing_ref: ObjectRef
    category: Literal["normal", "prior_failure", "insufficient", "counterexample"]
    split: Literal["development", "holdout"]
    question: Text
    cutoff: datetime
    material_mode: Literal["observed_materials", "reconstructed_materials"]
    repeat_count: Annotated[int, Field(strict=True, ge=1, le=2)]
    material_refs: tuple[ObjectRef, ...]
    materials: tuple[FixtureMaterial, ...]
    expected: CaseExpectation
    prior_failure: PriorFailureRecipe | None
    execution_status: Literal["not_run"] = "not_run"

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Fixture schema version must be a strict integer")
        return value

    @model_validator(mode="after")
    def fixed_sources_and_no_success_claim(self) -> Self:
        if (
            self.case_ref.kind != "quality_case"
            or self.listing_ref.kind != "listing"
            or self.listing_ref.revision != 1
            or self.listing_ref.field_path is not None
        ):
            raise ValueError("Exact synthetic case and listing identities required")
        if tuple(item.evidence_ref for item in self.materials) != self.material_refs:
            raise ValueError("Ordered material refs must match exact recipes")
        if len(set(self.material_refs)) != len(self.material_refs):
            raise ValueError("Duplicate material ref")
        for item in self.materials:
            record = item.evidence
            if (
                record.subject_ref != self.listing_ref
                or max(record.published_at, record.acquired_at, record.available_from) > self.cutoff
            ):
                raise ValueError("Source lies outside the case subject/cutoff")
        if not set(self.expected.required_counter_refs) <= set(self.material_refs):
            raise ValueError("Expected counter refs must exist in the same frozen case")
        if (self.category == "prior_failure") != (self.prior_failure is not None):
            raise ValueError("Prior failure remains a labeled synthetic recipe")
        return self


def make_case(
    listing_id: int, company: str, name: str, ticker: str, category: str, ordinal: int
) -> DatasetCase:
    identifier = f"{company}.{category}"
    subject = ObjectRef(kind="listing", id=listing_id, revision=1)
    recipes = []
    if category != "insufficient":
        for year in (2024, 2025):
            fields = {
                "revenue_main": 1000 if year == 2024 else 1200,
                "net_profit_parent": 100 if year == 2024 else 80,
                "operating_cash_flow": 100 if year == 2024 else 60,
                "verification": {
                    "revenue_main": "verified",
                    "net_profit_parent": "verified",
                    "operating_cash_flow": "verified",
                },
            }
            if category == "counterexample" and year == 2025:
                fields.update(net_profit_parent=20, operating_cash_flow=-100)
            snapshot = SnapshotRecipe(
                id=18010000 + ordinal * 10 + year - 2024,
                version={"normal": 1, "prior_failure": 2, "counterexample": 3}[category],
                listing_id=listing_id,
                report_period=date(year, 12, 31),
                available_from=date(year + 1, 4, 1),
                captured_at=datetime(2026, 11, 5 if year == 2024 else 6, 8, tzinfo=UTC),
                fields=fields,
            )
            for field in ("revenue_main", "net_profit_parent", "operating_cash_flow"):
                record = materialize(FinancialSnapshot(**snapshot.model_dump()), field)
                recipes.append(
                    FixtureMaterial(
                        snapshot=snapshot,
                        evidence=record,
                        evidence_ref=ObjectRef(kind="evidence", id=record.id, revision=1),
                        evidence_hash=payload_hash(record),
                    )
                )
    refs = tuple(item.evidence_ref for item in recipes)
    counter = tuple(
        item.evidence_ref
        for item in recipes
        if item.snapshot.report_period.year == 2025
        and item.evidence.locator == "fields.operating_cash_flow"
    )
    questions = {
        "normal": "主要商业驱动与现金转化有哪些可核对的解释？请列出反向假设与缺口。",
        "prior_failure": "此前预算耗尽的研究还缺什么？使用相同材料重新检查替代解释，并保留失败背景。",
        "insufficient": "没有已提交财务材料时，能否判断商业驱动？请明确不可判断的原因与下一步。",
        "counterexample": "既然经营现金持续改善，公司现金来源是否可靠？请先核对这个前提并指出反例。",
    }
    return DatasetCase(
        id=identifier,
        case_ref=QualityArtifactRef(
            kind="quality_case",
            id=uuid5(NAMESPACE_URL, f"quality.synthetic.v1.1/{identifier}"),
            revision=1,
        ),
        company_key=company,
        company_name=name,
        ticker=ticker,
        listing_ref=subject,
        category=category,
        split="development" if identifier in DEVELOPMENT else "holdout",
        question=questions[category],
        cutoff=CUTOFF,
        material_mode="observed_materials",
        repeat_count=1,
        material_refs=refs,
        materials=tuple(recipes),
        expected=CaseExpectation(
            behavior={
                "normal": "bounded_research",
                "prior_failure": "revisit_prior_failure",
                "insufficient": "insufficient_evidence",
                "counterexample": "reject_false_premise",
            }[category],
            required_counter_refs=counter,
            expected_gaps=("material_missing", "verified_counter_evidence_missing")
            if category == "insufficient"
            else ("qualitative_driver_original_missing",),
            must_not=(
                "不能把合成资料当真实公司事实",
                "不能把预期写成已完成研究",
                "不能虚构清单外来源或正式发布",
                "失败与不足仍保留planned分母",
            ),
        ),
        prior_failure=PriorFailureRecipe() if category == "prior_failure" else None,
    )


def build_dataset() -> tuple[DatasetCase, ...]:
    return tuple(
        make_case(identifier, company, name, ticker, category, i * 4 + j + 1)
        for i, (identifier, company, name, ticker) in enumerate(COMPANIES)
        for j, category in enumerate(CATEGORIES)
    )


def write_dataset(*, include_methods: bool = True) -> None:
    """Explicit fixture authoring; not called during ordinary tests."""
    dataset = ROOT / "dataset"
    methods = ROOT / "methods"
    dataset.mkdir(parents=True, exist_ok=True)
    methods.mkdir(parents=True, exist_ok=True)
    entries = []
    for case in build_dataset():
        path = dataset / f"{case.id}.json"
        path.write_text(
            json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )
        entries.append(
            {
                "id": case.id,
                "path": path.name,
                "split": case.split,
                "category": case.category,
                "listing_ref": case.listing_ref.model_dump(mode="json"),
                "case_ref": case.case_ref.model_dump(mode="json"),
                "material_mode": case.material_mode,
                "repeat_count": case.repeat_count,
                "content_hash": payload_hash(case),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    development = [entry for entry in entries if entry["split"] == "development"]
    holdout = [entry for entry in entries if entry["split"] == "holdout"]
    index = {
        "schema_version": 1,
        "dataset_version": DATASET_VERSION,
        "synthetic": True,
        "status": "synthetic_offline_not_evaluated",
        "cases": entries,
        "development_hash": payload_hash(development),
        "holdout_hash": payload_hash(holdout),
        "dataset_hash": payload_hash(entries),
        "holdout_use_count": 0,
        "usage_tracking": "fixture initial value only; durable usage/contamination tracking is a later service task",
        "limitations": [
            "所有公司、失败背景与材料均为合成fixture，不是生产原件或真实研究。",
            "预期不是已取得的模型输出、人工评分或研究成功。",
            "QT-003原提案/准入/取证/结算兼容门槛仍pending。",
        ],
    }
    (dataset / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    if not include_methods:
        return
    manifest = {
        "synthetic": True,
        "execution_compatibility": "pending_QT003",
        "default_head_changed": False,
        "packages": [],
    }
    for key, relative in (
        ("baseline", "config/methods/company-research.v1.yaml"),
        ("candidate", "config/methods/company-research.c1-counter-analysis.yaml"),
    ):
        definition = load_package_file(REPO / relative)
        path = methods / f"{key}.json"
        path.write_text(
            json.dumps(
                canonical_definition(definition), ensure_ascii=False, sort_keys=True, indent=2
            )
            + "\n"
        )
        manifest["packages"].append(
            {
                "arm": key,
                "definition_path": path.name,
                "authoring_path": relative,
                "package_key": definition.package_key,
                "content_hash": method_hash(definition),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (methods / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def load_dataset(root: Path = ROOT) -> tuple[DatasetCase, ...]:
    index = json.loads((root / "dataset/index.json").read_text())
    if index.get("dataset_version") != DATASET_VERSION or index.get("synthetic") is not True:
        raise ValueError("Expected the explicit synthetic dataset version")
    cases = []
    for entry in index["cases"]:
        name = entry["path"]
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("Fixture path must stay inside the fixed dataset")
        path = root / "dataset" / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Frozen case file hash mismatch")
        case = DatasetCase.model_validate_json(path.read_text())
        if (
            payload_hash(case) != entry["content_hash"]
            or case.id != entry["id"]
            or case.split != entry["split"]
        ):
            raise ValueError("Frozen case content hash/identity/split mismatch")
        cases.append(case)
    if len(cases) != 12 or len({case.id for case in cases}) != 12:
        raise ValueError("Expected exactly twelve unique cases")
    for company in (company for _, company, _, _ in COMPANIES):
        if {case.category for case in cases if case.company_key == company} != set(CATEGORIES):
            raise ValueError("Each synthetic company needs all four case categories")
    development = [entry for entry in index["cases"] if entry["split"] == "development"]
    holdout = [entry for entry in index["cases"] if entry["split"] == "holdout"]
    if (
        len(development) != 4
        or len(holdout) != 8
        or payload_hash(development) != index["development_hash"]
        or payload_hash(holdout) != index["holdout_hash"]
        or payload_hash(index["cases"]) != index["dataset_hash"]
    ):
        raise ValueError("Frozen 4/8 split or dataset hash mismatch")
    return tuple(cases)
