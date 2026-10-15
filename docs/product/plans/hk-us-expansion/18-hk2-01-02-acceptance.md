# 港美股能力矩阵与原件归档验收记录

日期：2026-10-15。范围：HK2-01、HK2-02。实施计划见 [16-hk2-01-02-execution.md](16-hk2-01-02-execution.md)。

## 实施状态

| 子任务 | 状态 | 验收内容 |
| --- | --- | --- |
| S1 能力与来源矩阵 | 通过 | 八家实际来源绑定、六项能力当前条件、逐来源未知项和启用门槛，校正文档中的 PE 与分红产品接线表述 |
| S2 归档底座与迁移 | 通过 | 原件包、不可变采集记录、资料关联、完整性检查与迁移 049 |
| S3 离线导入与重放 | 通过 | 实际导入归档、拒绝响应留存、CLI、迁移目录后的离线重放 |
| S4 采集任务与旧资料提示 | 通过 | 部分失败留原件、无新资料采集记录、旧资料缺口、在线默认关闭 |
| S5 主代理最终验收 | 通过 | 全量回归、静态检查、备份恢复和独立重放 |

实现由 GPT-6 Luna 子代理承担，主代理负责契约设计、跨模块集成与独立验收。S1 验收后开展归档实现；S2 契约确定后，S3 和 S4 按文件所有权独立推进。

## 开工基线

- 代码起点：`f165f25132131633808b619dec2297eaf2bc51d7`，分支 `codex/hkus-archive`，独立工作树。
- 主 checkout 原有待提交项：上一轮的下一阶段计划及其文档入口，原地保留；实施工作树复制作为计划依据。
- `pytest -q -m "not network"`：3280 passed、24 skipped、6 deselected，563.47 秒。跳过项依赖该工作树未携带的调研原件或可选构建制品；本次没有通过复制外部原件改变基线。
- `ruff check .`：通过。`mypy src`：通过，368 个源文件。
- 原始基线测试日志：`/tmp/zc-hk2-baseline-VGeoeA/pytest.log`，为本机临时验证记录。

## 交付物与验收映射

| 要求 | 实现 | 可复现检查 |
| --- | --- | --- |
| 八家来源与六项能力分层 | `17-capability-source-matrix.md`，README 与产品说明同步 | 对照 `pilot_list.yaml`、`context.py`、`valuation.py`、`dividends.py` 与当前股票页逐项审查 |
| 多响应正文、清单与采集记录 | `materials/archive.py`、`storage/models/material_acquisition.py` | `tests/unit/test_material_archive.py` 覆盖多响应、哈希/字节、未知时间、路径和链接边界、脱敏、写盘失败与幂等 |
| 独立记录没有产生新资料的采集 | 采集记录按稳定证据清单去重，资料版本保持原语义 | `test_material_handler_archive.py` 覆盖无草稿、重复响应、新抓取时间与部分异常 |
| 拒绝正文留存且不可重新采信 | 清单逐响应保存 `parse_eligible`，HTTP 非成功与边车校验失败保留正文但不进入事实 | `test_material_archive_replay.py` 的哈希/字节数拒绝案例，既有导入测试的七种 HTTP 拒绝案例 |
| 独立离线重放 | `materials/replay.py`、`ia materials replay --acquisition-id ID` | `test_material_archive_replay.py`，复制数据库与归档后移除输入、拒绝 HTTP，比较真实港股与 SEC 分片的事实及指标 |
| 备份工具覆盖及迁移恢复 | `tools/backup_research.py` 同时复制 `research-material/`，嵌套清单纳入校验 | 主代理独立增加 `test_material_archive_restore_acceptance.py`：生产备份工具 → 新目录 → 删除原输入、原数据及备份副本 → 禁止同步/异步 HTTP → 重放结果与版本数量一致 |
| 不可信或漂移的重放原子拒绝 | 可信数据库哈希、身份与适配器校验，重放写入及链接检查使用保存点 | 主代理增加 `test_material_replay_rejections.py`，验证身份/版本/清单拒绝，以及调用方捕获解析漂移异常后仍无新财务行 |
| 升级旧库与不可变保护 | 迁移 049 只新增表，SQL/ORM 禁止修改、删除、替换 | `test_migration_049.py`，从 048 升级并逐行比较旧资料、检查新表与模型一致性及降级 |
| 旧资料可读且缺口可见 | 财务读模型查询数据库关联，增加 `raw_archive_missing` 状态及中文页面标签 | `test_material_archive_context.py`；原有财务 API、页面和任务用例继续验证能力位与默认离线行为 |

新归档位于 `DATA_DIR/research-material/archive/<source>/`，正文和清单分别按 SHA-256 寻址。
`research_material_acquisition` 独立关联资料的 id、revision 和 content_hash；SEC 财务采集允许关联同组的申报索引。
清单身份包含实际受控绑定及原始请求窗口；普通导入的空窗口保留适配器默认窗口语义，适配器版本变动时重放拒绝。

## 集成审查修正

- 增加真实资料行的公司、来源组、适配器版本、revision 和内容哈希校验，不能只保存调用方提供的 ID。
- 校验清单时使用数据库保存的可信哈希；清单和正文都不接受静默覆盖，缺失与损坏分别拒绝。
- 增加逐响应解析资格，避免边车校验失败的 HTTP 200 正文在重放中被重新接受；旧材料未知 HTTP 状态 0 仍按原有语义处理。
- 失败前记录整批预期请求，保留尚未收到的请求缺口；异常携带已收到的正文，在重试前归档并检查点提交。
- 保留未知原抓取时间，修复 SQLite 读回时间失去时区后的重放处理；重放运行时间不回填成原抓取时间。
- 解析结果与可信资料关联不一致时回滚保存点，防止捕获错误后误提交新财务行。
- 原有两阶段测试假适配器曾声明一种版本、返回另一种版本，已修正假实现与声明一致；没有放宽生产校验。旧 HTTP 拒绝测试改为校验正文已归档且不可解析、无财务资料，而非假定数据目录不存在。
- 备份工具原先只复制研究运行与通知目录，现加入资料原件；修复嵌套 `manifest.json` 被排除在完整性清单之外的问题。

## 最终门禁与交付状态

主代理独立执行，所有数据库与原件演练均使用临时目录：

| 检查 | 结果 |
| --- | --- |
| 全量离线 `pytest -q -m "not network"` | **3308 passed、24 skipped、6 deselected，零失败，732.86 秒**；相对基线增加 28 项通过，跳过与联网排除数量相同 |
| `ruff check .` | 全部通过 |
| `mypy src` | 通过，371 个源文件 |
| 重放、拒绝路径、备份恢复及既有离线导入联合定向检查 | 24 passed，6.03 秒 |
| `git diff --check` 与计划本地链接检查 | 通过 |
| A 股 golden、既有资料契约与存储版本规则、依赖锁文件 | 均无改动；在线开关仍为 False |

全量日志：`/tmp/zc-hk2-final-RdIemF/pytest.log`。跳过项仍是工作树缺少调研原件或可选构建制品的原有条件，不计作本轮新增覆盖。新的归档和重放用例均依赖仓库内 fixture，没有依赖用户调研目录或真实网络。

复现全量命令时，从本工作树运行主 checkout 的 `.venv/bin/python`，设置 `PYTHONPATH=$PWD/src`，并将 `DATA_DIR` 与 `DATABASE_URL` 指向全新临时目录；使用测试用 API key，再运行上述 pytest、ruff 和 mypy 命令。

交付在独立分支 `codex/hkus-archive`。本轮只完成代码、文档与临时环境验收，未合回 main，未对应用数据库执行 049 迁移、未导入应用数据、未启用任何在线来源。主 checkout 的原有未提交文档保留。

## 操作入口

使用同一套 `DATABASE_URL` 和 `DATA_DIR`：

以下命令须使用包含本次提交的代码环境。独立工作树验证时，使用主 checkout 的虚拟环境，
并设置 `PYTHONPATH=<本工作树>/src`；尚未合回 main 时，不能把主目录旧代码的 CLI 结果当成本次版本结果。

```bash
ia migrate up
ia materials init
ia materials import --source-dir /path/to/saved-evidence --company-key jpmorgan-chase
ia materials replay --acquisition-id 1
```

从导入 JSON 的 `acquisition_ids` 获取真实 ID，不依赖示例中的 1。`replay` 退出码 0 表示完整通过、2 表示原资料仍有缺口、1 表示完整性/版本/身份等拒绝。旧调研目录中的真实缺片不会由归档补齐。

## 保留边界

本次交付不包含最新年度联网补验、在线来源启用、指标发布自动接线或行情估值分红产品接线。没有原件包的历史资料不补造正文、不改写旧版本。默认在线刷新仍关闭。

新采集记录和归档格式用于保存取得的输入及重放，不构成严格历史时点查询或全量公告发现能力。来源使用范围未知项继续按 HK2-01 矩阵保留。
