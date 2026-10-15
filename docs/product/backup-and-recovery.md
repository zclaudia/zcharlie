# 备份与恢复手册（MVP）

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
| 运行时交易日历 | `data/calendars/a_share.yaml`（存在时） | 数据健康页「应用候选」写入的副本，优先于仓库基线 `config/calendars/a_share.yaml`；不备份则恢复后回落到基线（缺年份时 fail-closed） |

数据库与制品必须通过 `run_id` 关联恢复，缺一不可。

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

### 2.1 生产环境每日备份（ai-server）

生产环境（[运维手册 2.2](operations.md#22-生产环境ai-server)）由 ai-server 的 cron 每天北京时间 03:15
（`15 19 * * *`，主机时区 UTC）执行 `~/.local/bin/zcharlie-backup.sh`：

- 在线备份，服务不停：先用一次性容器以 SQLite 备份 API 快照数据库，再复制制品，备份引用的制品必然齐全；
- 写入 `~/backups/YYYY-MM-DD`（按北京时间命名），`verify_backup.py` 校验后把 `app.db` 压缩为 `app.db.zst`（恢复：`zstd -d app.db.zst`）；
- 保留最近 7 份每日备份 + 最近 4 份周日备份；当天重复执行不会覆盖已有备份，而是先写 `.partial` 再替换；
- 日志 `~/logs/zcharlie-backup.log`，失败行以 `FAILED:` 开头（没有桌面通知，巡检时 `grep FAILED`）。

`tools/deploy.sh` 每次部署前另存一份到 `~/backups/deploy-<时间>-<提交号>`（保留 5 份）。
Mac 时期的备份（2026-10-09 起）已拷到 ai-server 的 `~/backups`；Mac 上的 launchd 任务
`local.zhvala.zcharlie-backup` 已停用。

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
# 1. 停止所有服务，进入全新的或已清空的 data 目录
mkdir -p restored/data
# 2. 从备份复制（cp 或 rsync -a）
cp  ~/backups/2026-10-06/app.db restored/data/app.db
cp -a ~/backups/2026-10-06/research-runs restored/data/
cp -a ~/backups/2026-10-06/notifications restored/data/

# 3. 先校验备份（在任何恢复前）
.venv/bin/python tools/verify_backup.py --backup-dir ~/backups/2026-10-06

# 4. 以指向恢复目录的配置启动，并确认迁移版本
DATABASE_URL=sqlite+aiosqlite:///restored/data/app.db .venv/bin/ia migrate current
# 预期输出：012 (head)。若备份来自旧版本，先执行 ia migrate up。

# 5. 启动 API/worker/scheduler，抽查一条 run 的结果、diff 与制品下载。
```

## 4. 恢复演练（自动化）

`tests/integration/test_backup_restore.py` 在临时目录完成一次完整演练：
建库 → 写入观察池与研究运行（含制品）→ 备份 → 校验 → 篡改检测 →
恢复到新目录 → 校验迁移版本与数据可读。该测试离线运行，属于默认套件。

## 5. 已知限制

- MVP 无自动增量备份；每次为全量（数据库体积在个人规模下可接受）。
- 恢复后 `artifact_manifest` 中的绝对路径如与新的 `DATA_DIR` 不同，
  下载端点会按"manifest 路径必须位于该 run 目录内"的安全规则拒绝——
  因此恢复时必须保持 `DATA_DIR` 与备份来源一致的相对结构（见第 3 节）。
- 不备份 LLM 调用成本之外的秘密；`.env` 不在备份范围，请妥善保管。
- 运行时配置（含加密的 LLM / iFinD key，表 `app_setting*`）随数据库一起备份；解密需要
  `.env` 中的 `SETTINGS_ENCRYPTION_KEY`。恢复到新环境时若该主密钥丢失，非密钥配置照常可用，
  密钥项在设置页显示「无法解密」，重新填写即可。
