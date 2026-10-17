# HK2-05 来源策略与可恢复采集验收

日期：2026-10-17（Asia/Shanghai）。结论范围为默认关闭的代码、离线与模拟验收，
不代表来源许可已确认或 HK2-06 真实来源验收通过。
实施拆分见 [31](31-hk2-05-execution.md)。

## 基线与隔离

- 基线：已提交 main `7df697aeeb951f9bf1b76de7b2194ca6a62c6e7a`。
- 独立分支：`codex/hkus-source-policy`；工作树：`/Users/zhvala/.codex/worktrees/hkus-source-policy/zcharlie`。
- 三个 GPT-6 Luna 子代理分别完成策略/统一设置、worker/HTTP、API/页面；主代理审查、集成、验收并提交。
- 不复制主 checkout 的 P4 未提交代码，不更改其 index、文件或 HEAD；未合并 main、推送、部署或操作生产数据库。
- 仅借用主 checkout 的 Python 工具链；代码通过本工作树的 `PYTHONPATH` 加载。测试使用 `/tmp/hk205-*` 临时数据库和数据目录。
- 原件依赖的银行回归测试需要 Git 忽略的历史文件；只读复制 70 个已有 PDF/JSON 制品至本工作树的忽略目录并核对 SHA-256，清单在 `data/hk205-offline-acceptance/local-evidence-copy.json`。不复制应用库或 P4 代码。

## 实现与验收映射

| HK2-05 标准 | 当前行为与验证 |
| --- | --- |
| 来源与证券分别判定 | `materials/source_policy.py` 同时核对受控证券绑定、许可公司范围/引用、技术公司范围/适配器版本/至少三个不同日期报告及引用、来源运维开关、全局保险。不存在、格式无效、未来/不足日期和范围不符均拒绝；合成证据仅在测试注入 |
| 统一运行设置 | `materials_sec_refresh_enabled` / `materials_hk_refresh_enabled` 接入现有 Settings/registry 默认、校验、历史、重置与执行时读取，均默认 false；普通设置不能写许可或技术审批。`config/research_material/source_policy.yaml` 为版本化受控审批记录，默认 unknown/not_accepted；修改内容也改变策略指纹 |
| 允许来源互不阻断 | 模拟 SEC 获准、HK 未获准时独立判定；某来源瞬态失败先归档/检查点，再继续其它允许来源。真实 worker 两次预算耗尽案例中，失败源只请求两次、成功源只请求一次且只保存 revision 1 |
| 关闭不被排队快照绕过 | 每次请求前重读 DB 有效设置及受控策略；Httpx 限速等待结束后再次检查。临时文件库中真实关闭运行设置，证明等待后零发送；第一响应后关闭，后续阶段不发送，先前响应仍归档；已有 before_send guard 被保留、链式执行并在 finally 恢复 |
| 运行结果精确 | 全部成功为 success；结构/权限/未启用等非重试结果为 partial，并记录逐源 source_status、source_decisions、checked、saved 和归档。瞬态错误由既有 worker 有限重试，耗尽后按原任务 06 契约为 failed，保留其它来源 saved 和已有数据；failed 不表示抹掉其它来源已保存结果 |
| HTTP 有界 | MockTransport 验证实际请求头与 timeout（注入 client 也生效）、来源 limiter、429 Retry-After 秒值/HTTP 日期及最多 5 秒等待；408/429/5xx 进入既有有限任务重试；401/403、非法 JSON、结构错误不无限重试。未实际访问 SEC/东财，实际 UA/条款仍须 HK2-06 核对 |
| 幂等与恢复 | 两个独立 AsyncSession 同请求并发只创建一条；相同策略/日期重复去重，策略内容/开关/版本改变后同日产生新审计批次；检查点重试跳过已完成来源，相同事实不产生新版本。缺少或不匹配 adapter_versions 的旧任务明确拒绝且不发请求 |
| API 与身份 | GET 保持只读、不取数、不入队；GET/POST 核对数据库发行人与受控名单，错配不采集。POST 保持 HK2-07 审计队列兼容，逐源返回 enabled、accepted_for_fetch 和 reason_codes；登记/去重不授予权限。显式 HK 期末日期请求（含混合来源）入队前返回 422；显式 SEC 合法期间保留原请求 |
| 页面真实表达 | 来源许可、技术验收、身份绑定、运维开关及拒绝原因分开展示；原 freshness 别名仍兼容，页面不重复显示别名。在线能力按当前有效来源共同判定，登记反馈不宣称已完成采集或资料更新 |

主代理审查接受 `materials -> settings` 一条新依赖边，并在 import graph 基线中仅增加此项：
材料采集需要统一设置的执行时读取，以便关闭即时生效，不建立第二套运行配置。其余分层门禁保留。

## 临时应用与浏览器

使用正常应用入口、正常登录、正常股票页与真实 API，临时库从空库迁移至当前 `052`；
本轮没有新增数据库迁移。通过既有 annual package 测试工厂生成明确的 **synthetic** 八家公司资料，
经生产 `import_reviewed_annual` 路径导入八份数据集，再启动外部 socket/DNS 禁止的离线服务。
这些合成数值、期间、原件和核对结果仅用于本轮来源策略页面回归，不能替代 HK2-04 的真实年度证据。

- Chromium 桌面 1440×1000、移动 390×844，八家公司共 16 张完整页面截图。
- 16 个视口均无横向溢出；来源 enabled 均为 false，许可未确认、技术未核验、运行开关关闭。
- 外部浏览器请求、JS 异常、console error、网络失败与 HTTP 错误均为 0。
- 新设置分组另通过桌面/移动正常浏览器只读检查，各生成一张截图；两个开关均显示默认关闭，无横向溢出、JS 异常或外部请求，没有操作开关或保存设置。
- 对八家公司真实 POST 各两次，第二次均去重；所有 requested source 的 accepted_for_fetch=false。
- 正常 MaterialCollectionHandler/ResearchWorker 执行这八条任务后均为 partial，source_policy_disabled 可见；新增 acquisition=0、原数据集仍为 8、last_success_at 仍为 null。
- API 与 worker 两个进程的外部请求禁止日志均不存在，即没有被记录的上游尝试；全局保险始终 false。

本地忽略制品目录：`data/hk205-offline-acceptance/`，包含 `migration.log`、
`seed-report.json`、`browser/report.json`、16 张资料页截图、`settings-browser-report.json`、
两张设置页截图、`closed-worker-report.json` 和进程日志。
截图及 scratch DB 不是生产数据，不纳入 Git 提交。

## 代码门禁

| 检查 | 结果 | 本机日志 |
| --- | --- | --- |
| 全量 unit，完整离线制品环境 | 2496 passed / 3 skipped / 6 deselected | `/tmp/hk205-unit-complete.log` |
| 全量 integration 首轮 | 778 passed / 14 skipped / 10 failed；全部失败项由下列完整模块复验覆盖并通过 | `/tmp/hk205-integration.log` |
| HK2-05 专项，13 个新旧模块 | 126 passed | `/tmp/hk205-acceptance-final.log` |
| integration 收口：api_settings、web_settings、web_reviewed_financials、material_collection_job、web_stock_financials | 62 passed，覆盖首轮全部 10 个失败项 | `/tmp/hk205-closeout-final.log` |
| 其它测试目录（排除 unit/integration） | 717 passed | `/tmp/hk205-other.log` |
| Ruff `src tests` | All checks passed | `/tmp/hk205-ruff-complete.log` |
| mypy `src` | 431 source files，无错误 | `/tmp/hk205-mypy-complete.log` |
| Node 语法检查 | financials.js / settings.js 均通过 | 命令退出码 0 |
| `git diff --check` | 通过 | 命令退出码 0 |

表中专项、收口与全量覆盖有重叠，不把数字相加。全量 integration 首轮的失败来自旧来源状态/文案断言、注册表顺序和设置页分组缺口；已更新精确来源状态断言、保持注册表原有顺序，并补齐设置页显式材料来源分组；上述五个模块完整复验无失败。单元初轮缺少 Git 忽略的银行原件及新 import graph 依赖边，补齐离线输入、审查依赖后在稳定环境完整重跑，结果以最终 unit 行为准。HK2-04 真实原件缺失等 skip 和 network deselection 不记为通过。

## 当前边界与下一步

HK2-05 只完成默认关闭的实现及离线/模拟验证。受控 YAML 仍为许可 unknown、技术 not_accepted，
两个来源运行设置默认 false，`ONLINE_REFRESH_ENABLED=False`；行情、FX、年度 PE、PB、分红能力不在本轮开启。

下一步先在来源层记录具体展示/缓存/使用范围及请求要求，再分来源安排 HK2-06 的真实观测：
至少三个不同日期，覆盖该来源受控证券、连续结构稳定、字段与 HK2-04 原件一致，并真实演练关闭、
在途停止、重启恢复与归档重放。SEC 和港股可以独立过关；本轮模拟成功不能计入三日真实观测。
通过后才能推进 HK2-07 在线浏览器验收及 HK2-08 M2。生产 M1 变更另行执行，不由本次 commit 触发。

本分支未含主 checkout 正在进行的 P4；未来合并前需要整合重叠的 Settings/registry、页面和 worker 相关路径，
并对合并后的最新 main 重新运行联合门禁，不能直接套用本轮测试数字。
