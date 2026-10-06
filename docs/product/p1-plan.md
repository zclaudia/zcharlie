# P1 执行计划：目标池、模板筛选与候选

状态：Ready for execution（第 2 节决策已于 2026-10-06 确认）

日期：2026-10-06

依据：[`funnel-design.md`](funnel-design.md) 第 12 节 P1，数据源结论见其第 15.1 节。

## 0. 目标与完成定义

P1 交付"目标池 → 周频模板筛选 → 候选（附理由与 LLM 起草的规则）→ 用户晋级/忽略"的完整闭环，
并从第一天起积累时点快照，为 P2 区间检测与 P3 回测打底。

P1 分两道门：

- **代码完成**：第 9 节全部任务通过门禁，离线端到端 smoke 跑通。
- **阶段退出**（设计文档原文）：快照连续积累 ≥ 8 周且完整率达标；每周稳定产出候选；
  每条理由可回溯到快照字段；每只候选都有通过校验的规则草稿；因子定义只有一份。

阶段退出依赖日历时间，因此**数据采集链路必须最先上线**（里程碑 M1～M2），其余工作与快照积累并行。

## 1. 现状要点（影响计划的事实）

| 事实 | 影响 |
|---|---|
| 现有单股链路每只股票发 10 次 proxy 请求，每次 1 只 | 沪深 300 全量刷新约 3000 次请求；P1 需要按 3 只一批重写采集，降到约 1000 次 |
| 因子计算分散在 `research/ifind_chain.py`（canonicalize）与 `research_kernel/scoring.py` | 需抽出统一因子库，否则会出现第二套定义 |
| 分类依赖行业**关键词**匹配用户手填的行业（如"电力设备"） | baostock 返回证监会分类（如"C38电气机械和器材制造业"），关键词匹配会失效，需要显式映射表 |
| 已有 `universe` / `universe_membership`（`valid_from` / `valid_to`）表，原为 legacy 保留 | 结构正好适合时点成分，可直接复用，不必新建 |
| 已有 `holding_item` 表（预留未用） | P1 不使用，P2 再评估是否沿用 |
| 队列、租约、心跳、幂等、调度、交易日历都已就绪 | 新增批处理任务全部复用现有 worker 与 `pipeline_run` |
| 数据库迁移当前到 `012` | P1 新迁移从 `013` 起 |

## 2. 已确认的决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 规则草稿何时起草 | 候选产生时自动起草 |
| D2 | 财报"可得日期" | 取**实际披露日**（见 2.1）；查不到时退回法定截止日近似并标记 `approx`；逾期未披露视为不可得并标记 `report_overdue` |
| D3 | 观察端迁移到因子库后分数是否允许变化 | 不允许：golden corpus 逐字段一致，不升 scoring version |
| D4 | 成分表 | 复用 `universe` / `universe_membership` |
| D5 | 回补深度 | 价格 5 年、年报 6 年、最近 4 个季报 |

### 2.1 披露日的取得方式（2026-10-06 实测 `ifind_get_stock_announcement`）

- 公告接口可返回实际披露日（`reportDate`）与标题；如贵州茅台 2024 年年报为 2025-04-03、2021 年年报为 2022-03-31。
- 限制：单次调用最多返回最近 50 条；多只股票合并查询时 `thscode` 为空，无法归属，因此**只能单只查询**。
- 做法：按披露季窗口逐只查询（年报与一季报 3/1～5/15，半年报 7/15～9/5，三季报 10/1～11/5）；
  返回满 50 条时把窗口对半切分重查；按标题正则匹配定期报告并排除"摘要""英文版"。
- 回补成本：300 只 × 6 个年报季 + 最近 4 个季报窗口 ≈ 3000 次调用（约 1.3 小时，40 次/分钟）。
- 日常：交易日对成分股增量查询当日公告，可得日期即实际披露日（真正的时点数据）。
- 逾期：过法定截止日仍未见定期报告的股票，该期视为不可得、沿用上一期，并产生 `report_overdue` 质量标记；
  筛选排除，P2 作为 `exit_review` 的候选触发条件。

## 3. 工作流 A：数据层

### P1-A1 baostock 参考数据 adapter（S）

- 新增依赖 `baostock`；独立模块 `providers/baostock_reference.py`，不与 iFinD 共享代码。
- 接口：`fetch_constituents(index_code, as_of)`、`fetch_industry(as_of, symbol=None)`。
- 代码映射 `sh.600000` ↔ `600000.SH`；`updateDate` 作为生效日期，拒收晚于采集日的结果。
- 登录/登出包装、超时、重试；结果附原始响应用于审计。
- 测试：录制的脱敏 fixture 离线回放；未来日期守卫；空结果（2005 年前）；代码映射往返。

### P1-A2 时点快照表（M）· 迁移 `013`

- `universe` / `universe_membership` 复用（D4），新增 `source`、`captured_at` 列。
- 新表 `industry_classification`：`symbol`、`system`（`csrc`）、`code`、`name`、`valid_from`、`valid_to`、`captured_at`。
- 新表 `market_snapshot`：`symbol`、`trade_date`、OHLC、成交量、总股本、`provider`、`captured_at`、`raw_ref`。
- 新表 `financial_snapshot`：`symbol`、`report_period`、`available_from`、`availability`（`actual` / `approx`）、
  `fields`（JSON，canonical 字段）、`provider`、`captured_at`、`raw_ref`、`is_backfill`。
- 不可变约束：同一主键重采产生新版本行（`version` 列），读取时按 `captured_at <= as_of` 取最新。
- 测试：迁移从空库与从 `012` 升级均可用；时点查询不读到 `as_of` 之后写入的数据。

### P1-A3 iFinD 批量采集器（M）

- 新任务类型 `snapshot_collect`，复用 worker、租约与幂等键（键 = 任务类型 + 日期 + 分片）。
- 按 3 只一批调用 proxy；按配置节流（默认 40 次/分钟，低于 proxy 的 60）；单批失败只影响该批，可重试。
- 子任务：`market_daily`（增量日线）、`financial_period`（指定报告期的利润/现金流/资本结构/盈利能力）。
- 原始响应落盘到制品目录，`raw_ref` 指向文件。
- 完整率统计：每次采集输出 应采 / 实采 / 缺失清单。

### P1-A4 历史回补（M）

- 一次性 CLI：`ia snapshot backfill --universe CSI300 --prices 5y --annual 6 --quarters 4`（按 D5）。
- 可中断续跑（按批次幂等）；回补行标记 `is_backfill=true`。
- 财务数据约 4000 次调用 + 披露日约 3000 次调用，合计约 3 小时（40 次/分钟）；进度与剩余量可查询。

### P1-A6 披露日采集（M）

- 新表 `disclosure_event`：`symbol`、`report_period`、`kind`（annual / semi / q1 / q3）、`disclosed_on`、
  `title`、`source_seq`、`captured_at`。
- 回补：按 2.1 的窗口策略逐只查询；日常：交易日 17:00 增量查询当日公告。
- `financial_snapshot.available_from` 优先取 `disclosed_on`，否则取法定截止日并标记 `approx`。
- 逾期检测：每个法定截止日次日扫描，未见定期报告的成分股写入 `report_overdue`，发 `action_required` 通知。
- 测试：标题匹配（含"摘要""英文版""更正""补充"等干扰）；50 条截断时的窗口切分；逾期判定。

### P1-A5 采集调度（S）

在现有调度器中新增（全部 Asia/Shanghai、仅交易日）：

| 任务 | 频率 | 内容 |
|---|---|---|
| 成分同步 | 每周一 08:00 | baostock 成分 → `universe_membership`；有进出则发 `info` 通知 |
| 行业同步 | 每月首个交易日 08:00 | baostock 行业 → `industry_classification` |
| 日线快照 | 交易日 16:30 | 全成分股 `market_daily` |
| 财报快照 | 每周六 + 4/30、8/31、10/31 后首个交易日 | 最近报告期 `financial_period`，只采尚无该期数据的股票 |

沿用 misfire 宽限、启动补跑与日历到期告警。

## 4. 工作流 B：因子库

### P1-B1 因子库骨架（M）

- 新包 `factors/`：注册表 + 纯函数。每个因子声明 `name`、`version`、`unit`、`inputs`、
  `applicability`（行业组）、`compute(snapshot_view) -> FactorValue`。
- `FactorValue`：`value`、`quality`（`ok` / `missing` / `not_applicable` / `approx`）、`inputs_ref`（快照引用）。
- `snapshot_view(symbol, as_of)`：只读时点视图，是因子唯一的数据入口。
- 首批因子从现有代码搬迁并保持数值一致：ROE、ROE 稳定性、毛利率、净利率、负债率、营收与利润 3 年增速、
  自由现金流、PE、PB、PS、市值、1 年收益、波动率、最大回撤。
- 测试：对 golden corpus 的 13 只样本，因子值与现有 canonical 输出逐字段一致。

### P1-B2 估值分位因子（M）

- `pe_percentile_5y`、`pb_percentile_5y`：每个交易日 PE = 收盘价 ÷ 当日可得的最近年报 EPS（按 `available_from`）。
- 历史不足 5 年按实际长度计算并标记 `approx`；不足 1 年为 `missing`。
- 逾期未披露的报告期不进入计算，沿用上一期 EPS，因子附带 `report_overdue` 标记。
- EPS ≤ 0 的交易日不计入分布（与 v1.3 负 PE 规则一致）。
- 测试：合成序列验证分位；时点边界（披露截止日前后）；亏损年份排除。

### P1-B3 行业适用性映射（S）

- `config/industry_groups.yaml`：证监会大类 → 行业组（`banking`、`insurance`、`utility`、`cyclical`、
  `growth_manufacturing`、`venture`、`consumer`、`other`），版本化。
- 因子 `applicability` 与模板适用行业都引用行业组，不再使用关键词。
- 覆盖校验：沪深 300 当前成分所属的每个证监会大类都必须有映射，否则测试失败。

### P1-B4 观察端改读因子库（M）

- 单股研究链路的 canonical 指标改由因子库计算；`research_kernel` 评分逻辑不变。
- 行业关键词分类改为基于行业组；用户手填行业仍可覆盖。
- 按 D3：golden corpus 结果必须逐字段一致，不升 scoring version。若不一致，单独评估后再决定。
- 删除被替代的重复计算代码。

## 5. 工作流 C：规则

### P1-C1 规则 schema 与校验器（M）

- Pydantic 模型：`RuleTemplate`、`RuleInstance`、条件（`factor` + `op` + `value`，`all` / `any` 组合）、区间枚举。
- 校验：因子存在于注册表；参数在模板 `param_ranges` 内；区间名属于该 `scope` 的枚举；无自由文本条件。
- 序列化为 YAML/JSON，可 diff；测试覆盖每一类校验失败。

### P1-C2 模板 v1（M）

- 把五个策略原型改写为 `config/rule_templates/*.yaml`：适用行业组、`screen` 条件、
  候选区间（`buy_zone` / `drop`）与持仓区间（`add` / `trim` / `exit_review`）默认参数、`param_ranges`。
- 持仓区间在 P1 只定义不执行（P2 使用），但必须通过校验。
- 全部标注 `calibration_status: uncalibrated`。
- 需要用户参与：每个模板的默认阈值由开发者起草、用户逐条确认（见第 8 节）。

### P1-C3 规则引擎 · 筛选（M）

- 输入：目标池时点成分 + 因子值 + 模板 `screen` 规则；输出：命中标的 + 结构化 `reason`（规则、因子、阈值、实际值、快照引用）。
- 纯函数、确定性；同一 `as_of` 与规则版本重复运行结果逐字节一致。
- 行业不适用的因子跳过而非判失败；缺失因子使该条件不成立并记录。
- 每周日 20:00 运行（新任务类型 `screen_run`），结果写入 `screen_result`。

### P1-C4 规则库与版本（S）· 迁移 `014`

- 表 `rule_template`、`rule_instance`（`symbol`、`scope`、`template_ref`、`version`、`status`、`definition`、
  `drafted_by`、`draft_rationale`、`approved_at`）。
- 生命周期 `draft → active → retired`；修改即新版本；模板文件变更时同步入库并升版本。

### P1-C5 规则起草 Agent（M）

- 输入：该股因子值与分位、行业组、可选模板、用户历史驳回理由（最近 N 条）。
- 输出：`RuleInstance` 草稿 + 逐条起草理由；复用现有 OpenAI 兼容客户端、PromptedOutput、超时与重试分类。
- 草稿必须通过 P1-C1 校验，失败重试一次，仍失败则候选标记"草稿不可用"并可人工补写。
- LLM 调用写入 `llm_call` 成本表。
- 离线 eval：固定输入下草稿 100% 通过校验；不出现交易指令语言；参数全部在允许范围内。

## 6. 工作流 D：候选与决策日志

### P1-D1 候选生命周期（M）· 迁移 `015`

- 表 `candidate`：`symbol`、`opened_at`、`expires_at`（默认 14 天）、`sources`（screen / manual）、
  `screen_result_ref`、`draft_rule_ref`、`status`（`open` / `promoted` / `dismissed` / `expired`）。
- 同一标的已有 `open` 候选时合并来源而非重复创建；被忽略的标的 30 天内不再自动提名（可配置）。
- 筛选完成后自动创建候选并按 D1 触发起草。

### P1-D2 决策日志（S）

- 表 `decision`（不可变）：`symbol`、`kind`（`promote` / `dismiss` / `nominate` / `approve_rule` /
  `reject_rule` / `edit_rule`）、`reason_text`、`rule_ref`、`context_ref`（当时的筛选结果与因子快照）、`at`。
- `dismiss`、`reject_rule` 必填理由；理由作为 P1-C5 的偏好输入。

### P1-D3 晋级流程与 API（M）

- `POST /candidates/{id}/promote`：要求批准（可编辑参数后批准）草稿规则 → 规则 `active` → 写入现有 `watchlist_item`
  （行业取证监会分类、报告年度取最近完整财年）→ 触发一次单股研究。
- `POST /candidates/{id}/dismiss`、`POST /candidates`（手动提名）、`GET /candidates`、`GET /screen-runs/{id}`、
  `GET /rules/{id}`、`PATCH /rules/{id}`。
- 全部在一个事务内写决策日志；复用现有鉴权、CSRF 与限流。

## 7. 工作流 E：Web UI

### P1-E1 候选页（M）

- 本周候选列表：标的、行业、命中模板、理由（可点击展开到快照字段与原始响应）、草稿状态。
- 规则审阅：渲染草稿条件与起草理由；参数可在允许范围内编辑；批准晋级 / 驳回 / 忽略（必填理由）。
- 首屏展示理由，不展示 0～100 综合分；全页显示"未校准模板"标记。

### P1-E2 数据健康页（S）

- 成分最近同步时间与变化；各采集任务最近一次完整率与缺失清单；回补进度；快照累计周数（阶段退出指标）。
- 关键指标并入今日概览的 warnings。

## 8. 工作流 F：验收与文档

### P1-F1 离线端到端 smoke（M）

- fixture：baostock 成分与行业 + 10 只股票的 iFinD 快照（覆盖银行、周期、成长、亏损、次新）。
- 流程：成分同步 → 快照采集 → 因子 → 筛选 → 候选 → 起草（stub LLM）→ 批准晋级 → 观察池单股研究。
- 断言：每条理由可回溯到快照；重复运行结果一致；决策日志完整。

### P1-F2 模板阈值评审（S，需用户参与）

- 开发者给出五个模板的默认阈值与依据；用户逐条确认或修改后合入 P1-C2。

### P1-F3 文档（S）

- README、`engineering-baseline.md` 技术决策表（新增 baostock）、运维说明（回补、调度、数据健康）。

## 9. 依赖与里程碑

```text
M1 数据地基          A1 ─┬─ A2 ─┬─ A3 ─┬─ A5     ← 尽早上线，开始积累快照
                         │      │      ├─ A6
                         │      │      └─ A4
M2 因子库                └─ B3  └─ B1 ─── B2
M3 规则与筛选                          C1 ─ C2 ─ C3 ─ C4
M4 候选闭环                                 D1 ─ D2 ─ D3 ─ C5 ─ E1
M5 收口                                     B4 · E2 · F1 · F2 · F3
```

| 里程碑 | 任务 | 完成标志 |
|---|---|---|
| M1 数据地基 | A1、A2、A3、A5 | 日线与成分开始按日/周写入快照（**8 周计时起点**） |
| M2 因子库 | A4、A6、B1、B2、B3 | 回补完成；13 只 golden 样本因子一致；分位因子可算 |
| M3 规则与筛选 | C1、C2、C3、C4、F2 | 周筛选稳定产出带理由的命中列表 |
| M4 候选闭环 | D1、D2、D3、C5、E1 | 浏览器内完成"候选 → 审阅草稿 → 晋级"全流程 |
| M5 收口 | B4、E2、F1、F3 | 代码完成门禁通过；观察端只剩一套因子定义 |

规模：S 约 0.5～1 天，M 约 1～3 天（一名开发者配合编码 Agent）。合计约 18 个任务、3～4 周代码量；
阶段退出另需快照积累满 8 周，从 M1 完成之日起算。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| Kimi 网关上游配额不足以支撑回补 | A4 可中断续跑；先回补价格，再分批回补财务；必要时降低节流 |
| baostock 服务不稳定 | 只影响成分/行业更新，快照已落库；失败发 `action_required` 通知 |
| 因子库迁移导致观察端分数变化 | D3 要求逐字段一致；B4 放在最后，失败可延后到 P2 |
| 模板阈值拍脑袋 | 全部标注 `uncalibrated`；F2 由用户确认；P3 校准 |
| 候选过多导致审阅负担 | 每模板设命中上限（如前 20），超出按理由强度截断并在页面提示 |
