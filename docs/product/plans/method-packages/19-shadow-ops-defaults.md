# 卡 19:候选影子运营默认值(最小正例数与预算上限)

设计依据:§12 开放问题 4、§7.3。前置:M4(卡 15/16)。本卡只影响**证据收集成本**,
不得降低 §7.3 质量门槛(全覆盖 + 一个被拒反例)。

## 1. Settings(运营默认值,env 可覆盖)

`src/investment_agent/config.py` 的 `Settings` 新增两个字段(带默认,不破坏既有构造):

- `method_candidate_shadow_min_positives: int = Field(default=1, ge=1)`
  ——批准前需要的 matched 正例候选影子数(每个 expected="pass"、matched、outcome="passed")。
  默认 1 = 现行为;只允许调高,ge=1 保证不会低于现有门槛。
- `method_candidate_shadow_max_per_proposal: int = Field(default=8, ge=1)`
  ——每个包提案允许发起的候选影子任务数上限(含批量),是证据收集成本的硬上限。

## 2. 服务层(`investor/methods.py`)

- `_require_candidate_shadow(row, *, min_positives: int = 1)`:
  保留全部现有校验(最新 matched 正例覆盖全部 `requires_candidate_shadow` + 至少一个
  matched expected="block" 反例)不变;新增「matched 正例记录数 >= min_positives」,
  拒绝消息点名已有数与要求数。**覆盖率判断不放宽**:每个 counted 正例仍以登记时
  冻结的 requirement_ids 记录,最新一条必须全覆盖(维持现状)。
- `decide(..., base: "Settings | None" = None)`:新 keyword-only 可选参,
  `min_positives = base.method_candidate_shadow_min_positives if base is not None else 1`。
  `base` 省略时行为与现 HEAD 逐字节一致(既有 `tests/test_method_evolution.py`
  直接调 `svc.decide(request, actor=...)` 的用例不改不删、必须原样通过)。
- `start_candidate_shadow(request, *, base)`:发起前统计该提案已有
  `kind="candidate_shadow_claim"` 工件数,达到 `base.method_candidate_shadow_max_per_proposal`
  即拒绝(`InvestorError("invalid_reference", ...)`,消息含上限与已用数)。
  预算沿用既有 mandate daily/task cost,不改。

## 3. 路由(`api/routes/methods.py`)

`POST /investor/methods/decisions` 增加 `settings: BootstrapSettings` 依赖并传入
`svc.decide(body, actor="authenticated_user", base=settings)`。

## 测试(新文件 `tests/unit/test_candidate_shadow_ops.py`)

复用 `tests/unit/test_candidate_shadow.py` 的 `shadow_proposal` / `shadow_ready_mandate` /
`start_shadow` / `run_shadow` / `release_fixture_budget` 与
`tests/test_method_evolution.py` 的 `register_candidate_shadow`、`decision_for` 惯例:

1. 默认 Settings(不传 base):既有门槛行为不变——block 反例 + 1 个正例即可批准。
2. `min_positives=2`:只有 1 个 matched 正例时 `decide` 拒绝,消息含「正例」与数量;
   再登记第 2 个 matched 正例后批准通过。覆盖率语义不降:第 2 个正例也须 matched。
3. `max_per_proposal` 用 `shadow_settings(method_candidate_shadow_max_per_proposal=2)`:
   第 3 次 `start_candidate_shadow` 被拒;前两次正常入队。
4. `decide` 不传 base(默认 1)与传 `base` 且 min=1 行为一致。

## 验收

三门禁 + 目标测试全绿;`tests/test_method_evolution.py`、`tests/unit/test_candidate_shadow.py`
既有断言零改动;不触碰用户在途文件(见 m4-plan 全局约束 6)。
