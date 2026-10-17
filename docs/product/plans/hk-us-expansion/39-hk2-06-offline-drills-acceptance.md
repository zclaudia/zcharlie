# HK2-06 离线工程收口与 D0 准备验收

日期：2026-10-17（Asia/Shanghai）。基线 `acb0856`（本地 main 最新已提交 HEAD），
独立分支 `codex/hk206-offline-drills`，工作树 `/Users/zhvala/.codex/worktrees/hk206-drills/zcharlie`。
三个并行子代理分别交付恢复/版本演练、停止演练与证据契约、D0 清单与操作手册；
主代理确定共同证据契约、审查实现并执行集成门禁。本轮**不启动真实三日观测**：
SEC 与东财真实合格观测仍各 **0/3**。

## 范围与边界

- 只做离线工程收口与真实观测前置准备；未发出任何真实上游请求，未操作生产库，
  未部署、未执行定时任务，未改动 `source_policy.yaml`（仍 licensing unknown /
  technical not_accepted）与 `observation_plan.yaml`。
- `ONLINE_REFRESH_ENABLED=False` 保持；两来源运维开关默认 false；演练内开关翻转均为
  进程内 monkeypatch，每个演练结束恢复并在步骤中断言产品默认仍为 False。
- 全部演练为 MockTransport/FakeFetcher/本地录制的 simulation；主 checkout 的 P4
  未提交工作未进入本分支（其新增脏项经核对均属用户并行工作，无 HK2-06 路径）。

## 交付与落点

| 项目 | 实现 |
| --- | --- |
| 恢复与版本演练 | `tests/integration/test_hk206_recovery_drills.py`（7 测试）+ `_hk206_drill_support.py`：真实链路（enqueue → ResearchWorker → MaterialCollectionHandler → MaterialStore → archive → replay / run_source_probe）上的 restart_resume（已完成来源零重 fetch、幂等重入、中断前 500 响应体保留归档）、版本恒等（同 digest 重存/重采集 created=False、原件 mtime 不变）、合法修订（revision 2 + previous_id 链、旧版本 history/by-id 可读、旧采集链接仍解析旧版本、仅 fetched_at 变化不产生新版本）、迁移断网重放（socket+httpx 双拦截、逐字段一致、损坏报 integrity、缺片报 unreadable、被拒重放零写入）、probe one-shot（无 pipeline_run/资料版本/checkpoint 文件、无 resume API、STOP 文件演练） |
| 演练证据契约 v1 | `src/investment_agent/materials/drill_evidence.py`：`DrillEvidence` schema（六类演练名与 `source_observations._DRILLS` 相等断言、company_scope 复用 `SOURCE_COMPANIES`、simulation 必须 network_calls=0 且 transport=mock、stop_semantics 仅三类停止演练可携带、未知键/bool 冒充 int/未来时间/不安全路径拒绝、artifact 逐哈希核对、原子落盘拒绝覆盖） |
| 六类演练 CLI | `tools/hk206_drills.py`：进程启动即 socket 审计钩子（connect/connect_ex/getaddrinfo 全拦、无逃逸口）；每演练自建 `<root>/scratch/<source>/…/{drill.sqlite3,data/}` 经 `run_with_engine` 显式传入，不读 env 的 DATABASE_URL/DATA_DIR/.env；证据落 `drills/<source>/<drill>-<UTC时间戳>/evidence.json`，汇总 `drill-summary.json`；退出码全过 0 / 否 2 / evidence-root 已存在 2。六演练均走真实服务链路：shutdown（先关=零请求、运行中关=恰好 1 请求后停止且响应保留）、inflight_stop（limiter 等待中翻转全局门）、restart_resume（脚本化 500 中断→重试→无重复版本；HSBC 双来源 run 展示 already_checked 跳过）、old_versions_readable（旧版本串归档可核验+双版本可读）、offline_replay（审计钩子下两次重放结果一致）、probe_stop_withdrawal（STOP 文件与许可撤回两份独立证据） |
| 契约与 CLI 测试 | `tests/unit/test_material_drill_evidence.py`（26 测试：正反例全集）+ `tests/integration/test_hk206_drill_runner.py`（5 测试：CLI 端到端、敌意环境哨兵、篡改字节拒载、**验收器对齐**——六项 citation 齐全时 simulation 报告 manual_review.ready=True 但 decision=pending、real_observations=0；删项/错哈希→not ready 且 pending） |
| D0 清单与手册 | `docs/product/plans/hk-us-expansion/38-hk2-06-d0-readiness-runbook.md`（9 章：SEC 六项/东财六项前置、聚合限流两种可接受证明与全消费者清单表、六发行人 CIK 与不预固定分片数、东财 9 响应与表码禁用、财年窗口仅引已核实 2022–2025、日证据目录契约、账本/review 字段语义表、每日操作序列、失败恢复、红线）+ `docs/product/hk-us-expansion/d0-templates/`（ledger/review/drill-citations 三模板） |

## 集成门禁（主代理独立执行）

测试均在工作树运行（`PYTHONPATH=<工作树>/src`，借主 checkout `.venv` 解释器，虚构
API_KEY；默认排除 network）。基线核对：acb0856 起点定向集在直跑缺 API_KEY 时有 1 个
已知环境失败（`test_material_archive_replay` 的 Settings 校验），补虚构 env 后 3/3 通过，
非代码回归。

| 检查 | 结果 |
| --- | --- |
| 新增专项（A+B 共 3 文件） | 38 passed（7 恢复演练 + 26 契约 + 5 CLI） |
| 定向回归（materials/source-policy/probe/observations/web 契约 17 模块） | 128 passed |
| 全量 unit | 2587 passed / 3 failed / 97 skipped，222 秒；`/tmp/hk206-offline-unit-final.log`。3 个失败均在 `tests/unit/test_bank_proofs.py`，原因为工作树缺少主 checkout 忽略目录 `research/industry-data-validation/evidence/` 下的银行原件 PDF/清单（`FileNotFoundError`，doc 36 记录过的同类工作树数据前置）；本轮交付全部为新增文件、不触及银行代码，判定为基线环境前置而非回归。跳过项多为同类的本地原件依赖，不计为通过 |
| Ruff（8 个新文件） | All checks passed |
| mypy `src` | 444 源文件 0 错（+1 为新增 drill_evidence.py） |
| CLI 演练（主代理亲跑，敌意环境 `DATABASE_URL`/`DATA_DIR` 指向哨兵文件、`ONLINE_REFRESH_ENABLED=true`） | SEC、东财各 exit 0，六演练全 passed；哨兵字节不变、sentinel 目录无新文件——外部 env 与启用意愿不能突破隔离 |
| 证据独立复核 | 两源各 7 份 evidence.json 全部通过 `load_drill_evidence`（含哈希核对）；probe_stop_withdrawal 两机制（STOP 文件/许可撤回）各一份；scratch 库均在 evidence-root 内 |
| 不变性 | 工作树 `git diff HEAD` 为空（全部交付为新增文件）；`handlers.py`/`source_policy.yaml`/迁移/golden 零改动；主 checkout HEAD 仍 acb0856 |
| 模拟边界 | 演练报告 mode=simulation；汇总 network_calls=0；验收器对齐用例证明六项 citation 齐全也只能 pending、real_observations=0，不累计真实观测日期、不自动批准来源 |

主代理审查修正：38 号手册 §8 原将 probe 写为“遵守 Retry-After 最多等 5 秒”，实际
probe 对 429 是立即终止整源（`terminal_http_429_stop_source`，不等待不重试），5 秒上限
属日常 worker 路径——已改为两条路径分开表述。子代理偏差经审查认可：演练入队用
`enqueue_material_job`（`request_refresh` 会钉生产适配器版本）；manifest 篡改负例不重复
既有覆盖；演练限速间隔缩短为 0.01s（停止/恢复语义不受影响，limiter 代码路径仍真实）；
probe_stop_withdrawal 出两份证据目录（计划允许）。

## 子代理发现的既有文档不一致（仅记录，未修改）

1. `observation_plan.yaml` 东财 request_count 写“至少 9 个成功归档响应”，而
   `source_observations.py` 实际要求请求集与 expected 恰好相等且失败数为 0（对 HK 即
   恰好 9 个成功响应）；“至少”措辞比校验宽松。
2. 35 号正文年度对照三值枚举中 `unreviewed` 合法但永远导致 not ready，正文未显式说明。
3. 35 号每日演练证据以 5 项列表呈现，完整六项枚举在同文档验收契约句与 `_DRILLS`；
   集合一致，仅列表呈现不完整。

## 仍需人工提供的来源材料（工程侧无法解除）

- SEC：责任主体与用途范围确认、真实 UA/可监控邮箱、全部消费者的聚合限流证明
  （单实例独占证据或跨进程共享限流器）、六发行人公司范围审批（38 号 §2.1–2.6）。
- 东财：实际端点的端点特定书面授权九项与 Choice 协议适用关系确认（38 号 §3.1–3.2）；
  当前 blocked。
- SEC 首日隔离取证前置条件：工程侧已就绪（演练 CLI、证据契约、D0 检查入口、操作手册）；
  未满足项全部为人工材料——§2.1–2.6 六项勾选齐备并通过 dry-run 预检之前不发请求。

## 下一步顺序

1. 来源范围及请求身份确认（38 号 §2/§3 逐项人工补齐，审批人书面核准公司范围）。
2. 分源三日真实观测（人工选定 D+0/D+1/D+2，逐日一次性 `--allow-network` 隔离 probe，
   按 38 号 §7 序列留证、记账、汇总）。
3. 人工技术验收（来源真实性复核后受控更新 `source_policy.yaml`）。
4. HK2-07 在线场景与 HK2-08 M2 准备（仅当对应来源真实观测通过后启动）。

日常在线刷新继续保持关闭，不操作生产库。
