# 港股与美股扩展实施计划 · 00 执行约定（所有子计划共用）

> 子计划位于 `docs/product/plans/hk-us-expansion/`。每个子计划（01～14）都是自包含的执行单元，交给一个独立 sub-agent 完成。
> 执行前先读本文件，再读自己的子计划；接口与文件边界见 [`README.md`](README.md) §3、§4。

## 执行环境（与本仓库其他计划不同，务必先读）

主 checkout `/Users/zhvala/SourceCode/zcharlie` 有 200+ 未提交的用户 WIP。**实施一律在独立 worktree 内进行**：

```
集成分支：/Users/zhvala/SourceCode/zcharlie-hkus   （分支 feat/hk-us-expansion）
子任务：  /tmp/zc-hkus-<task>                      （分支 hk-us/<task>，从集成分支 HEAD 建立）
```

- 子 agent 的当前目录是其任务 worktree；所有相对路径都相对该目录。
- **禁止**在主 checkout 里编辑、提交、切分支、stash 或 `git add -A`。
- 只 `git add` 自己子计划「文件清单」里的路径；用 `git add <path> ...` 显式列出，**不使用 `git add -A` / `git add .`**。
- 提交信息用子计划给出的文本，结尾加 `Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>`。
- 不开 PR、不 push、不合并到 main；合并由控制 agent 负责。

## 门禁命令（三条都必须过）

工具用主 checkout 的 venv 解释器，代码用 worktree 的 `PYTHONPATH` 覆盖（editable 安装的 src 会被覆盖）：

```bash
WT=$PWD                                  # 当前任务 worktree
PY=/Users/zhvala/SourceCode/zcharlie/.venv/bin/python
GATE=/tmp/zc-hkus-gate-<task>; mkdir -p "$GATE"

# 1) 定向测试（先跑，快）
PYTHONPATH=$WT/src $PY -m pytest -q tests/unit/test_<你的文件>.py

# 2) 全量测试（提交前必跑）
PYTHONPATH=$WT/src DATA_DIR=$GATE/data DATABASE_URL=sqlite+aiosqlite:///$GATE/gate.db \
  API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 \
  $PY -m pytest -q -m "not network"

# 3) 静态检查
/Users/zhvala/SourceCode/zcharlie/.venv/bin/ruff check .
/Users/zhvala/SourceCode/zcharlie/.venv/bin/mypy src
```

- **`DATA_DIR` / `DATABASE_URL` 必须指向本次任务的临时目录**：否则会命中 dev 库或残留状态，出现约 16 个迁移测试假失败。
- 基线：干净的 `feat/hk-us-expansion` 起点上，全量结果见 [`COMPLETION.md`](COMPLETION.md) 的"基线"一节。**只允许与基线相同的失败数**；出现新失败必须修掉或写进报告。
- ruff：`line-length=100`，规则 `E F I N UP B SIM ASYNC`；mypy strict（`src`）。`research/` 不在 worktree 内，无需排除规则。
- 测试约定：`tests/unit/`（纯离线）、`tests/integration/`（含 `test_api_*.py`、`test_migration_0NN.py`、`test_web_*.py`）；`asyncio_mode=auto`（async 测试不加 mark）；HTTP mock 用 `respx`；时间用 `freezegun`；`tests/conftest.py` 的 autouse `_no_real_akshare` 禁止真实 akshare 调用。
- 每个新迁移配 `tests/integration/test_migration_0NN.py`。
- 新增 JS 文件必须加进 `tests/integration/test_web_client_contract.py` 的清单（任务 11）。

## 数据与证据纪律（违反即视为任务失败）

1. **离线**：默认禁网。只用已保存的原始证据与本任务提交的 fixture；`@pytest.mark.network` 的测试本次不写、不跑。
2. **不可变**：资料版本一旦写入不改写；内容 hash 变化产生新版本；旧数据和旧研究引用不动。
3. **不伪造时间**：`acquired_at` 用真实取得时间；导入旧材料时原抓取时间另存 `source_fetched_at`，**不回填过去**。未知公布时间保留未知，页面展示限制，不塞进 `EvidenceRecord`。
4. **缺失 ≠ 零**：缺失、原始零、冲突、定义不适用四种状态分开表达；同比为空不得填零；不用相似标签猜补。
5. **不静默取有利值**：来源冲突保留冲突状态，不自动选对结论更有利的一边。
6. **不静默改 golden**：`tests/fixtures/research_kernel/**` 一个字节都不许动。
7. **A 股语义不变**：既有 A 股取数、评分、证据、任务流程保持原语义；新增来源走专用路径（`materials/`），不改 `evidence/snapshots.py` 的 A 股分支行为。
8. **文案纪律**：产品文案与 JS 不出现供应商端点名、CIK、运行内部 ID、交易建议或打分措辞（守卫测试会失败）。

## 原始证据在哪（`research/` 未被 git 跟踪，worktree 里没有）

只读引用，绝对路径：`/Users/zhvala/SourceCode/zcharlie/research/market-data-validation/`

| 用途 | 位置 |
| --- | --- |
| 港股原币三表原始响应（汇丰/中电/腾讯，四年） | `evidence/remaining-final-03/raw/native/{00005,00002,00700}/{income,balance,cashflow-original}-four-years.json` |
| 港股现金流端点的发现与失败请求（错误正文案例） | `evidence/remaining-final-03/raw/native/cashflow-discovery/` |
| 美股 SEC 五家 CompanyFacts / Submissions（含 JPM 历史分片） | `evidence/remaining-final-03/raw/us/{AAPL,COST,JPM,XOM,TSM}/{companyfacts,submissions}.json`、`us/JPM/shards/` |
| LNC 原始/修订/不可依赖案例 | `evidence/validated-run-01/raw/sec/{companyfacts,submissions}.json`、`*index.html`（accession `0000059558-23-000009` 原始、`-000019` 修订） |
| 2024 年逐字段对照结果（18 个金额 + 页码） | `evidence/remaining-final-03/derived/native-hk/result.json`、`remaining-validation.md` |
| 汇丰口径与人民币展示乘数 | `research/market-data-validation/remaining-validation.md` §2、`raw/RPT_HKF10_FN_INCOME_PC.json` |
| HKMA 日汇率（八条记录 + 单位说明） | `evidence/remaining-final-03/raw/fx/hkma-2025-correct.json` |
| 分红案例（汇丰 USD 0.36 / HKD 2.791007 确定日） | `evidence/remaining-final-03/raw/dividends/`、`remaining-validation.md` §5 |
| 调研脚本（口径与检查项的权威参考） | `scripts/native_hk_probe.py`、`scripts/directed_us_probe.py`、`scripts/valuation_probe.py`、`scripts/common.py` |

**提交进仓库的 fixture 必须是裁剪并脱敏的小样本**（单公司、单年度、必要字段），不整包复制：`remaining-final-03` 单文件可达 8 MB。裁剪时保留字段结构、单位、币种、期间与 `pageNumber/pageSize` 等结构与边界字段。

## 子 agent 返回格式（控制 agent 依赖它写 COMPLETION.md）

```
任务：<NN> <标题>
分支/提交：hk-us/<task> @ <sha>
门禁：定向 <n> passed；全量 <n> passed / <n> failed（对比基线：无新增）；ruff OK；mypy OK
改动：<新增/修改文件清单，与子计划文件清单的差异要说明>
实测事实：<关键行为与数字，含 文件:行号>
与计划的偏差：<逐条，附理由>
未决/风险：<需要控制 agent 或人工决定的事>
```

返回文本要具体到可复核（命令、文件:行号、失败输出），不要写"已完成"这类无内容结论。
