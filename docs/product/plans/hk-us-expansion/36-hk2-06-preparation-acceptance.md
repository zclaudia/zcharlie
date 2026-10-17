# HK2-06 准备阶段集成验收

日期：2026-10-17（Asia/Shanghai）。基线 `02daa6d`，独立分支 `codex/hkus-source-policy`，
工作树 `/Users/zhvala/.codex/worktrees/hkus-source-policy/zcharlie`。
子任务及依赖见 [33](33-hk2-06-preparation-execution.md)。三个 GPT-6 Luna 子代理分别实现
来源审查、隔离 probe、离线验收与计划，主代理复核官方依据、审查实现并执行集成门禁。

## 结论与范围

**HK2-06 准备阶段交付；真实三日观测尚未开始，HK2-06 整体未通过。**
SEC 与东财均为 **0/3** 个合格真实观测日。没有执行真实财报接口请求或创建定时任务，
没有写生产库、迁移、部署、合并 main，也没有改动主 checkout 的 P4 未提交文件。

`source_policy.yaml` 两源仍为 `licensing: unknown` / `technical_acceptance: not_accepted`；
`ONLINE_REFRESH_ENABLED=False`，两个来源 ops 默认 false。计划 YAML 不进入运行时审批。
本轮没有更改普通 fetcher、worker、设置、API 或页面行为。

## 交付与独立核验

| 项目 | 实现与验收落点 |
| --- | --- |
| 来源范围与请求要求 | [34](34-hk2-06-source-review.md) 仅使用官方依据，区分 SEC 一般再使用说明与项目审批、Choice 产品协议与实际证券端点的未知适用关系；主代理独立核验 SEC FAQ / Developer Resources 和东财官方协议，未将公开可访问当成端点授权 |
| 默认零请求预检 | `tools/hk206_source_probe.py`，默认 dry-run 只列计划范围和缺口；未来显式网络执行仍受固定策略的许可与全源公司范围门禁约束，没有 CLI 审批绕过选项 |
| 隔离与停止 | 显式网络/模拟路径仅用命令内 SQLite 内存库获取标准 archive 的本地 subject ID；不读取 .env、配置数据库或应用全局 engine。全源共享 fetcher / limiter；每次请求及等待后复核许可和独立 STOP，401/403/429 停整源，瞬态故障有界重试 |
| 多响应归档与重解析 | 标准 manifest/body SHA，逐请求与响应一一绑定；港股必须显式四个完整连续年度，SEC 自动发现 Submissions 历史分片。主代理用真实 fetcher、adapter、archive、grader 做港股三公司九响应 MockTransport 联测；SEC 六公司和 JPM 两个历史分片也完成独立联测 |
| 离线验收 | `tools/hk206_source_acceptance.py` 按当前 adapter 版本重解析，核验公司范围、原件、哈希、请求记录及上海日期；失败清零，同日不加天，simulation/dry_run 不计日。年度对照、来源范围和六项独立演练须有人工声明及本地 SHA 引用 |
| 三日计划 | [35](35-hk2-06-observation-plan.md) 与 `config/research_material/observation_plan.yaml` 分源给出 D0 前置、D+0/D+1/D+2 范围、失败重置和验收；无已完成日期、无自动排期 |

主代理集成审查促成的修正：禁止注入审批但缺 fetcher 时回落真实 HTTP；全源复用 limiter；
HK 显式年度跨度；报告子目录原件引用；畸形输入、布尔 schema 和非有限耗时拒绝；
当前适配器版本不再硬编码；已核验公司前缀下的已知 unmapped 提示保留为信息项，不能掩盖结构错误。

## 门禁与演练

所有 pytest 均默认排除 network；测试使用本工作树代码与主 checkout
既有虚拟环境，只提供虚构测试 API_KEY 和独立临时 DATA_DIR / DATABASE_URL，不使用生产配置。

| 检查 | 结果 / 证据 |
| --- | --- |
| HK2-06 新增专项 | 50 passed，覆盖最终代码、两个真实适配器模拟联测及输入/隔离负例；`/tmp/hk206-final-focused.log` |
| 全量 unit 离线回归 | 2532 passed / 3 skipped / 6 deselected，280.64 秒；收集后补充的四个畸形输入负例及最终报告语义修正另由上述最终专项覆盖。`/tmp/hk206-unit-complete.log` |
| 来源策略 / API / worker / 页面与归档、离线导入集成回归 | 首轮 54 passed / 1 failed：既有 replay 用例因测试进程未提供必填 API_KEY 失败。补虚构测试环境后完整 replay 模块 3 passed；不修改实现或断言。`/tmp/hk206-regression.log`、`/tmp/hk206-replay-retest.log` |
| Ruff | `src tests` 及两个新 CLI 全部通过；`/tmp/hk206-ruff-final.log` |
| mypy | `src` 全部通过，433 个源文件；`/tmp/hk206-mypy.log` |
| CLI 零网络预检与空账本 | SEC 计划六家公司、东财计划三家公司；两源实际 company_keys 为空、请求 0；空账本均 pending / real_observations=0；网络连接和 .env 读取有 audit guard 拦截，即使环境传入启用意愿也不触发请求 |
| 不变性 | CLI 演练前后 source_policy、handlers、config 的 SHA 一致；独立数据库 sentinel 字节不变；未打开既有数据库。最终演练 `data/hk206-preparation/root-acceptance-final-20261017/proof.json`（本地忽略目录） |

最初全量 unit 启动也缺少测试 API_KEY，发现环境问题后仅中断本任务的精确 PID，
在独立测试环境重新完整执行，未终止其它任务进程。最初不完整日志不作为最终门禁。
三个跳过项依赖本工作树未提供的完整银行、Costco 或 HSBC 原始调研捕获；不把跳过当作真实来源验收。

## 工具用法与证据边界

在仓库根目录，以下命令默认只预检；工作目录必须全新：

```sh
PYTHONPATH=src python tools/hk206_source_probe.py \
  --source sec.companyfacts.v1 --work-dir /absolute/new-evidence/sec-preflight
PYTHONPATH=src python tools/hk206_source_acceptance.py \
  --ledger /absolute/evidence/sec-ledger.json --evidence-root /absolute/evidence
```

空 ledger 示例和人工 review 字段见 [35](35-hk2-06-observation-plan.md)。报告内归档引用相对该报告目录，
review 引用相对共同 evidence-root；汇总输出若落盘，须为证据目录外的新文件。HK 预检可省略年度并报告缺口，
未来真实执行前必须由人工核对年度后显式提供四个 `--period START/END`。本轮不执行真实 `--allow-network`。

测试中的许可、公司响应、日期和人工复核均为合成证据；HSBC 的 SEC 联测使用重标 CIK 的本地样本，
不代表汇丰实际申报内容。模拟报告明确标 `simulation`，即使解析/归档通过也只能 pending。
UA 校验只能检查格式，不能证明邮箱可收信；责任人须核实真实身份、监控邮箱和全部消费者的总速率。
`limiter_wait_seconds=null` 表示没有取得实测等待值，不能当成零等待或跨进程限流证明。

本地哈希证明一致性，不能独立证明源站真实性或真实获取日期；工具最多输出
`technical_review_candidate`，不写审批或启用开关。一次性 probe 不提供断点续跑或应用资料版本登记，
真实停止/撤回、独立恢复、旧版本读取、年度修订和离线重放仍须按计划另行演练并人工复核。

## 下一步与依赖

1. **P0 / SEC**：责任人确认目标用途与官方再使用说明的适用范围，补真实项目/组织联系 UA、
   可监控邮箱、所有进程/机器的聚合限流证据，再通过受控流程批准对应公司范围；尚不执行真实观测。
2. **P0 / 东财**：先取得实际证券端点的自动化、速率、缓存/展示等书面范围依据及 Choice 协议适用关系；
   当前 blocked，不能用内部限速、旧响应或端点连通代替。
3. **P1 / 按源执行**：各自满足前置后，由人员选定三日窗口，日常刷新仍关闭，逐日运行获准的一次性隔离取证，
   完成年度对照、来源真实性及六项演练证据；任何失败重选 D+0，最后由工具给候选并人工审批。
4. **后续阶段**：只有对应来源真实验收通过，才评估 HK2-07 在线生命周期和 HK2-08 M2。
   当前可以准备证据，不能将 HK2-06 准备交付当作启用条件已经满足。
