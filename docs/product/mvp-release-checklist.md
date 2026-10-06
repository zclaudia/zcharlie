# MVP 发布清单（签字式验收）

状态日期：2026-10-06。对照 [`engineering-baseline.md`](engineering-baseline.md)
的 MVP Definition of Done 与 [`mvp-completion-plan.md`](mvp-completion-plan.md)
第 8 节逐项验收。每项标注通过证据（测试 / 提交 / 人工操作）。

## 1. 研究与数据

| 项 | 状态 | 证据 |
|---|---|---|
| 至少 12 只不同特征 A 股的 golden 与 provider fixture 离线通过 | ✅ | 13 只样本（600406 对 legacy oracle，12 只冻结 golden）；`tests/unit/test_golden_corpus_replay.py`；manifest `tests/fixtures/research_kernel/manifest.yaml` |
| 每个覆盖维度（交易所/业务/边界/规模）由 manifest 自动断言 | ✅ | `tests/unit/test_golden_corpus_manifest.py`；北交所样本因上游网关阻塞以沪深小盘替代并记录（恢复条件写入 manifest） |
| iFinD 响应 → canonical 契约回归（正常/空行/未知格式/缺字段/异常状态） | ✅ | `tests/unit/test_ifind_contract.py`（12 份 raw fixture 逐字节重放 + 合成边界） |
| Agent eval：引用有效率 100%、needs_review 规则、无交易指令 | ✅ | `tests/unit/test_agent_eval.py`、`tests/integration/test_evidence_navigation.py` |
| partial（评分成功、解释失败）保留确定性结果 | ✅ | `tests/unit/test_ifind_chain.py::test_explanation_failure_persists_partial_deterministic_result`、`tests/integration/test_api_research.py::test_get_partial_research_run_returns_deterministic_result` |
| golden 可离线重放；默认测试无网络、无真实 secrets | ✅ | CI `pytest -q -m "not network"` + inert 凭证；空环境全量通过 |

## 2. 运行系统与自选股

| 项 | 状态 | 证据 |
|---|---|---|
| 单股研究 API（幂等、202 + run_id）与独立 worker | ✅ | 既有 MVP-201～206 测试（`test_pipeline_run_lifecycle.py`、`test_research_worker.py`） |
| 失败恢复：重启可识别孤儿任务并安全重试/终止 | ✅ | worker 恢复测试；空环境 smoke 重启步骤 |
| 批量刷新逐标的隔离，单只失败不阻断 | ✅ | `tests/integration/test_api_watchlist.py`；`enqueue_watchlist_refresh` 逐项 outcome |
| meaningful change 一次通知；baseline/无变化/噪声静默 | ✅ | `tests/unit/test_change_notification.py`、`tests/unit/test_research_change_tracking.py` |

## 3. Web UI 与证据

| 项 | 状态 | 证据 |
|---|---|---|
| 浏览器登录（签名 HttpOnly SameSite=Strict 会话，Cookie 不含密钥） | ✅ | `tests/integration/test_web_app.py`、`tests/unit/test_web_session.py` |
| 添加/管理自选股并批量刷新（逐项状态可见） | ✅ | 浏览器实测 + `test_dashboard_read_models.py::test_watchlist_enable_toggle_roundtrip` |
| 查看分数、置信度、freshness、证据、缺失项、历史与 diff | ✅ | 单股页浏览器截图验收（含窄屏）；`/research/stocks/{ticker}/history`、`/dashboard/today` 读模型测试 |
| queued/running/success/partial/failed 均有可理解 UI | ✅ | 运行中心页 + 状态筛选测试；partial 页面显示确定性结果并注明解释不可用 |
| Agent 引用可定位到确定性字段与来源 | ✅ | 证据导航（inspector 显示 provider/endpoint/时间/quality/note）；`test_evidence_navigation.py`；浏览器实测 |
| 所有页面展示"研究辅助，非投资建议" | ✅ | 模板页脚 + `test_every_page_carries_states_disclaimer_and_noscript`；视觉验收确认 |

## 4. 发布工程

| 项 | 状态 | 证据 |
|---|---|---|
| ruff、strict mypy、全量 pytest 与 Agent eval 在 CI 通过 | ✅ | `.github/workflows/ci.yml`（Python 3.13，`pytest -q -m "not network"`，无 secrets；lint/类型/golden 故意破坏会失败） |
| 空数据库可按文档迁移、启动并完成一次 stub 研究 | ✅ | `tests/integration/test_clean_environment_smoke.py`（迁移 → API → 自选 → 刷新 → worker → partial 结果/diff/制品/Web → 重启可查询） |
| 备份恢复演练通过，数据库与制品由 run_id 对齐 | ✅ | `tests/integration/test_backup_restore.py`；runbook `docs/product/backup-and-recovery.md` |
| API、日志、页面和归档不泄漏任何秘钥 | ✅ | `tests/integration/test_security_checks.py` + 采集器双重凭证扫描；fixture 目录 grep 无密钥 |
| artifact 与 notification 路径穿越全部拒绝 | ✅ | `test_security_checks.py`（URL 编码穿越、manifest 逃逸、通知越界均 404） |
| 默认只绑定本地/受控地址；远程暴露显式配置 | ✅ | `ia api serve` 默认 `127.0.0.1`（测试钉住）；docker-compose 显式传 `--host 0.0.0.0` |
| Cookie、Origin、文件下载和错误响应满足约束 | ✅ | 跨源 POST 403、Cookie 篡改/过期回登录、错误只含可理解摘要 |
| 依赖扫描只阻断已知高风险且可利用问题；结果记录在案 | ✅ | pip-audit 2026-10-06 运行清零（starlette/urllib3/soupsieve/pyjwt/pydantic-settings 升级至已修补版本） |

## 5. 边界确认

- [x] 不存在下单、券商连接或自动交易路径（仓库内无交易执行代码路径）。
- [x] 不新增 LLM 角色、供应商、组合管理能力。
- [x] 非 A 股 Discovery/Attribution 保留为 legacy，不扩展、不删除。（2026-10-06 后续：已移出主干，代码保留在 tag `legacy-nasdaq-final`。）
- [x] 单用户、SQLite、iFinD 单数据源、2026 交易日历已列入 README 已知限制。

## 6. 遗留事项（不阻塞 MVP 发布）

1. 北交所样本：待 ifind-proxy/agent_gw 恢复北交所行情后补回不少于 2 只
   （manifest `blocked_exchanges.BSE` 已记录）。
2. CI 首次真实 runner 运行需 push 至 GitHub 后确认（工作流已按本地空环境验证）。
3. 迁移前的历史快照不反向生成 change set（新运行起自动比较最近快照，计划内行为）。

签字：本清单各项均有对应自动化测试或可复现操作；2026-10-06 由编码 Agent
依 `mvp-completion-plan.md` 执行完毕。
