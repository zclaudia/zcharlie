# 卡 01:包契约、注册表与 canonical hash

设计依据:`docs/product/investor-agent-method-packages.md` §4.1、§4.2、§8。
新建 `src/investment_agent/investor/method_packages.py`(纯库,无存储、无 IO)。

## 契约(全部继承 `investor/common.py` 的 `Contract`,带 `schema_version: Literal[1] = 1`)

1. `MethodStep`:`id`(稳定短标识,1–32 字符 `[a-z0-9_]+`)、`title`(≤200 字,仅供审阅)、
   `instruction_kind: Literal["falsifiable_plan","counter_analysis","dimension_assessment","snapshot_capture","calculation","peer_context"]`、
   `blocking: bool`、`depends_on: tuple[str, ...] = ()`、`required_capabilities: tuple[ToolFamily, ...] = ()`、
   `required_outputs: tuple[OutputRequirement, ...] = ()`、`allow_missing: bool = False`。
2. `OutputRequirement` 判别联合(`type` 字段,`extra="forbid"`,不允许无参数变体):
   - `plan`:`require_completion_criteria: bool`、`require_stop_conditions: bool`
   - `hypotheses`:`min_count: int = Field(ge=1)`
   - `counter_refs`:`min_count: int = Field(ge=1)`、`verified_only: bool = False`
   - `dimension`:`dimension: Dimension`(七枚举)、`minimum_status: Literal["missing","qualitative","evidenced"] = "missing"`
   - `calculation`:`method: CalculationMethod`
   - `snapshot_field`:`field: SnapshotField`
   - `peer_context`:`require_calculation: bool = False`
3. `DimensionRequirement`:`dimension`、`minimum_status` 默认 `"missing"`、`allow_not_applicable: bool = True`、
   `allow_missing: bool = False`。
4. `MethodPackageDefinition`:`package_key`(1–64 字符 `[a-z0-9-]+`)、
   `applicable_kinds: tuple[Literal["company_research","review"], ...]`(非空、不重复)、
   `steps`(≥1 条,至少一条 blocking)、`dimensions: tuple[DimensionRequirement, ...] | None = None`
   (None = 现状兼容默认值:七维各一条、minimum_status=missing、allow_not_applicable=True、allow_missing=False)、
   `discipline: MethodConfig = MethodConfig()`、`description: Text = ""`。
5. `MethodPackage`(入库工件 payload):definition 的 canonical 字段 + `artifact_id: UUID`、`version: int`、
   `content_hash: str`、`created_at`、`source`、`proposal_id | None`、`evidence_hash | None`(生命周期字段)。

## 代码注册表(包只能引用,不能扩展)

- `ToolFamily = Literal["snapshot_read","evidence","valuation","peer","disclosure","document","portfolio"]`
- `TOOL_FAMILY_MEMBERS: dict[ToolFamily, tuple[str, ...]]` 静态表,成员为现有注册工具名
  (`agent/research.py registrations()`):snapshot_read=`get_snapshot`,`capture_snapshot`;evidence=
  `get_evidence`,`get_event`,`search_saved_sources`,`compare_saved_sources`,`compare_stocks`;valuation=
  `historical_valuation_percentile`,`reverse_dcf_fcfe`;peer=`get_industry_peer_context`;disclosure=
  `discover_disclosures`,`fetch_disclosure_original`;document=`parse_disclosure_original`,`read_disclosure_original`;
  portfolio=`get_portfolio_context`,`calculate_portfolio_options`,`prepare_portfolio_plan`。
  附 `resolve_family_members(families) -> tuple[str, ...]`。
- `CalculationMethod = Literal["historical_valuation_percentile","reverse_dcf_fcfe"]`
- `SnapshotField = Literal["close","roe","operating_cash_flow","capital_expenditure","net_profit_parent","net_profit_total","revenue_main","revenue_total"]`
  (与 `agent/builtin_tools.py SnapshotParameters` 的 enum 一致;从那里导入常量列表而不是复制字符串,
  若不可导入则定义后加测试断言两者一致)。

## canonical hash(照 `rules/schema.py:300-321` 模式)

`canonical_definition(definition) -> dict`(YAML 输入也先归一化:dimensions None 展开为默认七条、
缺省字段补默认)、`canonical_json`(`sort_keys=True, separators=(",",":"), ensure_ascii=False`)、
`canonical_hash(definition) -> sha256`。哈希**包含** `description`、**排除**全部生命周期字段
(artifact_id/version/created_at/source/proposal_id/evidence_hash)。

## 聚合校验 `validate_definition(definition) -> list[str]`

一次性收集全部错误(照 `rules/validation.py` 风格),至少覆盖:步骤 id 重复 / 非法字符;
depends_on 引用未知步骤或自身;步骤依赖成环(Topo 检测);无 blocking 步骤;applicable_kinds 为空或重复;
dimensions 给定时不是七维恰好各一条;OutputRequirement 参数越界;
required_capabilities/required_outputs 中未知枚举(pydantic 会拒,捕获转错误消息)。
返回空列表即合法。`parse_definition(data: dict) -> MethodPackageDefinition`(yaml.safe_load 后传入,
校验失败抛 `MethodPackageError(errors=[...])`,消息带字段路径)。

## 虚拟 legacy 基线(§8 用)

`LEGACY_BASELINE_DEFINITION: MethodPackageDefinition` —— package_key=`company-research`,
steps = `falsifiable_plan`(blocking,required_outputs=[plan(require_completion_criteria=True,
require_stop_conditions=True), hypotheses(min_count=1)]) + `counter_analysis`(non-blocking,无 outputs)+
`dimension_assessment`(blocking,required_outputs=七条 dimension(minimum_status="missing")),
dimensions=None(默认),discipline=MethodConfig()。附断言注释:与
`ResearchProgress.depth` 对 company_research/review 的现行要求一一对应,不多不少。

## 测试 `tests/unit/test_method_packages.py`

- 同内容同哈希;改 description 变哈希;改 version/source 等生命周期字段不变哈希。
- 合法基线通过校验;环依赖、未知 depends_on、无 blocking、维度缺失/重复、空 kinds、
  非法枚举各自产生含字段路径的错误,且多个错误一次返回。
- dimensions=None canonical 展开后与显式七条等哈希。
- `TOOL_FAMILY_MEMBERS` 每个成员都出现在 `registrations()` 工具名中(直接调用 registrations 断言)。

## 验收

`.venv/bin/ruff check .`、`env API_KEY=dummy-test-key-123456 .venv/bin/python -m mypy src/investment_agent`、`env API_KEY=dummy-test-key-123456 .venv/bin/python -m pytest -q tests/unit/test_method_packages.py` 全绿;
不改动既有文件(本卡只新增)。
