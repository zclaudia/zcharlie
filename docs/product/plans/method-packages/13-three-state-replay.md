# 卡 13:三态历史回放(evaluate / check_sample 扩展)

设计依据:§7.1。前置:卡 11、12。这是 M4 的判定核心,新逻辑尽量落在
`agent/method_validation.py`(回放构造与逐要求判定),`investor/methods.py` 只做编排。

## 回放上下文构造

新函数 `replay_context(session, review, candidate_package, *, renderer_version, validator_version) -> MethodValidationContext`:

- `binding`:候选 MethodBinding v2(package_ref=提案冻结的工件、package_hash、
  config=package.discipline、listing_ref=该样本 scope);
- `package`:candidate_package(**不得读当前 head**,不得只换 method_config 复用旧任务 binding);
- `grant`:review 冻结的 admission grant;
- `first_plan` / `committed_actions` / `artifact_refs`:从 review 冻结的 task 数据重算
  (actions 按 committed_sequence 排序,历史 NULL 序列原样保留);
- `tool_family_members`:候选包 required_capabilities 的展开快照。

## 逐要求三态判定

新函数 `replay_requirements(context, progress) -> tuple[dict[str, Literal["pass","block","not_replayable"]], list[str]]`
——以候选包每个 blocking 步骤的每个 required_output 为单位:

**可重算**(仅依赖 progress 字段与已保存输出,不依赖 step_results/plan_bindings 元数据):
- `plan` / `hypotheses` / `dimension` / `counter_refs`(含 verified_only 的逐项核验)、
  `calculation`(fact_refs/calculation 引用中存在 method 匹配且可解析)、
  `snapshot_field`(committed capture_snapshot 动作参数命中字段)。
**不可重算**(历史 v1 输出没有这些元数据,判 `not_replayable`,不得当 pass):
- blocking 的 plan_bindings 覆盖、step result 存在性(exactly-one)、action_ids 归属、
  `peer_context` 的绑定一致(历史无冻结 peer 绑定可比)、依赖顺序(committed_sequence 为 NULL)。

聚合到样本级 `pass | block | not_replayable`:

- 全部可重算要求通过 → 仍需看是否存在不可重算要求:有 → 样本 `not_replayable`
  (其不可重算要求 id 计入 `requires_candidate_shadow` 集合);无 → `pass`。
- 任一可重算要求失败 → `block`,并记录失败要求 id 集合。

## evaluate() 编排扩展(仅包提案;config-only 路径一行不改)

- 失败样本:候选判定须为 `block` **且**失败要求与
  `strictly_stronger_requirements(old_pkg, candidate)` 有交集(实质要求);
  仅仅「缺 step result / not_replayable」不构成有效失败证据 → 判定不满足,evaluation failed。
- 范围外反例:沿用其原方法(样本原 binding 的 config/包),须 `pass`(沿用现有
  baseline 判定路径;原方法若有不可重算项,范围外不涉及候选,按现行 pass 语义)。
- 范围内反例:候选判定 `pass` → pass;`not_replayable` → 不判失败,其未覆盖项并入
  `requires_candidate_shadow`;`block` → evaluation failed(提案会挡掉好任务)。
- evaluation 工件 payload 增补:每样本 `{state, replayed: {requirement: state}, blocked_requirements,
  requires_candidate_shadow}`;`passed` 语义 = 原 complete 条件 + 失败样本实质 block +
  范围内反例无 block(requires_candidate_shadow 非空不阻断,留给前瞻影子覆盖)。
- proposal payload 记录累计 `requires_candidate_shadow`(供卡 15/16 覆盖门槛)。

## 测试(新增 `tests/unit/test_method_replay.py` + method_evolution 扩展)

用现有 frozen review 造数模式(真实任务跑完 → process_review → proposal):

1. 历史成功任务 + 候选加了 `dimension` 最低状态要求(该维度实际达标)→ 可重算 pass;
   因候选含 blocking 新步骤 → 样本 not_replayable,该步骤入 requires_candidate_shadow。
2. 历史失败任务 + 候选加强对应要求 → 实质 block;候选加强无关要求(历史输出恰好也满足)
   → 非实质,evaluation failed。
3. 历史成功任务 + 候选要求维度 evidenced 但实际 missing → 范围内反例 block → failed。
4. committed_sequence 为 NULL 的历史动作 → 顺序类要求 not_replayable。
5. config-only 提案:现有 evaluate 测试全部原样通过。

## 验收

三门禁 + 新旧目标测试全绿;`tests/test_method_evolution.py` 既有断言零改动。
