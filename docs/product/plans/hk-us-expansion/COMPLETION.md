# 港股与美股扩展实施 · 完成记录（COMPLETION）

> 2026-10-15 后续阶段：HK2-04 三项 P0 收口及 HK2-07 前置条件见 [25](25-hk2-04-closeout-acceptance.md)。以下原实施批次记录保持不变。

> 本文保留原实施批次的历史记录；该批代码已进入 main，旧分支与 worktree 已删除。历史哈希不表示当前可执行的合并步骤。
> 由**控制 agent**维护。本次实施在 4 小时硬截止前完成全部 14 个子任务，因此没有"终止记录"，只有收口记录。

## 执行信息

| 项 | 值 |
| --- | --- |
| 原集成分支（已删除） | `feat/hk-us-expansion`，原目录 `/Users/zhvala/SourceCode/zcharlie-hkus`；已交付至 main，修复基线 `79148be` |
| 起始提交 | 施工基线 `d5cf937`（**主 checkout 未提交工作区的快照**，见偏差 6）→ 其下为计划与文档修订链 `8c83241` → `75d3656` → `81e3775` → `99be5f5` → `f147e65` → `14745cb` |
| 启动时刻 T0 | 2026-10-12T01:36:44+0800（epoch 1791740204） |
| 硬截止 | T0 + 4h = 2026-10-12 05:36:44 CST（epoch 1791754604） |
| 实际收口 | 见"收口记录"；全部 14 个任务在截止前完成并合并，未触发终止规则 |
| 基线门禁（`d5cf937`） | `pytest -q -m "not network"` = **2392 passed / 1 failed / 3 skipped**；`mypy src` OK（293 files）；`ruff check .` 5 个既存错误（全在用户 WIP 的 portfolio 文件） |
| 工具链 | `PYTHONPATH=<worktree>/src` + 主 checkout 的 `.venv/bin/python`；`DATA_DIR`/`DATABASE_URL` 指向 `/tmp/zc-hkus-gate-*/` |

## 基线失败清单（只允许"不新增"）

```
1) pytest 失败 1 个：tests/unit/test_engine_lifecycle.py::test_api_lifespan_disposes_the_engine_on_shutdown
   顺序/状态相关（fresh gate dir 全量运行下有时通过、单独跑必失败，报 no such table: app_setting_meta）。
   属用户 WIP 既有问题，全部波次均未修、未放宽断言。
2) ruff 既存错误 5 个（全在用户 WIP 的 portfolio 文件，本次未修，保持快照与主 checkout 一致）：
   src/investment_agent/storage/models/portfolio.py:5:30 F401
   tests/integration/test_portfolio_valuation_jobs.py:9:1 I001
   tests/test_portfolio_import.py:10:1 I001
   tests/test_portfolio_valuation_binding.py:9:1 I001、:10:21 N817
3) 另观察到一次并发导致的偶发失败：tests/integration/test_clean_environment_smoke.py
   （子进程/端口型 smoke，重跑通过；见偏差 12）
```

## 任务状态（14/14 完成）

| 任务 | 波次 | 分支 | 提交 | 合并 | 定向测试 | 全量（各任务自测） | 偏差/未决 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 01 身份与市场注册表 | W1 | hk-us/01 | `b51805f` | `a19cdd0` | 34 | 2427/0/3 | 5 条偏差（见偏差 7） |
| 02 资料契约·模型·迁移·仓储 | W1 | hk-us/02 | `fafb0fb` | `734be28` | 75 | 2467/1/3 | 8 条偏差（见偏差 8） |
| 03 港股原币适配器 | W2 | hk-us/03 | `9211d3d` | `b6e28e6` | 27 | 2528/1/3 | `subject_id` 需注入；`unmapped_items` 为信息性（见偏差 9） |
| 04 美股 SEC 适配器 | W2 | hk-us/04 | `6a9f47f` | `feb6370` | 57 | 2558/1/3 | `days` 用排他跨度；ProfitLoss 标 `equity_scope=total`；TSM 股数未映射（见偏差 10） |
| 05 不可变资料存储·离线导入 | W2 | hk-us/05 | `8cf8b90` | `7680aa5` | 21 | 2522/1/3 | 适配器参数注入；`by_content_hash` 非全局唯一需比对键 |
| 07 资料→证据适配分支 | W2 | hk-us/07 | `4ee95f4` | `9d86877` | 24 | 2525/1/3 | `date_precision` 可选字段；`source_ref` 非事实级唯一键 |
| 08 确定性财务指标 | W2 | hk-us/08 | `94cb978` | `550388c` | 41 | 2542/1/3 | 年维度每年一条结果；`is_bank` 由调用方显式传 |
| 06 单证券资料采集任务 | W3 | hk-us/06 | `5528ab1` | `406fe75` | 59 | 2721/1/3 | `online_refresh_disabled` 默认关闭；多响应来源不落单文件 raw |
| 10 研究档案与发布校验接入 | W3 | hk-us/10 | `1e1d21c` | `c793d9b` | 101 | 2693/1/3 | 原币发布；跨币种仍拒绝；公司级 dataset 绑定校验 |
| 09 财务上下文读模型·API | W4 | hk-us/09 | `2988fb7` | `d2ac41e` | 18 | 2761/1/3 | 银行集合硬编码；GET 零上游；POST 去重 |
| 11 财务页面展示 | W5 | hk-us/11 | `d6be5ab` | `9dffad3` | 14 | 2776/1/3 | 服务端渲染；港美页不含 A 股专用区块；浏览器复核未做（子 agent 无浏览器工具，改真实实例 HTTP 复核） |
| 12 验收矩阵与文档收口 | W5 | hk-us/12 | `856d673` | `69eaf59` | — | 2761/1/3 | 只改 3 个文档；11/13/14 的行交由控制 agent 在最终门禁确认 |
| 13 年度 PE（尾部） | W5 | hk-us/13 | `9af49fd` | `988d44a` | 24 | 2785/1/3 | 能力位三位全关；ADS 恒不适用；汇率越界用 `STALE_INPUTS` |
| 14 分红事件（尾部） | W5 | hk-us/14 | `9ea1970` | `7c8ad28` | 41 | 2802/1/3 | 能力位两位全关；事件数据仍为离线导入；股息率需调用方声明覆盖 |

说明：全量列是各任务**在自己分支上**的实测值（只含自身新增测试），故逐行递增；集成后的波次门禁见下表。

## 波次门禁记录（集成分支，每次都是完整 `pytest -q -m "not network"`）

| 波次 | 合并内容 | passed / failed / skipped | ruff | mypy | 备注 |
| --- | --- | --- | --- | --- | --- |
| 基线 | `d5cf937` 快照 | 2392 / 1 / 3 | 5 既存 | OK（293 files） | 施工基线 |
| W1 | 01 + 02 | 2501 / 1 / 3 | 5 既存 | OK | 2501 = 2392 + 34 + 75 |
| W2 | + 03/04/05/07/08 | 2671 / 1 / 3 | 5 既存 | OK | 2671 = 2501 + 170 |
| W3 | + 06 + 10 | 2743 / 1 / 3 | 5 既存 | OK | 2743 = 2671 + 50 + 22 |
| W4 | + 09 | 2761 / 1 / 3 | 5 既存 | OK | 2761 = 2743 + 18 |
| W5 | + 11/12/13/14 | 2841 / 1 / 3 | 5 既存 | OK（313 files） | 2841 = 2761 + 15 + 24 + 41；最终门禁，344.92s |

无任何新增失败：每一波的 failed 恒为 1，且恒为同一个既存顺序相关用例。

## 交付物

- `src/investment_agent/materials/`（15 个模块）：`contracts`（冻结契约 + payload 双向读写）、`hk_native`（东财原币三表）、`us_sec`（CompanyFacts/Submissions + 申报版本状态机）、`store`（不可变资料 + 原始制品）、`offline_import`（离线导入）、`jobs`/`handlers`（单证券采集任务）、`evidence`（资料→证据）、`metrics`（六项确定性指标与适用性）、`context`（只读读模型）、`refresh`（刷新入口）、`investor_bridge`（研究档案/发布）、`valuation`（年度 PE 门控）、`dividends`（分红事件）
- `domain/security.py` + `config/research_material/pilot_list.yaml`（八家受控身份）
- `storage/models/research_material.py` + `alembic/versions/046_research_material.py` + 仓储
- `api/routes/research_materials.py` + `api/schemas/research_materials.py`（GET financial-context / POST refresh）
- Web：`web/routes.py` + `web/templates/stock.html` + `web/static/financials.js`
- 文档：`docs/product/hk-us-expansion.md`、`docs/product/plans/hk-us-expansion/12-acceptance.md`（验收矩阵）、`README.md`（API 表 / 文档索引 / 已知限制）
- 测试：约 460 个新用例（14 个测试文件 + fixtures）

## 偏差登记（跨任务）

| # | 内容 | 理由 | 处置 |
| --- | --- | --- | --- |
| 1 | 不引入 EdgarTools 依赖 | 离线环境无该包；设计要求版本选择与资格判断归属应用 | 自有有界解析；将来引入需固定版本并重跑版本案例 |
| 2 | 不新增运行时设置项（限速/UA/在线开关用模块常量） | 避免 `config.py` / `settings/registry.py` 镜像面与用户正在改动的设置 UI 冲突 | 需要运行时可调时走 settings 流程另开任务 |
| 3 | 在线刷新默认关闭（`ONLINE_REFRESH_ENABLED=False`） | 东财/Yahoo/HKMA 使用许可未确认 | 许可确认后打开并补联网验收 |
| 4 | 发布只支持原币，跨币种仍拒绝 | 汇率时间点与使用许可未验收 | 汇率能力验收后另开任务 |
| 5 | 实施在独立 worktree + 分支，不合并 main、不 push | 主 checkout 有 200+ 用户 WIP，避免把用户改动卷入提交 | 由用户决定合并时机 |
| 6 | **施工基线 = 用户 WIP 快照 `d5cf937`** | main 的已提交树缺 `evidence/`、`investor/` 与迁移 025–045（只是未提交改动）；按原基线施工会让 07/10 无可实现对象 | 复制而非 stash：主 checkout 工作区/索引/main 全程未动。本项仅记录当时施工背景；交付已进入 main，快照哈希不在当前交付祖先链中，不再执行 rebase。快照不含 `research/` 与 .gitignore 覆盖路径 |
| 7 | 01：加载期校验比计划更严（拒绝非法 canonical ticker/缺 ADS 比例/货币非三字母/美股无 SEC 绑定等）；两阶段写入使拒绝零写入 | 名单是身份权威，错行应整文件拒绝 | 已接受；`ensure_controlled_listings` 规则 1–4 不变 |
| 8 | 02：`FACT_ENTRY_KEYS` 14 键（含 `conflict_note`/`source_version`）；`MaterialAdapter.identity` 用 `Any`；raw_value 比"Decimal 可解析"更严；**14 个既有测试的 head 断言改为 `ScriptDirectory.get_current_head()`** | 新增迁移 046 的必然连带；同一波次不得 import 01 | 已接受；后续任务引用常量而非写死键集合 |
| 9 | 03：`parse()` 增加 `subject_id` 关键字（不注入即 `ValueError`）；`unmapped_items` 作为信息性 problem 进 `problems` | 冻结身份无行 id；结构信息必须暴露 | 06 已按裁决区分信息性/结构性 problem |
| 10 | 04：`FactPeriod.days` 用排他跨度（AAPL 53 周 = 370 而非 371）；`us-gaap:ProfitLoss → profit_parent` 标 `equity_scope=total`（官方定义含 NCI）；TSM 期末股数未映射（唯一候选定义不同） | 契约冻结 `(end-start).days`；口径必须可见而非隐藏 | 已接受；若产品需要 TSM 股数需单独裁决 |
| 11 | 06：`research/worker.py` 改 2 行（import + 注册）并连带改 `tests/unit/test_job_dispatch.py` 一行 kind 集合断言 | 新增 kind 必然改变 kind 集合断言 | 已接受；改动可独立回退 |
| 12 | 并发门禁干扰：某任务曾用无差别 `pkill -f "pytest -q -m"` 杀掉并行任务的全量运行；另有 `test_clean_environment_smoke` 一次偶发失败 | 多 worktree 并行时的进程/端口竞争 | 后续任务已在提示中禁止 `pkill`；smoke 偶发失败不计为回归（重跑通过） |
| 13 | 11/12 与计划的文件清单差异：11 另改 `web/static/app.js`（唯一脚本入口，属 web/** 所有权内）、未改 `base.html`；12 未新增 README 正文段落（并入文档索引条目） | 保持最小触碰 | 已接受 |
| 14 | 13：`SPLIT_EVENT_KEYS` 别名（`apple → AAPL`）让拆股规则对真实身份可触发；ADS 分支优先级最高 | 冻结 `CompanyProfile` 用名单 `company_key` | 已接受；ADS PE 仍需带来源的比例与有效期 |
| 15 | 14：`DividendEvent` 增 `fiscal_year`、`announced_on` 可空；`dividend_yield` 增关键字参数（`fiscal_year`/`as_of`/`coverage`）；ADS 净到账独立入口 | "按财年已宣告"必须按财年归属；完整覆盖不能从手上事件反推 | 已接受；原四参调用仍合法（返回 `MISSING_INPUTS`） |

## 未结事项（留给人工/后续）

1. **最新实际年报补验**：需联网逐家核对（本地证据只覆盖 2021–2024）；不得把 2024 当"自动最新"。
2. **在线刷新开关**：使用许可确认前保持关闭；打开后需补多日 fresh fetch、schema 变动与限速验收。
3. **多响应来源的原始正文**：在线与离线的多响应年度资料均无单文件 `raw_ref`，在线 metadata 只含响应摘要；离线必须保留来源证据目录及边车。完整来源级归档仍需另行实现；本次修复不将摘要当作正文。
4. **指标→发布未自动串联**：`materials/metrics.py` 的 `MetricResult` 尚未映射到 `save_material_calculation`；10 的测试里 `fcf_simple` 是手工相减后落计算记录的。
5. **能力位仍全关**：`price`/`annual_pe`/`dividends`/`pb`/`online_refresh` 默认 False，缺口在页面与读模型里可见。
6. **浏览器像素级复核**：11 只做了真实实例的 HTTP/DOM 文本复核（子 agent 无浏览器工具），交互与像素级确认可由用户在具备浏览器工具的会话补。
7. **快照的处置**：已随原交付完成；代码已进入 main，旧工作树和分支已删除，不执行历史 rebase 指令。

## 收口记录（全部任务完成，无终止触发）

- 结果：**14/14 子任务全部实现、合并并通过门禁**；未触发 4 小时终止规则（硬截止 05:36:44）。
- 合并方式：每个子任务一个独立 worktree + 独立 sub-agent，控制 agent 逐波合并（`--no-ff`），14 次合并**零冲突**。
- 时间分布（实测）：T0 01:36:44 → 基线快照与重测 02:04 → W1 收口 02:23 → W2 收口 02:46 → W3 收口 03:12 → W4 收口 03:28 → W5 收口 03:47 → 收口记录 04:00 前后。最大的额外开销是发现"已提交树 ≠ 工作区"并重建施工基线（约 30 分钟）。
- **最终门禁（集成分支，2026-10-12 03:54 实测）**：
  - `pytest -q -m "not network"` = **2841 passed / 1 failed / 3 skipped / 6 deselected**（344.92s）。唯一的 1 failed = `tests/unit/test_engine_lifecycle.py::test_api_lifespan_disposes_the_engine_on_shutdown`（基线既存、顺序相关）。**相对基线零新增失败**（2392 → 2841，+449 全部为本次新增用例）。
  - `mypy src` = Success, no issues found（313 source files；基线 293）。
  - `ruff check .` = 5 个**既存** portfolio 错误，与基线逐行一致；本次改动的文件零报错。
- 独立复核（控制 agent 亲自跑，不依赖子 agent 结论）：golden fixture 零改动；`research/` 零提交；`pyproject.toml`/`uv.lock` 零改动；`config/app.yaml`/`config.py`/`settings/**` 零改动；`evidence/` 仅 07 的加法（contracts.py +25/−1、store.py +58，A 股分支未动）；新增 18 个测试文件（17 个测试模块 + 1 个共享 helper）、`materials/` 15 个模块约 10,092 行；相对施工基线共 104 文件、+28,638/−66。
- 验收：设计 §实施阶段与验收 的 10 条强制用例全部有可执行落点，逐条结果见 [`12-acceptance.md`](12-acceptance.md) §1；其中用例 7 与 11/13/14 的行由控制 agent 在最终门禁补判为通过。唯一未覆盖的是"最新实际年报验收"（需联网，见未结事项 1）。


## 评审修复（2026-10-12）

施工基线：main `79148be`；修复分支 `codex/hkus-review-fixes`，独立目录
`/Users/zhvala/.codex/worktrees/hkus-review-fixes/zcharlie`。主 checkout 的未提交工作未改动。
本节描述修复后的行为，前述波次表与门禁数字保留为历史记录。

| 评审问题 | 修复与验证 |
| --- | --- |
| 缺少实际初始化/导入入口 | 新增 `ia materials init/import`，幂等登记八家公司、三个市场、证券与来源绑定；拒绝冲突时整个初始化回滚；真实适配器导入后 API/页面可读 |
| 失败 HTTP 响应被当作财报 | 边车显式非 2xx 或非法状态拒绝，哈希/字节数不符拒绝；两个真实适配器也拒绝已知非 2xx 响应 |
| 只有 ORM 不可变监听 | 新增迁移 047，SQLite 禁止 UPDATE/DELETE，以及绕过删除触发器的 INSERT OR REPLACE；create_all 与迁移环境保护一致；升级保留原行、降级撤销保护 |
| 来源绑定结束日仍有效 | 两处查询改为 `valid_to > at`，结束日只匹配新的绑定 |
| 离线适配器接线与财务完整性 | 注入真实公司 id，补读 SEC 历史分片；港股三表按来源/年度合并保存，旧逐表版本仍完整可读且不重复计数；移除测试中掩盖调用错误的包装器 |
| 完成记录/格式 | 删除不可用的快照 rebase 步骤，说明原交付已进入 main；补可运行 CLI 用法、修正多响应原件说明，清理文件尾多余空行 |

验证记录：

- 定向回归（十个测试模块）：**182 passed**（25.62s），覆盖真实汇丰六个 FY2024 原币金额、四个财年、三表同时显示、SEC 两个历史分片、重复导入、旧数据兼容、初始化回滚与 SQL 替换保护。
- 最终全量离线回归：**2977 passed / 3 skipped / 6 deselected**（451.86s），无失败；三个跳过项需要构建好的 zclaudia-gateway，六项联网测试排除。日志：`/tmp/zc-hkus-final-6aYVLn/pytest.log`。
- `ruff check .`：全部通过；`git diff --check`：通过。
- `mypy src`：六个既存错误（rules/generation、settings/loader、portfolio/valuation_jobs、agent/budget）。用 `git archive 79148be src pyproject.toml` 的独立基线重新检查，错误逐项一致；本次无新增类型错误。
- 现有完整调研目录实际重放：**37 个新版本**（港股三家公司 × 四年 = 12，美股五家公司年度资料与索引 = 25）；退出 **2**，SEC 索引引用的多份历史分片未保存，汇丰 SEC 文件命名需按用法整理。不把这些材料标作完整来源覆盖。验证数据库与日志位于 `/tmp/zc-hkus-import-ZcjPym/`，未写应用数据库。

仍保留的边界：在线刷新和行情/估值/分红能力位未开启；付费或使用许可未确认的来源未调用。
最新年度覆盖、严格历史时点重建、完整多响应原件归档仍未验收。真实调研材料缺失不会被代码补造。


## 合回 main（2026-10-12）

- 合并提交：`dc4e672`，将修复提交 `42da887` 合入 main `c238862`；无冲突。
- 合并后定向回归：十个测试模块 **182 passed**（20.29s）。
- 合并后静态检查：`mypy src` 成功（312 个源文件），`ruff check .` 全部通过。
  main 已包含此前六个类型错误的修复，本轮不再有遗留类型错误。
- 合并前的六个未提交文件逐一比对 SHA-256，内容全部保留，未纳入合并提交。
- 本次只合并代码，未执行应用数据库迁移；运行环境需执行 `ia migrate up` 应用 047 的不可变保护。
