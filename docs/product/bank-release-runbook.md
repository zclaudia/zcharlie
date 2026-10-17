# 银行年度研究发布手册

状态：**待发布前门禁及人工最终确认**。本手册为本轮准备的执行稿，不表示生产已发布、来源许可已取得或生产库已验收。执行部署章节前，须由责任人完成第 5 节清单并明确确认目标与中断窗口。

适用范围：固定 10 家银行 FY2024/FY2025 年报事实与原件导入、只读离线同行批次及 API/页面读取。在线采集、调度、iFinD/其他外部 LLM 调用不属于离线银行发布；本手册不授权启动它们。实现与验收边界见[银行在线验收](../review/bank-online-acceptance-2026-10-17.md)、[两年度矩阵验收](../review/bank-fy2024-acceptance-2026-10-17.md)及[跨年度修订验收](../review/bank-rollover-acceptance-2026-10-17.md)。

## 1. 发布边界与候选准备

本轮只允许选择一个由主代理全量门禁通过的确切提交作为候选。发布代码须来自该提交；先检查工作树与提交号，不得把并行 P4 修改、未提交文件或其它会话产物混入候选。P4 独立失败须记入门禁结果并由主代理判定是否阻断，不得为银行发布顺手修改、回退或隐藏 P4 文件，也不得把局部银行通过写成全仓通过。

银行功能保留离线数据路径：原件、审计清单与已保存快照导入后，批次通过 `ia peers submit-offline-batch` 本地冻结和计算，不发上游请求，不需要 worker、scheduler 或 LLM。运行期可见性受数据库持久设置 `industry_peer_context_enabled` 控制（默认关闭）；是否开启须在第 5 节明确记录，不能仅改 `.env`。API lifespan 还会启动 `GatewayManager`，并在 StockDirectory 总数为零时后台启动 Baostock 全目录同步；因此 API-only 启动仍须先将 `gateway_enabled=false` 写入持久设置，并核对十家银行各有一条精确 `StockDirectory` 记录。只有 Listing 而缺 StockDirectory 不满足该门槛。API GET 与页面读取已保存数据。页面可显示缺口/候选状态；它们不得被解释为完整可比事实。

另设在线及模型门禁：本轮不执行 `ia peers collect --now`、`ia bank-annual acquire`、刷新任务、worker/scheduler，也不打开任何在线采集开关。相关长期自动抓取、缓存、展示与外部模型用途条件尚须分别核实；未有来源使用许可证据前，保持在线来源关闭。LLM/iFinD 设置不在本次发布步骤中更改，验证过程中不得创建真实模型任务或调用真实模型。银行只读展示开关与在线来源/模型调用开关是不同控制面，分别记录当前值与目标值。

### 1.1 镜像与服务范围

[`Dockerfile`](../../Dockerfile) 的运行镜像包含 `src/`、`config/`、`alembic/` 及应用依赖；不包含 `tools/`、`tests/`、`research/`。因此容器里不能直接运行仓库备份/恢复脚本或测试，也不能假定审计 fixture、PDF 原件已在镜像内。需要脚本时，通过一次性 `docker compose run --no-deps` 将已审查的工具目录、只读 release 输入目录显式挂入容器。

[`docker-compose.yml`](../../docker-compose.yml) 的默认 `up` 拓扑会使 API 依赖迁移，并在 API healthy 后启动 worker 与 scheduler。通用 [`tools/deploy.sh`](../../tools/deploy.sh) 还会自动备份、构建后执行完整 `docker compose up -d` 并等待 API/worker/scheduler 全部健康；它不是本次 API-only、离线银行流程的发布器。`tools/deploy-remote.sh deploy` 会推送 `main` 并调用该通用脚本，同样不得用于本轮。不得以新增共享 compose 修改来绕过此限制。发布按第 6 节的逐服务命令执行：先停 worker/scheduler、显式迁移、只启动 API。

## 2. 原件、审计输入与目标数据目录

原件和证据包不是 Git 或运行镜像的一部分。银行验收所需真实 PDF 位于被忽略的本地证据根目录；必须由发布责任人从已审查的归档介质复制到目标主机的临时 release 输入目录。不要从本手册或 Git 文件名推断目录中已有原件。至少准备：

- FY2025 十家完整年报及宁波银行适用的补充原件，以及 `tests/fixtures/bank_proofs_v2/audit-manifest.json` 和对应 source manifest；
- FY2024 十家完整年报及补充原件，以及 `tests/fixtures/bank_fy2024/` 下 `icbc.json`、`large.json`、`regional.json`、`scope.json` 与 two-year matrix；
- 对应证据目录树 `research/industry-data-validation/evidence/`；
- 本候选提交中的 `tools/` 脚本（仅用于一次性任务）；
- 与它们配套的审计/验收结果、来源 PDF 清单和复制前后哈希记录。

把这些放在一个明确的只读输入根（下文用 `<RELEASE_INPUT_ROOT>` 表示），保留上述相对目录结构。该目录不放 `.env`、API key、DB 文件或其他 secret。搬运前后对照交付清单中的 SHA-256；逐个确认文件存在、长度及哈希相符，并核对十家证券、年度、公告身份与原件版本。任何缺文件、哈希不符、目录引用越界、多个完整修订版无法确定或无法复验时停止，不能以验收文档中的数字代替原件。

数据库与 `DATA_DIR` 必须以目标环境实际挂载为准。运行 `docker compose config` 时其输出含敏感环境值，不得保存或粘贴完整结果；只通过受控本机方式确认服务使用同一数据库，并记录脱敏后的 DB 后端/挂载路径。当前生产文档提到 ai-server 与 `/app/db/app.db` 具名卷，但不能据此推定当前目标主机、卷、在线进程或 schema。第 5 节必须现场采集并由责任人确认。保持数据库与整个研究制品目录在同一备份/恢复边界；另外保存临时输入根，直到发布及恢复演练验收完成。

## 3. 部署前隔离回归与恢复演练

先在候选 checkout 和独立证据根运行只读包预检（它只核对清单/文件完整性，不替代语义、数据库或 API 验收）：

```sh
.venv/bin/python tools/bank_release_preflight.py \
  --repo-root "<CHECKOUT>" \
  --evidence-root "<RELEASE_INPUT_ROOT>" \
  --report "<NEW_REPORT_PATH>/bank-release-preflight.json"
```

回归只能在专用临时数据库、临时 `DATA_DIR` 和不与目标生产卷共享的工作目录中运行。显式设置 `DATABASE_URL`、`DATA_DIR` 到隔离路径；核实进程环境没有继承目标 `.env`。不要将生产库路径作为默认值，不要在容器中挂载生产卷执行验收。候选预检工具和银行发布演练应由主代理在同一确切候选提交上完成，保留命令、退出码、版本、schema before/after、回归报告、镜像标签/ID、日志摘要及备份 manifest SHA。失败的用例不得通过跳过、放宽、修改基线或修复 P4 后重标成本轮通过。

门禁至少包括：

1. 主代理确认基线与候选 SHA、工作树干净、提交中不含 P4 并行改动；检查 migrations/head 与候选镜像 revision 一致。
2. 跑银行 FY2024/FY2025 导入、原件哈希重验、在线路径关闭检查、离线批次计算、API GET/页面读取、旧版本/批次/研究不变性及备份恢复回归。覆盖真实原件的用例必须实际执行，缺件 skip 不算通过。
3. 对临时 DB + 全部关联制品执行备份和 `verify_backup.py`；恢复到全新空目录，用恢复后的 DB/制品再做银行 release check、迁移版本检查和只读 API/页面检查。证明 DB 与原件/研究制品成套恢复。不得在原目录上覆盖恢复。
4. 检查批次条目、年度/证券/来源、95/1/4 状态计数及历史记录内容哈希。FY2024 预期 47 verified、1 candidate、2 scope mismatch；FY2025 预期 48、0、2。明确解释唯一 FY2024 候选和四个平安单体口径排除；禁止把候选/排除填成 verified 或补零。

通用部署脚本所做备份不能替代以上恢复演练；它的回滚仅换镜像并不回退数据库或恢复研究制品。

## 4. CLI 命令契约与执行位置

以下数据库写命令均须在**目标应用容器**执行，使用同一目标数据库及 `DATA_DIR`，且只在第 5 节确认后执行。主机工作目录 `<CHECKOUT>` 是批准提交的 checkout；FY2025 证据根是 `<RELEASE_INPUT_ROOT>/research/industry-data-validation/evidence`，其余 FY2024 原件也按仓库相对路径放在 `<RELEASE_INPUT_ROOT>/research/`。工具/fixture 位于候选 checkout；运行镜像不含这些目录，使用一次性容器只读 bind mount。`<BACKUP_ROOT>` 是独立持久备份目录。所有路径均由发布责任人替换为绝对路径，不能照抄占位符。

操作员须记录实际 Compose project 名称及 machine-specific compose override 文件。旧服务命令使用 `docker compose -p "$COMPOSE_PROJECT" -f docker-compose.yml -f "$MACHINE_COMPOSE_OVERRIDE"`；建好候选后再在该命令后追加 `-f "$RELEASE_IMAGE_OVERRIDE"`。若环境没有 machine override，去掉该 `-f` 参数。目标主机必须沿用实际 production project/config，不得重新创建空 project。候选 override 只覆盖 `migrate` 与 `api` 的 `image: investment-agent:bank-release-<SHA>`、`pull_policy: never`，不变更 DB/data/network/ports。该发布专用override应持久保存在例如`<CHECKOUT>/.deploy/bank-release-<SHORT_SHA>.yml`，与实际命令参数一并归档；后续运维沿用它，不能删除后用默认Compose配置重新创建旧镜像服务。`run` 命令加 `--no-build`，API 启动加 `--no-build`，防止 compose 从当前目录另建或覆盖镜像。

执行示例前，在 Bash 维护终端设置已确认值并建立两个 wrapper；示例中的 `dc_old` 用于停服和旧镜像备份，`dc_candidate` 用于候选迁移、导入和启动：

```bash
COMPOSE_PROJECT='<CONFIRMED_COMPOSE_PROJECT>'
MACHINE_COMPOSE_OVERRIDE='<ABSOLUTE_MACHINE_OVERRIDE_PATH>' # 没有 override 时设为空字符串
RELEASE_IMAGE_OVERRIDE='<ABSOLUTE_PERSISTED_RELEASE_OVERRIDE_PATH>'
COMPOSE_ARGS=(-p "$COMPOSE_PROJECT" -f docker-compose.yml)
if [ -n "$MACHINE_COMPOSE_OVERRIDE" ]; then COMPOSE_ARGS+=(-f "$MACHINE_COMPOSE_OVERRIDE"); fi
dc_old() { docker compose "${COMPOSE_ARGS[@]}" "$@"; }
dc_candidate() { docker compose "${COMPOSE_ARGS[@]}" -f "$RELEASE_IMAGE_OVERRIDE" "$@"; }
```

发布专用image override仅覆盖`migrate`和`api`服务镜像。候选tag确定后，在目标主机写入上述持久文件（不放环境变量或密钥）：

```yaml
services:
  migrate:
    image: investment-agent:bank-release-<SHORT_SHA>
    pull_policy: never
  api:
    image: investment-agent:bank-release-<SHORT_SHA>
    pull_policy: never
```

先用 `dc_old` 停止服务和完成备份；构建候选后创建该 override，再用 `dc_candidate` 执行候选命令。每个 `run` 均加 `--no-build`，避免迁移或导入时重新构建。

FY2025 离线导入前，目标库必须已有完整的 A 股公司/上市目录，使固定十只证券均能由 ticker 精确解析；银行名单导入命令不会创建或猜测证券身份。若为新库，先按项目的证券目录准备流程离线导入并验证这十个 listing；本轮不提供或执行外部目录采集命令。然后把隔离证据包中的 `sample.json` 作为 `bank_run02_sample` 名单导入，显式绑定银行在线 source scope：

```sh
dc_candidate run --no-build --rm --no-deps \
  -v "<RELEASE_INPUT_ROOT>:/release:ro" \
  migrate ia peers import-roster \
  /release/research/industry-data-validation/evidence/run-02/sample.json \
  --industry bank --bank-online --cohort-key bank_run02_sample
```

这一步只创建不可变名单快照，不会发 HTTP 请求；虽绑定在线 source scope，发布批次仍从已导入原件/事实走显式 offline adapter source（见下面批次来源参数），绝不运行 `collect --now`。名单导入失败或证券缺失时，停止并先修复本地目录/输入。

FY2025 离线导入入口及实际参数：

```sh
dc_candidate run --no-build --rm --no-deps \
  -v "<CHECKOUT>/tests/fixtures:/release/tests/fixtures:ro" \
  -v "<RELEASE_INPUT_ROOT>/research:/release/research:ro" \
  migrate ia peer-annual import-bank \
  --evidence-root /release/research/industry-data-validation/evidence/run-02 \
  --report-period 2025-12-31 \
  --audit-path /release/tests/fixtures/bank_proofs_v2/audit-manifest.json \
  --originals-root /release \
  --bank-annual-source --cohort-key bank_run02_sample
```

FY2024 已审阅矩阵导入前，须已从同一目标目录成功导入十家银行的证券 listing 和上面的 `bank_run02_sample` roster。四个审计 bundle 位于候选 checkout；真实 PDF 由 evidence 根只读挂载。命令参数来自 `ia bank-annual import-reviewed`：

```sh
dc_candidate run --no-build --rm --no-deps \
  -v "<CHECKOUT>/tests/fixtures:/release/tests/fixtures:ro" \
  -v "<RELEASE_INPUT_ROOT>/research:/release/research:ro" \
  migrate ia bank-annual import-reviewed \
  --cohort bank_run02_sample \
  --evidence-root /release \
  --bundle /release/tests/fixtures/bank_fy2024/icbc.json \
  --bundle /release/tests/fixtures/bank_fy2024/large.json \
  --bundle /release/tests/fixtures/bank_fy2024/regional.json \
  --bundle /release/tests/fixtures/bank_fy2024/scope.json \
  --output /app/data/research-runs/bank-fy2024-matrix.json
```

真实部署前由主代理根据 `tools/bank_release_preflight.py`、验收器与实际输入核对 `evidence-root`/原件路径布局和容器只读挂载。`migrate` 服务默认 `command` 是 `ia migrate up`，Compose run 后的命令会覆盖它；每次只运行上述明确命令。不得把这些命令改成 `docker compose up` 或在含生产库环境下运行测试套件。

FY2024 的矩阵为 **47/1/2**、FY2025 为 **48/0/2**（verified/candidate/scope mismatch）；两次导入合计 **95/1/4**。原件导入只追加可审计版本；后续离线批次应明确固定 cohort、年度、供应商和适配器版本：

```sh
dc_candidate run --no-build --rm --no-deps migrate ia peers submit-offline-batch \
  --cohort-key bank_run02_sample \
  --report-period 2024-12-31 \
  --provider eastmoney --adapter-version bank-annual-v1 \
  --batch-run-id bank-fy2024-<RELEASE_ID>

dc_candidate run --no-build --rm --no-deps migrate ia peers submit-offline-batch \
  --cohort-key bank_run02_sample \
  --report-period 2025-12-31 \
  --provider eastmoney --adapter-version bank-annual-v1 \
  --batch-run-id bank-fy2025-<RELEASE_ID>
```

`--bank-annual-source` 是 FY2025 导入必需的显式 opt-in；省略时旧导入路径写成 `offline-run02-v1`，与本次银行名单绑定的 `bank-annual-v1` source scope 不一致，批次读不到新快照。FY2024 `import-reviewed` 也写入 bank annual 快照版本。`submit-offline-batch` 是离线冻结/计算命令，不触网、不启动 worker，也不会自动改变历史批次。发布批次后记录返回的 batch id、内容 hash、输入快照 ID、生成数、缺口/冲突和制品目录；旧研究与历史批次必须逐项保持原 hash。批次身份中的 `<RELEASE_ID>` 应替换为实际批准的短唯一标识。

## 5. 部署前最终确认清单（须由人填写并确认）

此表在任何目标主机写操作前填写；未知值不得猜测。

| 项目 | 部署前记录 |
|---|---|
| 确认人、确认时间（含时区）、授权的维护窗口 | `<确认人 / 时间 / 窗口>` |
| 目标主机、checkout 绝对路径、当前分支与当前 HEAD | `<实测值>` |
| 候选提交 SHA、镜像 tag 与 immutable image ID/digest | `<主代理验收后填>` |
| 当前 API/worker/scheduler 实际运行状态与版本 | `<现场采集，不据旧文档推定>` |
| 目标数据库实际后端、DB 路径/具名卷、Compose project | `<脱敏位置与映射>` |
| 目标 DB 当前 Alembic revision；候选镜像预期 head；迁移差异 | `<实测 revision 与差异>` |
| 当前 DATA_DIR、研究制品根、运行时日历/原件位置 | `<现场采集>` |
| 可验证的部署前备份路径、manifest/hash、DB 与制品覆盖 | `<验证结果>` |
| 新空目录恢复演练路径、DB revision 与业务核验结果 | `<演练结果>` |
| FY2024/FY2025 输入根、源清单/原件清单 hash | `<复制前后匹配结果>` |
| `industry_peer_context_enabled` 当前值与本次目标值 | `<逐项确认>` |
| `investor_agent_enabled`、`investor_agent_background_enabled`、`gateway_enabled` 当前值与目标值 | `<逐项确认；目标分别 false / false / false>` |
| 固定十家证券均存在精确 StockDirectory 行（ticker/status/synced_on） | `<只读查询结果，须十条>` |
| 在线采集/调度开关、当前任务状态与目标关闭状态 | `<逐项确认>` |
| 外部 LLM/iFinD 是否可能被调用；本次明确不允许真实调用 | `<状态/隔离证明>` |
| 是否允许停止 API/会话、预计中断与恢复联系人 | `<责任人明确批准>` |
| 回滚所需旧镜像 ID、备份、原件/制品副本均可读 | `<核对结果>` |
| 主代理 B4 全量发布门禁结论，含 P4 失败处理决定 | `<结论与证据>` |

仅当所有字段有实测值、恢复材料已验证、责任人明确同意中断窗口，且主代理确认发布 SHA 与镜像一致时，才可进入第 6 节。任何数据库、schema、挂载、开关或镜像身份不清楚时停止在只读采集阶段。此文档本身不构成该确认。

## 6. 确认后执行：仅 API 与离线银行

所有步骤在已确认的**目标主机 `<CHECKOUT>`** 目录执行，保持终端记录但对密钥值脱敏。先验证主机身份、HEAD、工作树、镜像 ID、compose project、DB/data 挂载和现有服务状态与确认表一致。不得运行 `tools/deploy.sh` 或 `tools/deploy-remote.sh`。

1. 进入已批准维护窗口后，记录现有容器、DB revision、队列/后台任务状态及旧镜像 ID；然后停止所有 DB/制品写入者，包括 worker、scheduler 和 API：

   ```sh
   dc_old stop worker scheduler api
   ```

   API 停机会造成用户可见中断。若无法确认写入已静止，或窗口不允许此中断，停止发布。
2. 所有写进程停止后，使用**旧代码/旧镜像和当前旧 schema**对同一 DB 与 `DATA_DIR` 做一致备份。将已确认的 DB 容器内路径填入 `DB_PATH_IN_CONTAINER`、备份目录名填入 `BACKUP_NAME`，然后通过一次性旧镜像容器显式挂载脚本与持久备份根：

   ```sh
   dc_old run --no-build --rm --no-deps \
     -v "<CHECKOUT>/tools:/app/tools:ro" \
     -v "<BACKUP_ROOT>:/backups" \
     migrate python /app/tools/backup_research.py \
     --data-dir /app/data --db "$DB_PATH_IN_CONTAINER" \
     --output "/backups/$BACKUP_NAME"
   dc_old run --no-build --rm --no-deps \
     -v "<CHECKOUT>/tools:/app/tools:ro" \
     -v "<BACKUP_ROOT>:/backups:ro" \
     migrate python /app/tools/verify_backup.py \
     --backup-dir "/backups/$BACKUP_NAME"
   ```

   `dc_old run migrate`选用的镜像必须核对为记录的旧版本；不能只假定`investment-agent:dev`标签仍指向运行中的旧镜像，必要时用只含旧image ID的独立override固定备份命令。检查manifest覆盖数据库、原件与`research-runs/`，记录哈希和旧image ID。备份根、输入根与恢复目录均使用已解析的规范绝对路径；工具会拒绝symlink路径（如macOS的`/tmp`别名应使用`/private/tmp`）。备份/验证失败即中止并保持服务停止。
3. 将批准 SHA 通过 `git archive` 解到全新空临时目录，再从该目录构建唯一候选 tag（例如 `investment-agent:bank-release-<SHORT_SHA>`）：

   ```sh
   BUILD_DIR="$(mktemp -d)"
   git -C "<CHECKOUT>" archive "<APPROVED_SHA>" | tar -x -C "$BUILD_DIR"
   docker build -t "investment-agent:bank-release-<SHORT_SHA>" \
     --build-arg GIT_SHA="<APPROVED_SHA>" \
     --build-arg APP_UID="$(id -u)" --build-arg APP_GID="$(id -g)" \
     "$BUILD_DIR"
   rm -rf -- "$BUILD_DIR"
   ```

   命令须由实际拥有目标 `./data` bind mount 的 Linux 部署用户执行，使 UID/GID 与宿主目录权限匹配；不得使用不同用户构建或沿用默认 10001 导致不可写。记录旧/候选 image ID；候选 `docker image inspect` 的 `org.opencontainers.image.revision` 必须等于批准 SHA。不得覆盖旧 `investment-agent:dev`。创建只覆盖 `migrate` 和 `api` image 的本轮临时 Compose override；保留当前 project、machine override 及 DB/data/network/ports。之后候选命令经 `dc_candidate` 执行且带 `--no-build`。镜像不含工具和测试；临时容器需显式 bind mount。
4. 用候选镜像在同一 DB 查询迁移前 revision 并留存：`dc_candidate run --no-build --rm --no-deps migrate ia migrate current`；再执行 `... migrate ia migrate up`，随后再次查 `current`。revision 必须到候选预期 head 且与主代理报告一致。异常或版本不符时不得导入/启动 API，转第 7 节恢复。
5. 在任何名单/年度写入和 API 启动前，先用候选容器完成 settings 的一次性 env/app.yaml 导入，再读取当前持久设置并记录旧值。API lifespan 也会调用此导入；若尚未 claim，首次启动会把明确配置的 env/app.yaml 值写入 DB，可能覆盖发布目标。因此不要跳过此步骤，也不要使用 `--force`：

   ```sh
   dc_candidate run --no-build --rm --no-deps migrate ia settings import
   ```

   若显示 `Already imported`，无需再次导入；若显示已导入若干设置，记录结果但不要保存任何 secret 值。随后使用同一候选容器、同一 DB 读取当前持久设置并记录旧值：

   ```sh
   dc_candidate run --no-build --rm --no-deps migrate ia settings get industry_peer_context_enabled
   dc_candidate run --no-build --rm --no-deps migrate ia settings get investor_agent_enabled
   dc_candidate run --no-build --rm --no-deps migrate ia settings get investor_agent_background_enabled
   dc_candidate run --no-build --rm --no-deps migrate ia settings get gateway_enabled
   ```

   在本次目标值经责任人确认后，用 `ia settings set` 写入数据库持久值（不只是修改 `.env`）：

   ```sh
   dc_candidate run --no-build --rm --no-deps migrate ia settings set industry_peer_context_enabled true \
     --note "<RELEASE_ID>: approved offline bank read-only display"
   dc_candidate run --no-build --rm --no-deps migrate ia settings set investor_agent_enabled false \
     --note "<RELEASE_ID>: keep Investor Agent disabled during offline bank release"
   dc_candidate run --no-build --rm --no-deps migrate ia settings set investor_agent_background_enabled false \
     --note "<RELEASE_ID>: keep background research disabled during offline bank release"
   dc_candidate run --no-build --rm --no-deps migrate ia settings set gateway_enabled false \
     --note "<RELEASE_ID>: prevent gateway connection during offline bank release"
   ```

   再用四条 `settings get` 逐项确认结果为 `true/false/false/false`，保存脱敏收据。若当前值与批准目标不同，先记录原值；若任一步失败，停止并恢复原值。回滚时可按记录的旧值使用 `ia settings set KEY <OLD_VALUE> --note "<RELEASE_ID>: restore prior setting"`。这些是可恢复的 DB 写操作，只能在本节明确确认后执行。
6. API lifespan 在 startup 时检查全库 StockDirectory 行数是否为零；若为零会异步发起 Baostock 全目录同步。为证明本次启动不会触发该路径，须在启动前对目标 DB 作只读核对：`stock_directory` 中固定十个 canonical ticker 各存在一行，并记录 `status` 与 `synced_on`。若 DB 为 SQLite，可通过候选 `migrate` 一次性容器运行：

   ```sh
   dc_candidate run --no-build --rm --no-deps migrate sqlite3 "<DB_PATH_IN_CONTAINER>" \
     "SELECT ticker,status,synced_on FROM stock_directory WHERE ticker IN ('601398.SH','601939.SH','601288.SH','600036.SH','601166.SH','000001.SZ','600919.SH','601009.SH','002142.SZ','601128.SH') ORDER BY ticker;"
   ```

   结果必须是十行且 ticker 与固定名单完全一致。若为非 SQLite 数据库，使用已批准的只读运维方式对同一张表执行等价查询。少行、异常 ticker 或查询失败均停止；不要让 API 空目录自动初始化代替本地证据准备，也不要为此调用 `ia snapshot sync-universe --now`。随后按第 4 节顺序导入 bank roster、FY2024 矩阵、FY2025 verified manifest。核对名单 revision、写入/幂等结果、事实状态、来源 scope 和原件校验。任何不是 47/1/2 与 48/0/2 的结果均停止，不创建新批次。运行回归和预检只能用隔离 DB；目标库只运行发布导入命令与只读核验。
7. 用 `bank-annual-v1` source scope 分别创建 FY2024、FY2025 离线批次。核对合计 95 verified、1 candidate、4 scope mismatch；检查缺口、冲突及每项原件引用。FY2024 常熟银行 NIM candidate 不可升级，平安两年各自 NPL 与 coverage 均保持 scope mismatch，不得用供应商数字填补集团字段。
8. 仅启动 API：

   ```sh
   dc_candidate up --no-build -d --no-deps api
   ```

   `--no-deps` 跳过 API 的 `migrate` 依赖（已显式完成迁移）；worker/scheduler 不是 API 的依赖，但无服务名的 `docker compose up -d` 会启动全部服务，禁止使用。确认 worker/scheduler 仍停止，API `/healthz` 报告候选 revision，运行镜像 ID 与批准一致，日志无启动/DB 错误；另检查 gateway 维持 disabled。
9. 使用现有受控浏览器会话或已批准的 API session 做鉴权只读核对，不把 API key 放在命令行、日志或文档中：`GET /industries/bank_run02_sample`，分别指定 `report_period=2024-12-31` 与 `2025-12-31`；`GET /stocks/002142.SZ/industry-peer-context?batch_run_id=<对应批次>` 及 `/stocks/002142.SZ/industry-peer-context/companies?batch_run_id=<对应批次>`。也可按 release matrix 抽查建行/工行/常熟银行及平安；确认年度、来源、批次绑定、原件证据、缺口和排除状态。经浏览器打开银行证券个股页，核对“行业与同行参照”区块对应同一批次；不触发“刷新资料”等 POST 操作。`industry_peer_context_enabled=false` 时，`GET /industries/{cohort_key}` 应返回 `enabled=false` 和 gap；个股同行接口返回 `status=not_supported`。如经确认本次设置 true，则接口应返回已保存的离线批次，并仍不发上游请求。
10. 对比部署前数据库/制品备份、旧研究 run、已冻结批次与旧 strict references。新导入只能追加；不得重写旧快照、研究或批次。保存脱敏日志、镜像 revision、DB revision、批次 IDs/hashes、95/1/4 统计和页面/API 截图或响应摘要到发布证据目录。最后复核在线采集、worker、scheduler、Investor Agent、gateway 与确认表一致。

## 7. 失败与回滚

任一步失败先停止发布批次/进一步写入，并保持 worker/scheduler 停止。记录当前镜像 ID、运行 API 状态、schema revision、数据库和制品是否有写入、备份路径及错误摘要；不要删除新镜像或部署前备份。

回滚须将代码、schema、数据库及制品作为匹配集合处理：

- 若没有发生 schema 迁移，且确认旧镜像仍兼容当前 DB 和数据，才可将 API 恢复到第 5 节记录的旧镜像 ID 并仅重建 API。
- 若 schema 已前进，或导入/发布已改数据库，则不能仅切回旧代码。停止 API/所有写服务；使用发布前已验证备份恢复 DB **和配套 DATA_DIR 原件/研究制品**到一致的目标状态，再以对应旧代码/镜像启动。恢复工具拒绝覆盖已存在目录，应恢复到新目录/卷，验证 DB revision、备份清单和业务引用后，按责任人批准的切换方式恢复服务。
- 不要运行 `tools/deploy.sh rollback` 作为通用回滚。它只重标 `investment-agent:prev` 并启动 API、worker、scheduler，不执行 schema/data 恢复；schema 改变时 `--force` 会绕过保护而非自动修复兼容。
- 如果无法证明旧代码与当前 schema/数据兼容，维持服务停止并交由负责人决定从完整备份恢复；不能试探性启动旧 worker/scheduler。

完整恢复遵照[备份恢复手册](backup-and-recovery.md)：先 `verify_backup.py`，再将 `restore_research.py --backup-dir <备份> --output <全新恢复目录>` 恢复到新目录并重验。现有部署手册描述的生产主机、DB 卷及每日备份配置仅作为历史运维记录；执行时须以第 5 节现场采集结果为准。
