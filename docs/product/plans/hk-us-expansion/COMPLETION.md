# 港股与美股扩展实施 · 完成记录（COMPLETION）

> 由**控制 agent**维护：每个子任务合并后追加一行，每波结束追加一行波次门禁，任何终止/暂停都在"终止记录"里写清。
> 子 agent 不要直接改本文件。

## 执行信息

| 项 | 值 |
| --- | --- |
| 集成分支 | `feat/hk-us-expansion` @ `/Users/zhvala/SourceCode/zcharlie-hkus` |
| 起始提交 | 施工基线 `d5cf937`（**主 checkout 未提交工作区的快照**，见偏差 6）→ 计划与文档修订链在其之下：`8c83241` 计划 → `75d3656` gateway → `81e3775` 基线 → `99be5f5` 波次耦合 → `f147e65` 起点说明 → `14745cb` T0 记录 → `d5cf937` WIP 快照。 |
| 启动时刻 T0 | 2026-10-12T01:36:44+0800（epoch 1791740204） |
| 硬截止 | T0 + 4h = **2026-10-12 05:36:44 CST**（epoch 1791754604）；**到点停止派发新任务** |
| 基线门禁（施工基线 `d5cf937`，02:04 实测） | `pytest -q -m "not network"` = **2392 passed / 1 failed / 3 skipped / 6 deselected**（≈5.4 分钟）；`mypy src` OK（293 files）；`ruff check .` **5 个既存错误**（全在用户 WIP 的 portfolio 文件，非本次代码） |
| 工具链 | `PYTHONPATH=<worktree>/src` + 主 checkout 的 `.venv/bin/python`；`DATA_DIR`/`DATABASE_URL` 指向 `/tmp/zc-hkus-gate-*/` |

## 基线失败清单（后续波次只允许"不新增"）

```
基线（施工基线 d5cf937）：
1) pytest 失败 1 个：tests/unit/test_engine_lifecycle.py::test_api_lifespan_disposes_the_engine_on_shutdown
   （与 zcharlie 记忆里的"用户 WIP 已知失败"一致，非本次引入；不修，也不许因它放宽断言）
2) ruff 既存错误 5 个（全在用户 WIP 的 portfolio 文件，非本次代码；不修，保持快照与主 checkout 一致）：
   src/investment_agent/storage/models/portfolio.py:5:30 F401
   tests/integration/test_portfolio_valuation_jobs.py:9:1 I001
   tests/test_portfolio_import.py:10:1 I001
   tests/test_portfolio_valuation_binding.py:9:1 I001
   tests/test_portfolio_valuation_binding.py:10:21 N817
要求的门槛：全量 pytest 的 failed 数 ≤ 1 且新增失败必须归零；ruff/mypy 的报错不得落在本次改动的文件上。
全量 pytest 单次约 5.4 分钟——预算里要算进去。
```

## 任务状态

| 任务 | 波次 | 分支 | 提交 | 合并 | 定向测试 | 全量 | 偏差/未决 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 01 身份与市场注册表 | W1 | | | | | | |
| 02 资料契约·模型·迁移·仓储 | W1 | | | | | | |
| 03 港股原币适配器 | W2 | | | | | | |
| 04 美股 SEC 适配器 | W2 | | | | | | |
| 05 不可变资料存储·离线导入 | W2 | | | | | | |
| 06 单证券资料采集任务 | W3 | | | | | | |
| 07 资料→证据适配分支 | W3 | | | | | | |
| 08 确定性财务指标 | W3 | | | | | | |
| 09 财务上下文读模型·API | W4 | | | | | | |
| 10 研究档案与发布校验接入 | W4 | | | | | | |
| 11 财务页面展示 | W5 | | | | | | |
| 12 验收矩阵与文档收口 | W5 | | | | | | |
| 13 年度 PE（尾部） | W6 | | | | | | |
| 14 分红事件（尾部） | W6 | | | | | | |

## 波次门禁记录

| 波次 | 合并后 `pytest -q -m "not network"` | ruff | mypy | 备注 |
| --- | --- | --- | --- | --- |
| W1 | | | | |
| W2 | | | | |
| W3 | | | | |
| W4 | | | | |
| W5 | | | | |
| W6 | | | | |

## 偏差登记（跨任务）

| # | 内容 | 理由 | 处置 |
| --- | --- | --- | --- |
| 1 | 不引入 EdgarTools 依赖 | 离线环境无该包；设计要求版本选择与资格判断归属应用 | 自有有界解析；将来引入需固定版本并重跑版本案例 |
| 2 | 不新增运行时设置项（限速/UA/在线开关用模块常量） | 避免 `config.py` / `settings/registry.py` 镜像面与用户正在改动的设置 UI 冲突 | 需要运行时可调时走 settings 流程另开任务 |
| 3 | 在线刷新默认关闭（`ONLINE_REFRESH_ENABLED=False`） | 东财/Yahoo/HKMA 使用许可未确认 | 许可确认后打开并补联网验收 |
| 4 | 发布只支持原币，跨币种仍拒绝 | 汇率时间点与使用许可未验收 | 汇率能力验收后另开任务 |
| 5 | 实施在独立 worktree + 分支，不合并 main、不 push | 主 checkout 有 200+ 用户 WIP，避免把用户改动卷入提交 | 由用户决定合并时机 |
| 6 | **施工基线改为"用户 WIP 快照" `d5cf937`** | 计划编写时依据的是主 checkout 的**工作区**状态，但 main 的已提交树缺少 `evidence/`、`investor/` 两个包与迁移 025–045（它们只是未提交改动）。按原基线施工会让 07（资料→证据）与 10（研究档案/发布）没有可实现的对象，且交付物与用户即将提交的代码无法合并 | 快照是复制而非 stash/checkout：主 checkout 的工作区、索引、main 全程未被改动。用户自行提交那批改动后，用 `git rebase --onto main d5cf937 feat/hk-us-expansion` 丢掉本快照，只保留港美股扩展的实现提交。快照不含 `research/`（448MB 未跟踪调研材料）与 .gitignore 覆盖路径 |

## 终止记录（仅在触发时限或异常停止时填写）

- 触发原因：（4 小时硬截止 / 其他）
- 停止时刻：
- 已完成并合并的任务：
- 已提交但未合并的分支：
- 未启动/中断的任务及其原因：
- 恢复入口（下一条命令 / 需要人工决定的事）：
- 已跑的最终门禁数字：
