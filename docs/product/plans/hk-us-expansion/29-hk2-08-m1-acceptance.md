# HK2-08 M1 本地发布与备份恢复验收

验收日期：2026-10-16（Asia/Shanghai）；起点 `c14c1ab`；工作树 `codex/hkus-archive`。
按 [28](28-hk2-08-m1-execution.md) 分为备份恢复、只读发布检查、可复现演练三个 GPT-6 Luna 子任务；
主代理负责接口审查、真实原件集成、独立负例、恢复后的浏览器验收和最终门禁。

**M1 本地演练通过。** 本次只使用新演练库，没有操作生产库、部署、push 或合入 main；在线刷新保持关闭。
原件仍只核对截至 2026-10-15，不以本轮演练时间推定当前没有新披露。

## 交付与操作顺序

1. `backup_research.py` 将数据库、`research-material/`、`reviewed-annual/`、研究制品、通知与存在的运行时日历一起备份。源 SQLite 只读打开，使用一致快照；拒绝与源重叠、非空或符号链接输出，目录/文件权限为 0700/0600。
2. `verify_backup.py` 暴露可复用校验函数，拒绝畸形/空清单、缺少 app.db、重复及非规范路径、父目录符号链接、缺损/篡改及清单外文件，兼容既有 v1 清单。
3. `restore_research.py --backup-dir PATH --output PATH` 先验证备份，复制到同父私有暂存目录，再验证清单和 SQLite integrity，通过后才完成恢复；不覆盖既有目标，不自动迁移，不加载部署配置，异常清理本次暂存目录。
4. `hk208_release_check.py --database PATH --data-dir PATH --report PATH --require-m1` 以 mode=ro 检查迁移、数据库 integrity/FK、实际身份、资料内容哈希、有效复核回执、所有引用的原件哈希、精确 evidence 字段与版本、计算输入哈希和当前复核版本关联。报告不能覆盖数据库、其 sidecar 或 DATA_DIR 内原件。
5. `hk208_m1_drill.py` 创建全新隔离目录，完成 048 旧库哨兵及备份 → 升级 049 → 八家导入两次 → 显式计算准备 → 配套备份 → 新目录恢复 → 新进程 API/引用/只读发布检查。所有数据库路径由演练目录确定，外部 DATABASE_URL/DATA_DIR 不参与目标选择；父子进程均禁止网络。

M1 检查要求八家各有当前有效复核资料及至少一个由这些资料支持的合格 material_metrics 计算；
不把任意同公司旧计算算作通过。缺失、不适用和可比性限制继续保留，不为凑数量生成额外计算。
最小测试包生成 36 个计算；真实原件包生成 40 个，二者均逐条核验保存与恢复引用一致。

复现命令（工作目录必须不存在）：

```sh
PYTHONPATH=src python tools/hk208_m1_drill.py \
  --work-dir data/hk208-m1-acceptance/run-01 \
  --manifest config/research_material/annual_validation_2026-10-15.json \
  --source-root data/hk2-latest-evidence/2026-10-15/raw
```

操作细节见 [备份恢复手册](../../backup-and-recovery.md)。原件核对清单 SHA-256 沿用
`2b75bd0c612b174a1a82d8cd23f32627309e2b6235d6b0fa9e8e538fca7b132a`。
无新增迁移，演练的是已有 048→049 路径及其不可变保护。

## 真实资料集成与独立复验

真实演练运行于本地时间 00:18:52–00:23:16（报告时刻为 UTC 2026-10-15 16:18:52–16:23:16），退出 0。
生成报告 `data/hk208-m1-acceptance/run-01/report.json`；以下数据库与目录全部位于该隔离根目录。

| 验收项 | 结果 / 证据 |
| --- | --- |
| 迁移前备份 | 048 库含通过正常仓储建立的 A 股观察池和 MaterialStore 旧资料/原件哨兵；备份 2 个文件，清单验证通过 |
| 升级与旧行 | 升至 head 049，旧行内容和原件不变，直接修改旧 research_dataset 被不可变触发器拒绝 |
| 迁移前恢复 | 主代理另外恢复 `backup-before-049` 到新 `rollback-048`；迁移版本仍为 048，观察池/资料行与原件哈希完全相同，不可变保护仍生效；未通过 downgrade 回退 |
| 实际导入 | 八家 complete，首次新增 8，第二次复用 8；核对日与实际导入时刻独立保存 |
| 资料与计算 | 总资料 9 份（旧哨兵 1 + 复核 8）、唯一 evidence 85 个、合格计算 40 个；响应 acquisition 0，采集任务 0 |
| 配套备份 | `backup-restorable` 含 164 个文件，包括 154 个唯一复核原件、9 个资料原件/索引及数据库；verify 通过 |
| 搬迁恢复与只读检查 | SQLite integrity=ok、FK 0、迁移 049/049，8/8 有效复核资料、40/40 当前复核计算；正常财务 API 8/8 返回 200 |
| 原位置不可用 | 主代理把演练源 `data/` 改名为 `source-retired/` 后继续检查恢复目录；没有回退读取原数据目录 |
| 精确引用 | 源位置不可用时，新进程解析 40 个计算及 116 次事实引用，状态全部 ok，引用与原保存版本一致 |
| 网络 | 父/子进程的 connect、connect_ex、create_connection、getaddrinfo 守卫记录尝试 0；浏览器服务外部连接尝试 0 |

迁移前备份清单 SHA-256：`bfc1f0781ca589f2e3585a0300a7c450be861691766ee3e5d06454c62d1468a4`；
完整恢复备份清单 SHA-256：`dd07df4ea19564ccb035f0018b52c88535729f2b0f36c85a432fd83be03d2232`。

主代理在恢复副本中独立执行删除原件、改动字节、数据库与迁移前原件目录错配三个负例，均被 M1 检查拒绝；
修复副本后重新通过。检查期间数据库文件哈希及修改时间不变。定向测试还覆盖有效文件清单对应损坏 SQLite 时
恢复失败、不留下最终目标或暂存残留；既有输出目录保留原内容；错误身份、迁移、事实引用、locator 和 input_hash 均拒绝。

## 恢复后的正常浏览器

使用独立 API 进程、显式恢复库及恢复 DATA_DIR、正常表单登录，在线来源关闭，外部网络被阻断。
复用 `tools/hk207_browser_acceptance.py`，八家各在桌面 1440×1000、移动 390×844 访问正常股票路由。

- **16/16 视图通过**，无页面横向溢出；外部请求、JS 异常、控制台错误、网络失败和 HTTP 错误均为 0。
- 全部可见事实的指标、期间、原币金额与 API 相同；桌面/移动一致。汇丰 HKD 报价/USD 报表、TSM ADS/USD 报价/TWD 报表均正确。
- 核对截至日仍为 2026-10-15，最近采集成功保持未知；online_refresh、行情、估值和分红等能力均保持 false，缺失/不适用未补零。
- 主代理人工检查汇丰桌面和 TSM 移动截图；恢复后的页面日期、来源关闭及许可未知可读。
- 独立八家 GET 前后资料/证据/计算/acquisition/任务行数均不变（9/85/40/0/0），没有触发采集。验收服务完成后已关闭。

制品保存在忽略目录 `data/hk208-m1-acceptance/run-01/`：演练报告、两份备份与恢复副本、
`rollback-048-check.json`、`independent-checks.json`、`final-reference-check.json`、
`restored-contexts.json`、`browser-comparison.json`、`browser-restored/report.json`、16 张矩阵截图、人工截图及日志。
CLI 自身只声明 API/引用验收；本节浏览器结论来自主代理独立运行，不从 CLI 成功状态推定。

## 最终门禁

Python/pytest/Ruff/mypy 使用主 checkout 的 `.venv`；`PYTHONPATH=$PWD/src`，测试 API_KEY 使用假值，
DATABASE_URL、DATA_DIR、pytest 临时目录分别隔离。最终各组均退出 0。

| 命令 | 结果 | 日志 |
| --- | --- | --- |
| `pytest -q tests/unit` | 2129 passed / 16 skipped / 6 deselected | `/tmp/hk208-unit-final.log` |
| `pytest -q tests/integration` | 634 passed / 8 skipped | `/tmp/hk208-integration.log` |
| `pytest -q tests --ignore=tests/unit --ignore=tests/integration` | 717 passed | `/tmp/hk208-other.log` |
| 合计 | **3480 passed / 24 skipped / 6 deselected** | 同步保留到演练目录 |
| `ruff check src tests tools/backup_research.py tools/verify_backup.py tools/restore_research.py tools/hk208_release_check.py tools/hk208_m1_drill.py` | 通过 | `/tmp/hk208-final-ruff.log` |
| `mypy src` | 379 个源文件通过 | `/tmp/hk208-final-mypy.log` |
| `git diff --cached --check` | 通过 | 提交前执行 |

24 个跳过项沿用既有依赖条件（含缺少 run-02 行业原件），6 个联网测试按离线配置排除。
既有 A 股 golden、评分、发布与恢复测试均包含在本次完整回归中，没有修改其预期来通过门禁。

## 发布状态与剩余边界

| 状态 | 本次结果 |
| --- | --- |
| 代码 | 独立工作树实现、验收并 commit；未合入 main、未部署 |
| 数据库迁移 | 仅新演练库 048→049；生产库未操作 |
| 原件及资料导入 | 仅演练库与恢复副本；生产环境未导入 |
| 来源启用 | `ONLINE_REFRESH_ENABLED=False`；无真实在线采集 |

M1 本地发布与恢复演练完成；下一步可准备目标部署环境的 M1 变更包和运行检查，环境操作须另行授权。
HK2-05 来源级控制和使用范围核对、HK2-06 三日采集与恢复验收仍待实施，它们继续阻塞 HK2-07-D/HK2-08 M2 在线发布。
本次不开放行情、PE、分红或跨币种换算。

备份数据库与制品目录的一致性仍要求先停止所有写入；工具不自动停止服务。发布检查是显式全量核验，
没有新增常驻完整性监控。旧 ResearchSnapshot 绝对制品路径的搬迁限制沿用备份手册；本轮实际旧行哨兵验证的是
观察池及可相对搬迁的 MaterialStore 资料，不声称改写或迁移旧研究快照的绝对路径。
