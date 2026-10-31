# 卡 11:§8 包级加强偏序(纯函数)

设计依据:`docs/product/investor-agent-method-packages.md` §8。前置:M1–M3 已交付。
只改 `src/investment_agent/investor/method_packages.py` 与其测试。

## 新增函数

1. `strictly_stronger_requirements(old, new) -> tuple[str, ...]`
   —— 列出**研究要求**层面的严格增强项(用于「至少一项严格增强」与卡 13 的实质要求判定),
   每项一个稳定标识(如 `blocking:<step_id>`、`outputs:<step_id>:<type>[:param]`、
   `dimension_min:<dimension>`、`discipline:<flag>`、`kinds`)。
2. `strengthening_violations(old: MethodPackageDefinition, new: MethodPackageDefinition) -> list[str]`
   —— §8 表逐条,违反项带字段路径,一次性聚合:

| 维度 | 判定(违反时加入列表) |
|---|---|
| applicable_kinds | new ⊅ old(缩小适用范围) |
| discipline | 任一开关 true→false |
| 步骤集合 | old 的 step id 在 new 中缺失(删除或改名) |
| 固定语义 | 同 id 的 instruction_kind 变化 |
| blocking 升级 | 允许 false→true(不算违反);**不得** true→false |
| outputs | 同 id 的 required_outputs 缩小:min_count 降低、dimension/field/method 集合缩小、参数收紧(require_completion_criteria/require_stop_conditions/verified_only/require_calculation true→false、minimum_status 提高→算收紧吗?不——minimum_status 提高是**增强**不是违反;检查方向:放松才违反) |
| 依赖 | 同 id 的 depends_on 缩小;或新图成环(复用 `_cycle_members`) |
| 缺口 | 步骤或维度 allow_missing false→true、allow_not_applicable false→true、维度 minimum_status 降低 |
| 能力前置 | 既有步骤(同 id)的 required_capabilities 集合变化(§8:必须不变,增减都算) |

   注意方向:**「违反」=放松或破坏结构**;增强方向(升 blocking、加输出、加依赖、提最低状态)
   合法且应出现在 `strictly_stronger_requirements` 里。版本号/哈希/description/title 变化既不算
   违反也不算增强。
3. `legacy_package_for(binding_config: MethodConfig) -> MethodPackageDefinition`
   —— v1 旧方法的比较基准:`LEGACY_BASELINE_DEFINITION` 的拷贝、`discipline` 换成传入 config
   (v1 的三个开关是旧方法仅有的可变部分)。「新增 v2 元数据本身不算研究增强」由此自然成立。

## 测试(扩展 `tests/unit/test_method_packages.py`)

- 每条违反一个最小反例 + 一个合法增强正例(增强不出现在 violations、出现在 strictly_stronger)。
- 「无任何研究要求增强」(仅改 description/title)→ violations 空,但 strictly_stronger 也空
  → 上层会拒(卡 12 用;此处断言两函数输出)。
- v1 比较:`legacy_package_for(MethodConfig(require_two_counter_hypotheses=True))` 相对默认
  legacy:discipline 增强成立、无违反;反向(true→false)有违反。
- 删除/改名步骤、同 id 换 instruction_kind、缩 depends_on、动既有步骤 required_capabilities 各自违规。

## 验收

三门禁 + 目标测试全绿;不动其他文件。
