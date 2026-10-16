# P0/P1 十四组修复的针对性复审

日期：2026-10-16（Asia/Shanghai）。状态：复审、补修及主代理独立验收完成，十四组问题在本次审查范围内闭环；追加的全量集成补验已完成，689 项全部通过。本记录承接[上一轮十四组问题](review-2026-10-16.md)，不将上一轮的测试计数作为本轮新增验证。继续由 GPT‑6 Luna 子代理复审、补修与构造反例，主代理交叉审查并独立验收。范围限 P0/P1，未推进 P2、修改生产库或启用生产开关。

## 复审范围与分工

| 问题组 | 本次重点 | 复审方式 |
| --- | --- | --- |
| R01、R09 | 其他账户约定不能误用；旧用途绑定旧快照应保留有限 unknown | Luna backend 审查 DTO 与计算；新增合法旧用途独立回归 |
| R02 | 500 持仓的诊断容量在 fit 与 entry 一致，不把批次 100 标的错误累加到单条 entry | Luna backend 检查上界与容量用例；主代理核对计算循环 |
| R03、R04 | 模板共享命中不吞轮次；冷却结束的包含边界与零天语义 | Luna jobs 审查排序/生命周期及既有独立预期 |
| R05、R06 | live 前序隔离回放；旧 policy 缺字段兼容、未知 policy 不执行 | Luna jobs 检查查询、冻结、输入摘要与回放 |
| R07、R08 | 精确制品在当前来源失效后可读；未来估值 cutoff 前后的缺失与恢复 | Luna backend 复跑真实 SQLite/HTTP 矩阵 |
| R10 | 精确引用、行快照摘要、条目状态及两张表的 SQL JSON 约束 | Luna backend 检查契约、存储及 050 迁移 |
| R11、R13 | 上海业务日与 UTC 截止；unknown TTL 与 not_applicable 区分 | Luna jobs 检查真实 SQLite 日期反例与 60 秒 TTL 反例 |
| R12 | 账户及 operation 隔离、ABA 切换、迟到响应、存储失败及恢复 | Luna browser 运行真实页面、HTTP、worker，主代理独立复跑 |
| R14 | 内容摘要不能只自证一致；命令、收据、来源及派生动作之间必须一致 | Luna jobs 补窄校验与真实 worker/历史回放反例，主代理交叉核对 |
| 跨组 | 排序逐项检查点和最终原子发布，在失租约/恢复时不重复或发布半份结果 | Luna backend 新增独立 SQLite 排序恢复测试，主代理验收 |

## 本轮发现与修订

### R14：摘要自校验与授权绑定之间仍有缺口

- 原 `authorized_run()` 只比较收据列与 run 内保存的 request_hash，没有从 client_request 和命令 kind 重算；改客户端命令但保留原摘要仍可能通过授权。现在重算并同时核对原收据及 run。
- fit 的冻结内容即使自行重算两个摘要，也必须匹配接收时的顶层 source_manifest_hash；历史回放接收即拒绝不符原件。ranking 的顶层摘要对应候选 SourceManifest，另按其真实类型核对，不误比账户摘要。
- 原排序 worker 只验证动作分区的集合并集，字典会吞重复 listing，合法形状的动作或费用变更也可穿过。现在使用同一纯函数从原请求、冻结持仓和候选清单生成预期动作及 unknown 分区；执行和回放都精确对照已存清单，不悄悄修复损坏原件。
- 原批次摘要校验为了允许 historical_replay 规范了 mode，却未另行核对请求 mode。现在要求实际冻结 mode 与该 run 的请求一致，防止 live 被改成 replay 后穿过摘要检查。
- 最后检查发现 ranking 历史清单自身摘要非法或政策版本不支持时会抛未处理的验证异常。现在只在历史本地解析/验证边界将指定输入异常映射为 `historical_manifest_unavailable`（422）；不捕获数据库/IO 故障。新增真实 SQLite 回放拒绝与 legacy v1 正常回放验证。

修订集中在 `suitability_jobs.py`、`ranking_jobs.py`；不改变客户端请求、收据字段、排序 v1 次序或旧制品。新测试 `test_account_fit_jobs_followup.py` 直接使用临时 SQLite、真实 admission 和 ResearchWorker；对 worker 拒绝断言 assessment、entry、calculation 均为零，对历史回放拒绝断言未接收额外 run。原正常幂等恢复、fit 回放及缺 policy 字段的 ranking v1 回放继续验证。

### R12：恢复分支与迟到异步响应仍有交错窗口

复审确认 run-status GET 的暂时失败会停止当前页面跟踪；另有更重要的 ABA 窗口：旧 fit 终态读取失败，只检查账户，可能在 A→B→A 恢复完成并提交新操作后覆盖新操作界面。终态读取期间的表单编辑也可能丢失 `form_dirty`，将旧情景当作当前输入。

修订要求响应成功、失败、保存与渲染分别核对账户、operation 和 pending 阶段；终态读取以最新 durable context 判定旧情景。表单编辑保留 terminal 阶段和结果 ref，迟到 202 合并同 operation 的最新 context。主代理交叉复核发现首次改法将 durable 对象与提交闭包分离，反而回归了迟到 202 的旧情景标记；原浏览器断言实际失败后补修，须以最终脚本复跑为验收依据。

暂时网络/5xx 状态读取失败使用有上限的退避恢复同 run；401/403/404 等永久错误保留 pending 并停止自动重试，提示检查登录及刷新，避免反复发送被拒请求。已替换 operation 的恢复回调在身份不符时清理。测试 helper 另修正选择账户后等待 context 刷新及提交按钮解禁的竞态，未以绕过 UI 或伪造成功响应代替。

另确认两模块的账户 context GET 与表单输入共用 epoch：读取期间编辑费用会令合法 context 返回被丢弃，提交永久禁用。现在 context 使用独立请求序号与当前账户校验；表单编辑不取消 context，不放宽 A/B 迟到响应保护。两个模块均新增延迟真实 context 响应、编辑费用、释放响应后仍能正常提交的反例，在两个视口执行。

### 排序恢复验证缺口

新增 `test_candidate_ranking_recovery_followup.py` 三项：

1. fit/calculation 已提交后注入暂时 I/O 失败，由 ResearchWorker 正常重排同 run；attempt 2 复用同 ID、同 payload，最终只发布一份完整排序。
2. 真实 flush 排序头及 entries 后，在同一事务内注入租约身份变化，最终 fence 拒绝、整个排序事务回滚；原已提交 fit 保留。恢复后旧 attempt 仍不能发布。此项是事务内失败注入，不冒称 SQLite 写锁期间另一 writer 抢租。
3. 在最终写事务前，以独立 session 通过真实 `recover_stale` 与 `claim_next` 将租约交给 attempt 2 并提交；旧 handler 被 fence，零排序，fit 保留。再经正常恢复、attempt 3 完成，不重复制品。

这些是有执行反例的验证补强；未因此改变既有租约协议或引入新任务表。

## 主代理独立验收

全部数据库验证使用各自临时 SQLite 与 `DATA_DIR`，凭据使用惰性测试值；不调用真实模型或行情来源。测试成功响应及 worker 运行真实实现，故障注入只用于被测试的失败边界。

| 主代理命令/范围 | 本轮实际结果 |
| --- | --- |
| 全部 P0/P1 后端专项：`tests/unit/test_account_fit*.py`、`test_candidate_ranking*.py`、store；对应 fit/HTTP/050 与新增 ranking recovery 集成 | 119 passed，30.82 秒 |
| 收紧零制品断言后，重跑 `test_account_fit_jobs_followup.py` | 10 passed，9.99 秒；检查 calculation 与 entry 无孤儿 |
| `test_web_account_fit.py`、`test_web_client_contract.py` | 49 passed，8.20 秒 |
| 原组合 calculations/purposes/plans/valuation_binding，候选 lifecycle/promote/API/Web | 137 passed，37.33 秒 |
| 全量 unit：`.venv/bin/pytest -q tests/unit` | 2364 passed，2 skipped，6 deselected，294.99 秒；最终历史错误映射修订及其新增两项在收集后完成，另由最终专项复测 |
| 最终后端全部专项与 Web 契约合跑（上一后端命令加两份 Web 测试） | 170 passed，37.22 秒；包含最后历史错误映射、全部新增后端反例及现存 P0/P1 回归 |
| 最终真实 browser：`AF11_FIXTURE_PORT=19866 tests/browser/run.sh account_fit.mjs` | 主代理独立完整通过；1440/390 基线、两模块延迟 context 编辑、页面宽度均通过。复杂 ABA/终态读取/错误重试交错在最终 390 分支执行；抽查最终手机 fit、桌面 ranking 截图 |
| 最终 Ruff / mypy / diff / JS 语法 | 全量 Ruff 通过，mypy 401 源码文件通过；两个模块与 browser 脚本 `node --check` 通过，`git diff --check` 通过 |
| 迁移 head 与默认开关 | 050 唯一 head；Settings 与 RuntimeSettings 两开关均 false |

119 项包含本轮新后端用例，最终 170 项再纳入最后两项及 49 项 Web 契约；各次专项与完整 unit 有重复，不累加成独立测试总数。截至上述针对性复审结束，尚未重跑全部 integration 及根目录所有测试，不借用上一轮 3751 项作为本轮结论。后续用户追加授权的全量集成补验单列于下节。unit 两项跳过为未随仓库提供的原始资料，六项排除为 network 测试。

## 全量集成补验（同日追加）

用户追加要求补跑全量集成、发现问题直接修复并补验证、更新最终验收，继续 GPT‑6 Luna 子代理执行，主代理验收。本节记录补验，保留上节各次测试的实际时间边界。

主代理独立收集 `tests/integration`，共 122 个模块、689 个不重复用例；该目录未发现 network 标记。检查发现本机已有独立网关构建，使用 `ZCLAUDIA_GATEWAY_DIR=/Users/zhvala/SourceCode/zclaudia-gateway` 即可运行此前缺环境跳过的四项契约测试。主代理先用临时 SQLite、网关临时数据目录及随机本地端口独立预验，结果 **4 passed，3.18 秒**。未修改网关仓库，也未连接部署网关。

子代理首次未配置网关的预跑由主代理要求主动中止，日志保留在 `/tmp/zcharlie-integration-full.CJoIqY/integration.log`，不计作最终结果。随后另建临时库与数据目录，从头运行完整集合，明确设置 `NTFY_ENABLED=false`。本轮实际 Git HEAD 为 `49e62d09160f00c3aa48dc22b0f7e0ceb378cd7e`，同时保留既有未提交 P0/P1 工作区，未自动提交或回滚。

最终全量命令（Luna 执行，主代理核验原始日志、JUnit 与独立收集清单）：

```sh
integration_tmp=$(mktemp -d /tmp/zcharlie-integration-full.XXXXXX)
mkdir -p "$integration_tmp/data"
API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 \
DATABASE_URL="sqlite+aiosqlite:///$integration_tmp/integration.db" \
DATA_DIR="$integration_tmp/data" \
ZCLAUDIA_GATEWAY_DIR=/Users/zhvala/SourceCode/zclaudia-gateway NTFY_ENABLED=false \
.venv/bin/pytest -q tests/integration --junitxml="$integration_tmp/integration.xml" \
  > "$integration_tmp/integration.log" 2>&1
```

本次最终目录为 `/tmp/zcharlie-integration-full.4ooCzJ`，包含 `integration.log`、`integration.xml` 和主代理独立核验结果 `root-verification.json`。

| 执行及验收 | 实际结果 |
| --- | --- |
| Luna 完整 `tests/integration`，含独立网关 | **689 passed，0 failed，0 errors，0 skipped，0 deselected；600.86 秒** |
| 主代理独立比对收集清单与 JUnit | 122 个模块、689 个唯一用例逐项一致，无漏项、重复、失败或跳过；包括 4 项网关及 3 项排序恢复用例 |
| 主代理在共享工作区并行变更后补跑 `tests/integration/test_web*.py` | **140 passed，30.44 秒，命令退出码 0**；JUnit 同样无失败、错误或跳过 |
| 主代理独立验收脚本 | 退出码 0；核对日志完整完成、JUnit/清单一致及下面的源文件变更边界 |
| 最终 JS 语法、差异及开关核对 | `agent.js` 的 `node --check`、`git diff --check` 通过；Settings/RuntimeSettings 两个开关默认仍为 false |

全量 pytest 的输出与 JUnit 均记录 689 项成功。其外层临时 zsh 收集脚本随后使用了只读变量名 `status`，导致外层命令非零，未能另存 pytest 自身退出码；这里不将未捕获的码冒充已核实的 0，也不将收集错误当成测试失败。主代理直接解析原始结果、逐项比对独立清单，确认完整执行成功；补验收集命令改用非保留变量 `task_test_exit`，140 项 Web 复跑及独立结果核验均以退出码 0 结束。没有因这一日志收集问题改动或放宽测试。

主代理另外比较了执行前后 1100 份源码、测试及迁移输入的内容摘要：1099 份保持不变，唯一变化为共享工作区另一路修改的 `web/static/agent.js`（429 轮询退避），不属于本次账户适配任务。保留该变更及同时出现的 mobile/移动端设计文档变更，未覆盖、回滚或据此扩展实现范围；相关 Web 集成由主代理按最终文件再跑 140 项通过，补验开始至结束的源码/测试摘要稳定。此次全量集成补验没有修改业务源码、测试断言或迁移，也没有发现待修复的业务缺陷。

本次 Web 补验目录为 `/tmp/zcharlie-integration-web-final.93TCwK`，含 `web.xml`。四项网关预验、140 项 Web 补验均与完整集合重复，不累加为独立通过总数。本节补齐先前未重跑全量 integration 的证据缺口；根目录 `tests/test_*.py` 仍沿前次已记录结果，本次不重新声称全部测试分区在同一时刻重跑。

## 结论及边界

R01—R11、R13 的原修复经本轮逐项复核与回归保持；R12、R14 的遗漏及修复期间新引入的交错回归已补修并按最终代码复测。未留下本次已确认而未修复的 P0/P1 阻塞项。排序最终提交、检查点复用、旧 attempt fence、合法旧用途与缺省 v1 回放都有明确反例或兼容性验证，不能只从通过总数推断并发安全。

本轮没有修改数据表/迁移、候选生命周期政策或排序 v1 次序，没有新增任务表、规则引擎或生产规则提供方。保持原请求/收据形状与历史制品，未运行生产迁移、写生产运行时设置或提交 Git。两个生产开关默认仍为 false；P2 未推进。生产可信规则及数据来源的就绪条件仍沿首次验收，临时 SQLite 与合成浏览器测试不替代这些条件。
