# HK2-04 三项 P0 收口及 HK2-07 开工评估

> 后续更新：本报告保留 HK2-04 收口时的条件判断；HK2-07 离线 A–C 已按 [26](26-hk2-07-offline-execution.md) 完成，实测结果见 [27](27-hk2-07-offline-acceptance.md)。

日期：2026-10-15；分支 `codex/hkus-archive`，起点 `19cc4ea`。
执行拆分见 [22](22-hk2-04-closeout-plan.md)。三个 GPT-6 Luna 子代理并行完成专项实现，
主代理复核官方原件、集成生成器与验收器、检查负例和全量回归后统一提交。
此前 [20](20-latest-annual-evidence.md)、[21](21-hk2-03-04-acceptance.md) 保留当时的 partial 结论。

## 三项收口

| 缺口 | 本次处理 | 保留的限制与负例 |
| --- | --- | --- |
| 汇丰 IFRS 17 期初权益 | 2023 官方年报物理页 424、427、135 对照；`189015 − 22414 = 166601` 百万美元；五项过渡调整合计 `−9235`。原 IFRS 4 普通股权益 `175836` 及原始来源完整保留 | 166601 是有方法版本、十项来源输入、确定性哈希的派生结果，未声称原文直接披露。核对每项页码、列、金额、单位、日期及发布依据；错误引用、篡改哈希或未验证解决标记仍拒绝。银行 ROE 仍不适用 |
| Apple 利润标签 | ROE 接纳明确为 US GAAP、归母或普通股权益范围的 `net_profit`；原始 `Net income`、指标名、accession 和精确事实引用不改 | 未知/含 NCI 范围不接纳；同范围别名不一致或不可比时拒绝；币种、银行、ADS 与 CFO/利润原有约束继续生效。真实保存、发布准备及 PublicationService.check 链路通过 |
| Costco 权益范围 | 2023 10-K 明确披露归母权益：2022-08-28 为 20642、2023-09-03 为 25058 百万美元，替换计算所选范围 | 旧 TOTAL EQUITY 20647/25058 及 NCI 5/0 保留，逐期验证归母加 NCI 等于总权益；2023 原文破折号与 `ixt:fixed-zero` 保留。不同 scope 即使同值也不混用 |

来源详情及原件哈希分别见 [23](23-hsbc-restatement-evidence.md)、[24](24-costco-equity-evidence.md)。
本次只关闭这些特定、版本化的复核差异，不建立可自动解释任意公司重述的通用会计规则。
ROE 选择规则变化使用新计算方法版本 `material.metrics.v2`；新旧方法版本进入不同计算哈希，
不会覆盖已有 v1 记录。原始事实契约及来源绑定保持兼容。

## 独立验收

原件包断网重验与重建实测通过：

- CLI `ia materials verify-annual --manifest config/research_material/annual_validation_2026-10-15.json --source-root data/hk2-latest-evidence/2026-10-15/raw --as-of 2026-10-15`：socket 连接被禁止，退出 **0**，网络连接尝试 **0**，八家全部 `complete`，无 issues。
- **48/48** 最新六类字段证据、**8/8** 连续四财年及期初权益、**79/79** SEC 索引引用分片、**156/156** 所列原件哈希与大小核对通过。银行、ADS、不可比资本开支的 N/A 保留；完整证据不表示六项指标均有数值或全部历史三表字段已逐项核对。
- 清单生成器再次断网执行，前后 SHA-256 同为 `2b75bd0c612b174a1a82d8cd23f32627309e2b6235d6b0fa9e8e538fca7b132a`。
- 汇丰计算引用 `sha256:3ced29e2f3aef6293741ff5f6f1e3ddca24bb28c976f917cf886a8798a4c7dea`，十项输入及旧事实保留；Costco 复核引用 `sha256:1d50e17f4bce47aa524dafa6280469829b3dd6b06e5d9dcd137b1df7790e3bc3`。
- 方法升级回归验证 v1 计算仍可解析、v2 使用独立回执、重复 v2 准备不新增计算。

机器报告为忽略制品 `data/hk2-latest-evidence/2026-10-15/closeout-cli-report.json`，
CLI 元数据为同目录 `closeout-cli-meta.json`。大体积原件只保留在工作树 data，不随 Git 提交；
其它 checkout 复验必须复制完整 raw 根目录，只有清单不能证明本地原件存在。

最终全量离线门禁按互不重叠的三组执行，合计 **3441 passed / 24 skipped / 6 deselected**，无失败：

| 范围 | 结果 | 日志 |
| --- | --- | --- |
| `pytest -q tests/unit` | 2105 passed / 16 skipped / 6 deselected | `/tmp/hk204-final-unit.log` |
| `pytest -q tests/integration` | 619 passed / 8 skipped | `/tmp/hk204-final-integration.log` |
| `pytest -q tests --ignore=tests/unit --ignore=tests/integration` | 717 passed | `/tmp/hk204-final-other-key.log` |
| `ruff check src tests tools/build_hk_us_annual_validation_manifest.py tools/review_hsbc_restatement.py` | 通过 | `/tmp/hk204-final-ruff.log` |
| `mypy src` | 377 个源文件通过 | `/tmp/hk204-final-mypy.log` |
| `git diff --cached --check` | 通过 | 提交前执行 |

各组 `PYTHONPATH=$PWD/src`，使用主 checkout 的 `.venv`，DATA_DIR、DATABASE_URL、pytest
临时目录分别隔离在 `/tmp/hk204-final-*`。首次其它测试组因未设置必填测试 API_KEY 导致五个
既有 worker/工作台测试失败；使用假值 `API_KEY=hk204-offline-test-only` 重跑完整该组后 717 项通过，
没有修改应用设置或测试断言来规避失败。24 个跳过项按既有依赖条件登记，6 项联网测试排除。

复核中修正了官网索引 PDF 链接与下载地址的 `download=1` 差异：保存两个真实地址，只允许
这一份已核对汇丰报告的下载参数匹配，其它查询或路径差异仍拒绝。另将 Costco 冲突解决引用
绑定到公司和实际权益日期，并保留原冲突描述，避免有效复核哈希被用于无关期间。

## HK2-07 条件与顺序

**具备推进离线实现的条件；完整浏览器验收与发布条件尚未满足。**
年度验证清单是证据索引，`verify-annual` 不把复核事实自动写入 MaterialStore；
现有 bootstrap/offline_import 通过供应商适配器导入原始响应，未消费人工复核选择与重述计算链。
因此必须先补资料接线，不能直接用验收 JSON 截图代替实际产品状态。

| 子任务 | 优先级 / 依赖 | 验收标准 |
| --- | --- | --- |
| HK2-07-A 复核事实离线导入 | P0；依赖 03、04 | 临时库断网导入八家；逐事实保留原指标、日期、来源、原始与替代版本；派生计算保存独立输入和方法引用，不能伪装为直接来源。重复导入不新增相同版本，未通过记录保持部分状态 |
| HK2-07-B 状态与时间契约 | P0；依赖 A，在线状态预留 05/06 | API 分开表达覆盖、可计算性、冲突、原件复核及来源启停；最近尝试/最近成功/最近原件核对分别记录。入队、来源关闭和失败不能更新最近成功；新鲜度来自实际披露核对日，不能用固定 366 天推断最新 |
| HK2-07-C 页面及离线浏览器矩阵 | P0；依赖 A、B | 八家公司身份、原币和实际财年；桌面/移动；空、部分、冲突、过期、待核对、关闭、失败状态；保留截图与交互证据；GET 无写入或网络，指标可回溯到所选精确事实版本 |
| HK2-07-D 在线生命周期验收 | P1；依赖 05、06、B、C | 来源使用范围、技术验收、运维启停分别控制；三日实际观测及失败/恢复/关闭演练通过后，逐来源开放真实刷新。当前全局关闭继续生效 |

代码依据：`materials/context.py` 的 `financial_context`、`_capabilities`、`_statuses`；
`api/schemas/research_materials.py`；`web/templates/stock.html`、`web/static/financials.js`；
`materials/bootstrap.py`、`offline_import.py`、`refresh.py`、`handlers.py`。
当前页面与 API 已有读取基础，但缺分离的三类时间及完整运行状态；现有 Web 测试不替代八家双视口浏览器实测。
HK2-05/06 不阻塞 A–C 开工；它们继续阻塞 D 和在线发布。
