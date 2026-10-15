# 备份与恢复手册（MVP）

港美 HK2-08 M1 的实测矩阵与生产/演练状态区分见 [验收报告](plans/hk-us-expansion/29-hk2-08-m1-acceptance.md)。

适用范围：单机部署的 `zcharlie`（SQLite + `data/` 制品目录）。目标是：
数据库与制品可以通过 `run_id` 对齐恢复，且恢复后迁移版本、观察池、研究运行、
快照、diff 与制品全部可读。

MVP 保留策略：**不自动删除任何原始证据**。备份按日期目录保存，由使用者自行
决定归档周期；建议至少保留 30 天的每日备份。

## 1. 备份内容与位置

| 内容 | 位置 | 说明 |
|---|---|---|
| 数据库（状态真相） | `data/app.db` | 迁移版本、观察池、run、快照、diff、通知状态 |
| 研究制品（审计真相） | `data/research-runs/<run_id>/` | 原始响应、canonical、scores、报告 |
| 变化通知 | `data/notifications/` | meaningful-change Markdown |
| 港美资料与原件归档 | `data/research-material/` | 单文件原件、多响应正文与版本化清单；采集记录和资料版本关联随数据库备份 |
| 港美复核原件 | `data/reviewed-annual/sources/` | HK2-07 复核导入保存的内容寻址原件，复核索引位于资料 payload，必须随数据库恢复 |
| 运行时交易日历 | `data/calendars/a_share.yaml`（存在时） | 数据健康页「应用候选」写入的副本，优先于仓库基线 `config/calendars/a_share.yaml`；不备份则恢复后回落到基线（缺年份时 fail-closed） |

数据库与制品必须通过 `run_id` 关联恢复，缺一不可。

港美资料还须恢复数据库引用的 `research-material/`、`reviewed-annual/` 相对目录。
备份工具同时复制这些目录及 `calendars/`，逐文件校验正文和嵌套清单；只恢复数据库不能恢复离线重放与原件核对能力。

## 2. 备份步骤

```bash
# 1. 停止写入（保证制品目录与数据库一致）
docker compose stop          # Compose 部署
# 或手动停止 uvicorn / ia worker start / ia scheduler start 对应进程

# 2. 生成备份（含 manifest.json：每个文件的大小与 SHA-256）
.venv/bin/python tools/backup_research.py --data-dir ./data --output ~/backups/$(date +%F)

# 3. 立即校验
.venv/bin/python tools/verify_backup.py --backup-dir ~/backups/$(date +%F)

# 4. 重启服务
```

说明：SQLite 的在线备份 API 本身可产出一致快照，但制品目录按文件复制，
因此先停止写入才能保证"数据库引用的制品 == 备份里的制品"。

### 数据库在 Docker 卷中（macOS + colima 推荐）

Docker 部署在 macOS（colima / Docker Desktop）上时，`./data` 经 virtiofs 挂载，
宿主机进程看不到虚拟机内的 SQLite 文件锁：宿主机上的 `sqlite3` 或本机 `ia`
可能把自己当作最后一个 WAL 连接，删除容器正在使用的 `-wal`/`-shm`，之后所有查询报
`disk I/O error`。因此建议把数据库放进具名卷（如挂到 `/app/db`，并设
`DATABASE_URL=sqlite+aiosqlite:////app/db/app.db`），制品与通知仍留在 `./data`。
此时查询与备份都在容器内进行，`--db` 指定数据库位置：

```bash
docker compose exec -T api sqlite3 /app/db/app.db

docker compose stop
docker compose run --rm --no-deps -v ./tools:/app/tools:ro -v ~/backups:/backups api \
  python tools/backup_research.py --data-dir /app/data --db /app/db/app.db \
  --output /backups/$(date +%F)
docker compose start
```

## 3. 恢复步骤

```bash
# 1. 停止目标环境所有服务；先校验备份，不覆盖现有 data
.venv/bin/python tools/verify_backup.py --backup-dir ~/backups/2026-10-06

# 2. 恢复到不存在的新目录（工具复制前、复制后均校验）
.venv/bin/python tools/restore_research.py \
  --backup-dir ~/backups/2026-10-06 --output restored/data

# 3. 以显式指向恢复目录的配置确认迁移版本
DATABASE_URL=sqlite+aiosqlite:///restored/data/app.db .venv/bin/ia migrate current
# 迁移版本须与备份记录一致；旧版本恢复后按升级说明执行 ia migrate up。

# 4. 港美 M1 环境在启动前做业务完整性检查（报告写到 data 之外）
.venv/bin/python tools/hk208_release_check.py \
  --database restored/data/app.db --data-dir restored/data \
  --report restored/m1-check.json --require-m1

# 5. 检查通过后启动 API，抽查财务页、精确引用及既有 run/diff/制品。
#    M1 不启动在线采集；worker/scheduler 是否启动按对应环境运行手册执行。
```

`restore_research.py` 拒绝已存在的输出目录，先校验清单，再复制到暂存目录，验证后才完成恢复；
它不迁移数据库、不加载部署配置。校验失败保留备份且不创建成功恢复目录。
`verify_backup.py` 证明备份文件与清单一致；`hk208_release_check.py` 进一步证明数据库引用的
复核原件、资料版本与计算事实配套，不以“清单校验通过”代替业务恢复验收。

## 4. 恢复演练（自动化）

`tests/integration/test_backup_restore.py` 在临时目录完成一次完整演练：
建库 → 写入观察池与研究运行（含制品）→ 备份 → 校验 → 篡改检测 →
恢复到新目录 → 校验迁移版本与数据可读。该测试离线运行，属于默认套件。

港美 HK2-08 M1 使用独立的新目录运行，不读取生产 DATABASE_URL/DATA_DIR：

```bash
PYTHONPATH=src .venv/bin/python tools/hk208_m1_drill.py \
  --work-dir ./data/hk208-m1-acceptance/run-01 \
  --manifest config/research_material/annual_validation_2026-10-15.json \
  --source-root data/hk2-latest-evidence/2026-10-15/raw
```

工作目录必须不存在；演练按 048 旧库备份 → 升级 head → 受控八家导入与计算准备 → 配套备份 →
搬迁恢复 → 新进程只读核验执行。真实报告和浏览器结果记录在对应 HK2-08 验收文档；
本地演练通过不表示已迁移生产库或已开启在线刷新。

若需要恢复到迁移前版本，使用 `restore_research.py` 将 `backup-before-049` 恢复到另一个新目录，
确认 `ia migrate current` 为 048，并对照备份核验旧行及原件后再决定后续操作；不通过降级迁移覆盖现有数据。
本次另行恢复的 `rollback-048` 验证结果见上述验收报告。

## 5. 已知限制

- MVP 无自动增量备份；每次为全量（数据库体积在个人规模下可接受）。
- 恢复后 `artifact_manifest` 中的绝对路径如与新的 `DATA_DIR` 不同，
  下载端点会按"manifest 路径必须位于该 run 目录内"的安全规则拒绝——
  因此恢复时必须保持 `DATA_DIR` 与备份来源一致的相对结构（见第 3 节）。
- 不备份 LLM 调用成本之外的秘密；`.env` 不在备份范围，请妥善保管。
- 运行时配置（含加密的 LLM / iFinD key，表 `app_setting*`）随数据库一起备份；解密需要
  `.env` 中的 `SETTINGS_ENCRYPTION_KEY`。恢复到新环境时若该主密钥丢失，非密钥配置照常可用，
  密钥项在设置页显示「无法解密」，重新填写即可。
