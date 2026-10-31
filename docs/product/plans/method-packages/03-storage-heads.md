# 卡 03:存储、迁移 056 与 head 解析

设计依据:§3、§5。前置:卡 01、02。

## 模型(`storage/models/investor.py`)

1. 新增 `MethodDefaultHead`:`__tablename__ = "investor_method_default_head"`,
   `owner_id: str(32)` + `channel: str(32)` 联合主键、`revision: int`、`payload: JSON`
   (存 MethodBinding.model_dump)。不加不可变监听(head 有 revision 可更新,同 `MethodHead`)。
2. `ToolAction`(`storage/models/agent.py`)新增可空列 `committed_sequence: Mapped[int | None]`
   (任务内单调,代码层赋值,不加唯一约束;可建 `(task_id)` 下的普通索引不强制)。

## 迁移 `alembic/versions/056_method_package_storage.py`

照现有迁移风格(参考 052/055):upgrade 建 `investor_method_default_head` 表 +
`agent_tool_action` 加列 `committed_sequence INTEGER NULL`;downgrade 反向。SQLite 与通用方言均可执行。

## `MethodService` 扩展(`investor/methods.py`,沿用 method_transaction / replay / receipt 惯例)

1. `save_package(definition, *, source: str, proposal_id: UUID | None = None, evidence_hash: str | None = None) -> dict`
   :先 `validate_definition`;按 `(owner, package_key, content_hash)` 在 `MethodArtifact kind="package"`
   中查重,存在即返回已有工件(幂等);否则 `version` = 该 package_key 现有最大 version + 1,新增工件,
   payload 为 MethodPackage 全字段。
2. `package(package_key: str, version: int) -> dict`、`package_by_ref(ref: ObjectRef) -> dict`
   (ref.kind 必须 `"package"`,按 artifact_id 查 `kind="package"` 工件;找不到抛 `InvestorError("not_found", ...)`)。
3. `default_head(channel: str = "company-research") -> tuple[int, MethodBinding | None]`(无 head 返回 `(0, None)`)。
4. `activate_global(request: ActivateGlobal, *, actor: str) -> dict`:
   - `ActivateGlobal(Contract)`:`mutation_id: UUID`、`channel: str = "company-research"`、
     `package_ref: ObjectRef`、`expected_revision: int = Field(ge=0)`、`reason: Text`。
   - actor 必须 `"authenticated_user"`(照 `decide`/`rollback` 的断言);包必须存在且
     `applicable_kinds ⊇ {"company_research","review"}`(首版 channel 语义)。
   - 追加 `MethodArtifact kind="version"`(payload 含新 MethodBinding v2:
     `method_ref=ObjectRef(kind="method", id=uuid4(), revision=1)`、`package_ref`、`package_hash`、
     `config=package.discipline`、`listing_ref=None`)与 `kind="event"`(operation=activate_global,
     冻结请求与包哈希);幂等 replay(`InvestorMutation`);乐观并发:head revision 不符抛
     `stale_revision`。原子 move head(照 `move_head`,建行或 revision+1)。
   - **不触碰任何 per-listing `MethodHead`**。
5. `resolve_binding(listing: ObjectRef | None, research_kind: str) -> MethodBinding`:
   单证券 company_research/review:per-listing head(现行 `current` 语义)→ default head → `MethodBinding()`
   (legacy v1);其余情况一律 `MethodBinding()`。注意:per-listing head 现存 payload 都是 v1(本轮没有
   v2 提案路径),原样返回即可。

## 测试(扩展 `tests/unit/test_method_evolution.py` 或新文件 `tests/unit/test_method_packages_store.py`)

- save_package 幂等去重、version 按 package_key 单调、不同内容新版本。
- activate_global:幂等 replay、stale_revision、head 移动且 per-listing head 不动、
  actor 非 authenticated_user 拒绝、包 applicability 不足拒绝。
- resolve_binding 三级顺序(造 default head 后无 per-listing 覆盖的单证券任务取 default;
  有 per-listing 覆盖时取 per-listing;多标的/comparison 取 legacy)。
- 迁移:对空库 `ia migrate up` 后新表/新列存在(照现有迁移测试模式,若有 alembic 测试基建则复用;
  否则用 alembic upgrade head 于 tmp SQLite 断言 schema)。

## 验收

三门禁 + 新增测试全绿;既有测试零回归。
