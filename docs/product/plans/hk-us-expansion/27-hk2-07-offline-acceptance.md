# HK2-07 离线集成验收

验收日期：2026-10-16（Asia/Shanghai）；证据核对截至日仍为 2026-10-15。
起点 `8a6be5d`，独立工作树 `codex/hkus-archive`。按 [26](26-hk2-07-offline-execution.md)
拆分，由三个 GPT-6 Luna 子代理并行实现导入、状态契约、页面；主代理按导入 → 状态 → 浏览器顺序集成、复核及提交。

**HK2-07 A–C 离线部分通过。** 真实资料仅导入临时验收库；生产库、main、部署和在线刷新没有变更。
HK2-07-D 仍依赖 HK2-05/06 的来源控制、使用范围核对及至少三个不同日期的实际观测。

## 实现与契约

- 新增 `ia materials import-reviewed`。先验证完整受控原件包，再按身份写入 `issuer.reviewed_annual.v1` 年度资料；支持选择公司，但不缩小原件包验证范围。全局验证失败拒绝全部导入，公司局部失败独立返回，CLI 部分结果退出 2。
- 一家公司一份四年度资料，保留唯一期初权益、原始事实、经济含义相同的重复原件、旧值和复核解决链。汇丰派生值保留计算输入哈希、方法与独立引用，页面标为“复核计算”，不建立虚构原文摘录。
- 原件复制到 DATA_DIR 的内容哈希路径；复制时再次验哈希。相同原件不覆盖，既存损坏原件拒绝复用。重复资料返回原版本。复核索引是本地证据清单，不伪造上游响应采集记录。
- 页面与发布准备共用版本选择：有效复核资料优先覆盖其年度窗口内旧供应商/旧复核版本；更晚的未核对资料保留并使最新性变为过期。旧版本仍可由精确引用读取。
- API 增加 `freshness`、`refresh`、`metric_coverage` 和事实 `evidence_kind`。请求、实际尝试、真实采集成功、离线导入、原件核对分别取证；时刻带 UTC，核对日保留日期。排队、关闭来源和失败均不冒充成功。
- 最新性仅声明核对截至日；取消“财年末超过 366 天即过期”的推断。资料覆盖、事实复核、指标可计算性和来源启停分别展示。不适用及缺失不补零，银行和 ADS 的既有限制继续生效。
- 刷新按钮展示排队与运行结果，并进行有限次数的只读状态查询；普通财务 GET 不入队、不写资料、不请求上游。在线开关仍为 `ONLINE_REFRESH_ENABLED=False`。

## 实际原件与持久性

沿用清单 `config/research_material/annual_validation_2026-10-15.json`，SHA-256：
`2b75bd0c612b174a1a82d8cd23f32627309e2b6235d6b0fa9e8e538fca7b132a`。
原件根目录为 `data/hk2-latest-evidence/2026-10-15/raw`。

临时库从空库迁移到 head 049，初始化受控身份后执行实际 CLI；socket 连接被禁止。

```sh
ia materials import-reviewed \
  --manifest config/research_material/annual_validation_2026-10-15.json \
  --source-root data/hk2-latest-evidence/2026-10-15/raw
```

| 检查 | 实测结果 |
| --- | --- |
| 首次 CLI 导入 | exit 0；八家 complete；新增 8，复用 0；网络尝试 0 |
| 重复 CLI 导入 | exit 0；新增 0，复用 8；154 个唯一原件的修改时间不变；网络尝试 0 |
| 原件持久化 | 156 个清单文档对应 154 个唯一哈希；重启复验全部存在且哈希一致 |
| 发布准备 | 八家共保存 40 个合格指标计算；无歧义来源；不适用指标继续拒绝产生数值 |
| 重启及精确引用 | 新进程断网读取 40 个计算、116 次事实引用，全部解析到原保存版本 |
| 只读契约 | 集成测试导入八家后移走调研原件目录，断网读取 API 仍成功；两次导入仍为 8 份资料、0 个运行任务 |

实际导入时刻为 `2026-10-15T15:48:58.018262Z`，原件核对日为 `2026-10-15`；二者不互相替代。
实际资料仅证明核对截至日的年度范围，不声明 10 月 16 日仍无新披露。

## 浏览器与刷新交互

运行正常应用路由，外部 socket 连接被禁止，仅允许本机浏览器访问。使用本地 Chromium、正常表单登录；
没有产品调试路由。真实八家公司使用实际导入库；状态案例单独使用 `states.db`，合成案例不作为实际发行人披露证据。

桌面 1440×1000、移动 390×844。工具 `tools/hk207_browser_acceptance.py` 保存完整页面截图、
身份、币种、期间、全部可见事实金额、状态、日期、指标和浏览器错误/请求记录。

| 矩阵 | 范围 | 结果 |
| --- | --- | --- |
| 真实资料 | 中电、汇丰、腾讯、Apple、Costco、JPM、Exxon、TSM；各两个视口 | 16/16 通过 |
| 独立状态 | 空、部分、冲突、过期、来源关闭、失败、待原件核对、未知日期、未覆盖；各两个视口 | 18/18 通过 |
| API/页面对照 | 每家公司桌面/移动身份与币种、实际财年、全部可见事实的指标/期间/原币金额一致 | 通过；不适用空金额保持空值 |
| 布局与运行 | 页面无横向溢出，长表格内部滚动；外部请求、JS 异常、控制台错误、网络失败、HTTP 错误 | 全部为 0 |
| 主代理人工复核 | 汇丰桌面、Apple 移动、移动来源冲突及刷新任务展开状态 | 文案、金额与日期可读；来源关闭与许可未知清楚可见 |

复现入口（先配置临时库、DATA_DIR 和验收专用 API_KEY）：

```sh
python tools/hk207_offline_server.py --port 8767 --denied-log data/hk207-offline-acceptance/server-denied.jsonl
HK207_API_KEY=... python tools/hk207_browser_acceptance.py \
  --base-url http://127.0.0.1:8767 --require-imported \
  --output-dir data/hk207-offline-acceptance/browser-actual
```

`tools/hk207_state_fixtures.py` 从实际验收库只读复制到独立目标库，再通过正常存储接口建立九类状态；
状态浏览器使用 `--only-cases --case 标签=/app/stocks/代码`，不污染实际原件库。

主代理实际点击 Apple 刷新按钮，页面显示“排队中”，不声称已更新。随后仅启动财务采集 handler 的实际 worker，
注入拒绝 fetch 的 fetcher 并禁止 socket 连接。任务从 queued 到 partial，各来源记录 `online_refresh_disabled`；
资料数保持 8，响应采集归档数保持 0。`last_requested_at`、`last_attempt_at` 有实际记录，
`last_success_at` 仍为空，核对日和导入时间不变。重载页面展开任务记录可见部分完成及开始/结束时间。

制品保存在忽略目录 `data/hk207-offline-acceptance/`：`import-first.json`、`import-repeat.json`、
`contexts.json`、`calculations.json`、`restart-check.json`、`refresh-worker.json`、
`refresh-after-context.json`、`browser-comparison.json`、两套 `browser-*/report.json`、34 张矩阵截图及 `manual/`。
服务端外部连接拒绝日志不存在或为空，表示没有外部连接尝试。临时服务验收后关闭。

## 回归门禁

使用主 checkout 的 `.venv`，`PYTHONPATH=$PWD/src`；各组 API_KEY 使用验收假值，DATA_DIR、DATABASE_URL
和 pytest 临时目录分别隔离。本次最终版本全组均退出 0。

| 命令 | 结果 | 日志 |
| --- | --- | --- |
| `pytest -q tests/unit` | 2120 passed / 16 skipped / 6 deselected | `/tmp/hk207-unit.log` |
| `pytest -q tests/integration` | 630 passed / 8 skipped | `/tmp/hk207-integration.log` |
| `pytest -q tests --ignore=tests/unit --ignore=tests/integration` | 717 passed | `/tmp/hk207-other.log` |
| 合计 | **3467 passed / 24 skipped / 6 deselected** | 日志已复制到验收制品目录 |
| `ruff check src tests tools/hk207_browser_acceptance.py tools/hk207_state_fixtures.py tools/hk207_offline_server.py` | 通过 | `/tmp/hk207-final-ruff.log` |
| `mypy src` | 378 个源文件通过 | `/tmp/hk207-final-mypy.log` |
| `git diff --cached --check` | 通过 | 提交前执行 |

24 个跳过项沿用既有依赖条件，包括缺少 run-02 行业原件；6 个联网测试按离线配置排除。
新增测试覆盖导入拒绝、身份绑定、幂等、旧/替代事实、派生计算、复核回执验证、时间语义、版本优先及跨会话发布引用。

## 下一步与边界

1. 推进 HK2-08 的 M1 本地发布演练：旧库备份、升级、数据库与原件配套恢复、目标环境导入及重启读取；本次临时库验收不能代替生产环境就绪。
2. 单独推进 HK2-05 来源级策略与关闭/恢复控制，先核对实际来源使用范围；再实施 HK2-06 的多日采集验收。
3. 05/06 通过后才能推进 HK2-07-D；当前不开放在线刷新，也不开放行情、估值和分红能力。

已知限制：GET 验证复核回执和引用结构，不逐次重哈希大型原件；导入后若外部删除/损坏持久原件，
不会自动撤销复核截至日。本次重启已全量验哈希；后续 HK2-08 恢复/完整性检查应补持久原件的定期校验与告警。
保存失败可能留下未引用的内容寻址原件文件，不会留下该公司的错误事实行。
