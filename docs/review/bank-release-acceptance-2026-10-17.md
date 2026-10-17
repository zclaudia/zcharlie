# 银行发布准备与部署前回归验收

日期：2026-10-17；执行计划见[银行发布收尾计划](../product/plans/industry-expansion/bank-release-plan.md)，操作步骤见[银行发布手册](../product/bank-release-runbook.md)。三个 GPT-6 Luna 子代理分别负责证据预检、迁移与发布演练、操作手册；主代理独立复跑、审查并验收。本轮不执行真实部署、不推送、不访问生产数据库、不启动实际 worker/scheduler、不调用真实业务数据源或模型。

## 候选与隔离

基线为 `acb0856a6c32d319a5411404054e827a6276f05f`，在独立工作树 `/Users/zhvala/.codex/worktrees/bank-release-readiness/zcharlie` 验证。主工作区的并行 P4 未带入该候选；候选唯一迁移 head 为 `052`，不包含未提交的 `053/054`。本轮另有两个银行 CLI 源文件增量及工具、测试、文档，尚未提交。部署必须使用之后明确批准的完整提交 SHA，不能把基线 SHA 当作已经包含本轮修复的发布版本。

基线回归固定使用 Git 跟踪的测试文件；原件从本机已有归档独立复制到候选的忽略目录。所有数据库和测试 DATA_DIR 均位于临时目录，测试关闭 checkout dotenv。Python 用显式 `PYTHONPATH=<candidate>/src`，避免共享开发虚拟环境的 editable install 把 P4 模块带入。没有使用生产卷或运行共享 Compose stack。

## 实际修复和交付

1. 新增 `tools/bank_release_preflight.py`：只读检查固定100槽与审计清单的状态、数值、单位、页码及来源绑定；FY2024 还核对固定原件的完整受限规则指纹。核查20份完整年报、配套元数据、固定身份的宁波CCXI补充PDF，以及 FY2025 名单和10份供应商输入。报告记录路径、字节数、SHA-256和明确缺口；缺件或篡改失败，报告用独占创建，避免覆盖输入。该工具不替代逐字段数据库/PDF语义验收。
2. 发布演练发现 FY2025 旧导入默认写入 `offline-run02-v1`，而 FY2024 已审阅导入写入 `bank-annual-v1`。名单切换后直接提交 FY2025 银行年度批次会缺数据。新增公开命令 `ia peer-annual import-bank --bank-annual-source --cohort-key bank_run02_sample ...`，要求审计及原件目录、最新名单显式绑定银行年度来源且十家 listing 均为成员；固定适配器调用既有实际原件核验器，不触网。公开 wrapper 同步透传选项，旧默认不变。
3. 新增真实 Alembic `048→052` 演练：保留旧来源 FY2025 批次，再显式导入银行年度来源的 FY2025 与 FY2024，冻结两年批次，按年度执行鉴权只读 HTTP GET，并核对旧历史值、幂等和任务数量。
4. 发布手册修正通用部署脚本会启动全部服务的边界；给出停写后备份、明确挂载镜像外证据、批准提交的归档构建、唯一候选 tag、仅 API 启动以及代码/schema/数据库/制品成套恢复的步骤。生产主机、当前 schema、挂载、开关及中断窗口由部署前实测并再次确认。

## 证据与数据状态

主代理预检报告：`/tmp/zcharlie-bank-release-2026-10-17/root-preflight-final.json`，实际 `ready=true`；20份年报PDF、20份元数据、11份供应商/名单输入、1份必需补充PDF均通过。原件不随Git和运行镜像分发，部署输入需按手册另外准备并校验。

| 年度 | verified | candidate | scope_mismatch |
| --- | --- | --- | --- |
| FY2024 | 47 | 1 | 2 |
| FY2025 | 48 | 0 | 2 |
| 合计 | 95 | 1 | 4 |

常熟FY2024净息差保持候选；平安两年度不良率、拨备覆盖率保持本行口径排除。原因及处理方案沿用[两年度矩阵验收](bank-fy2024-acceptance-2026-10-17.md)，本轮没有为了发布改写证据结论。演练额外保留旧 FY2025 离线来源10份快照，因此数据库有30份年度快照，其中本次银行年度来源20份；不是重复计入100槽。

## 验证记录

主代理运行记录位于 `/tmp/zcharlie-bank-release-2026-10-17/`。以下三组固定基线测试路径互不重叠：

| 门禁 | 主代理实际结果 |
| --- | --- |
| 固定基线 `tests/unit` | 2659 passed、2 skipped、6 network deselected；398.19秒 |
| 固定基线 `tests/integration` | 818 passed、4 skipped；1334.73秒，含真实银行原件整链及历史兼容 |
| 其余固定基线测试 | 717 passed；237.89秒 |
| 基线合计 | **4194 passed、6 skipped、6 network deselected，0 failed** |
| 本轮导入/预检及既有银行兼容专项 | 56 passed；13.51秒，覆盖最后的两CLI源修改 |
| 无忽略原件目录的预检单测 | 15 passed；1.03秒，证明工具单测不依赖本机research归档 |
| 新增迁移及两年度发布演练 | 主代理独立1 passed、202.00秒；子代理另行1 passed、246.12秒；均实际使用原件，无skip |
| 候选类型检查 | 443个生产源文件mypy通过 |
| 本轮静态检查 | 6个Python文件Ruff及Git差异空白检查通过 |

六个skip分别为2项海外忽略原件缺失、4项外部网关构建环境缺失；银行原件用例均实际执行。基线测试收集完成后的CLI增量另由56项专项、新迁移演练及候选制品烟测覆盖；不将这些重叠用例累加成全仓独立测试数，也不把这份隔离候选结论扩展到主工作区P4。

额外使用正式 `backup_research.py → verify_backup.py → restore_research.py` 对已经结束的两年度验收临时库做完整备份恢复：manifest内5个文件的尺寸与SHA-256全部匹配（PDF存储在SQLite中），恢复到全新目录。新engine重新读取20份年度快照、21份PDF blob；**95项verified字段逐项原件重验、两年度8项严格引用全部通过**。该来源是模型建表的隔离验收库，不是生产副本；`048→052` 的真实迁移另由新增演练和现有迁移保留数据测试覆盖。证据为 `backup-source.json`、`backup-restore-canonical.log` 与 `restore-readback.json`。

候选wheel从基线归档加两个受审CLI文件构建，SHA-256为 `861ae659229e9b8272d4a3dc066021a25391b1e0e9102189638183049a972f3c`；包内CLI文件哈希与候选一致，Adobe-GB1映射、行业模板及JS均存在，未混入未提交P4模块。提供单独的config目录后，实际wheel公开命令帮助包含新增选项。

Linux/amd64镜像从基线Git归档按现有Dockerfile构建，再只覆盖两份已审阅CLI文件形成演练候选；没有将主工作区或候选的未跟踪目录送入运行镜像。精确身份：

- 基线镜像：`investment-agent:bank-preflight-acb0856`。
- 演练镜像：`investment-agent:bank-release-prep-4a04d3bc6bb9`；image ID为 `sha256:98e3d21c713187a71500610f48eecee4a96505645fe9eea71358f8b34f21b6f3`。
- revision标记：`acb0856-bank-release-4a04d3bc6bb9`；两文件增量manifest SHA-256为 `4a04d3bc6bb9d6cedc5ca232c544fefa92c1b4bf03ac6ee426b777e00d7c45ca`。这是未提交演练候选身份，不是可直接批准部署的Git提交。

主代理在该amd64镜像中以UID10001、`--network none`、只读根文件系统、仅tmpfs数据目录及只读测试输入挂载执行烟测：源码哈希、公开CLI新选项、`pypdf==6.20.0`、`pydantic-ai==2.31.1`、Adobe映射/行业页面资源、唯一迁移head052全部通过；实际执行全链迁移至052及数据库持久设置import/set，再对真实工行FY2024净息差进行字节/定位核验并拒绝篡改值。确认tools/tests/research和未提交P4模块不在镜像内。容器未挂载生产卷、未暴露端口、未运行API服务/worker/scheduler，退出后自动删除。证据为 `candidate-overlay.json`、`candidate-image-build.log`、`image-smoke-final.log`。全仓测试在宿主隔离环境执行，容器完成上述运行时烟测，不混称全仓测试均在容器内执行。

本轮**发布准备验收通过，实际部署待用户再次确认**。部署前仍须提交本轮文件、用批准完整SHA重新归档构建并核对目标机器/数据库现状及恢复材料；不得直接部署此未提交增量演练tag，也不得据自建临时库演练宣称生产库迁移已经完成。

## 执行偏差与范围

第一次 Linux/amd64 基础镜像拉取遇到 Docker Hub EOF，有限重试后构建成功。最初wheel的无隔离构建缺少本机setuptools，改用独立构建环境后成功；wheel烟测需要同时提供Dockerfile本来就单独复制的config目录。正式备份校验按既有防symlink规则拒绝macOS的`/tmp`别名路径，改用已解析的`/private/tmp`规范路径后校验和恢复通过。Docker VM不共享宿主`/tmp`文件，烟测输入改放共享的工作树`.dev/`后重跑。没有修改共享依赖环境或为通过门禁放宽校验。

文档子代理曾误用 `uv run ia --help`，导致在隔离工作树创建忽略的 `.venv` 并下载依赖；随后停止该方式，未触碰主虚拟环境、业务来源、真实模型或生产服务。该步骤不计为离线业务测试，依赖构建与业务联网分别记录。

本轮代码、文档与证据准备不授权实际部署。最终提交、目标环境现状、迁移兼容与恢复材料核对完毕后，再由用户确认实际部署及维护窗口。
