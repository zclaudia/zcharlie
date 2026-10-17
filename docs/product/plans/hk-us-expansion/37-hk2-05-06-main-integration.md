# HK2-05 / HK2-06 准备阶段合入 main 联合验收

日期：2026-10-17（Asia/Shanghai）。本轮只合并已提交代码，不启用来源，不操作生产数据库或部署。

## 基线和合并范围

- 最终本地 main 基线：`18640973d493eb3173c989f19351480fc626aa71`，含银行年度在线采集和原件复核闭环。最初基线为 `b75d5f28`；完整首轮验收期间 main 新增银行提交，因此重新建立最新 main 上的合并树并补验。
- 主题分支 `codex/hkus-source-policy`：`02daa6d`（HK2-05 来源策略）及 `30e8203`（HK2-06 准备工具与计划）。
- 集成分支 `codex/hkus-main-integration-20261017` 从上述 main 建立，在已有独立工作树
  `/Users/zhvala/.codex/worktrees/hkus-source-policy/zcharlie` 合并主题分支；自动合并无冲突。
  原主题分支保持 `30e8203`。先以未提交的合并树执行门禁，再提交双父合并并将本地 main 快进至该结果。
- main 两笔银行提交涉及的 35 个路径与最新 main 逐项一致；HK 来源策略、fetcher/worker 门禁、probe 与 grader
  与已验收主题分支逐字节一致。没有复制当前 P4 未提交代码到集成树或合并提交。

历史阶段报告 [32](32-hk2-05-offline-acceptance.md)、[36](36-hk2-06-preparation-acceptance.md)
保留当时“未合入 main”的状态；本记录追加本轮合并验收。

## 联合门禁

所有命令在上述独立工作树运行，借用既有 `.venv`，以 `PYTHONPATH=<独立树>/src` 加载代码；
使用虚构测试 API_KEY、忽略目录 `data/hk20506-main-acceptance/*-runtime/` 下的 scratch 数据库与 DATA_DIR。
默认排除 network 测试，不加载生产配置、不开在线刷新、不执行生产迁移或导入。

| 检查 | 最终结果 / 日志 |
| --- | --- |
| 初始 b75d5f28 全量 unit | 2593 passed / 3 skipped / 6 deselected，359.40 秒；`/tmp/hk20506-main-unit.log` |
| 最终 1864097 全量 unit | 2642 passed / 2 skipped / 6 deselected，311.03 秒；`/tmp/hk20506-latest-main-unit.log` |
| 初始 b75d5f28 全量 integration | 808 passed / 12 skipped，498.06 秒；`/tmp/hk20506-main-integration.log` |
| 最终 1864097 银行 / CLI / 港美相关 integration | 148 passed，418.37 秒；`/tmp/hk20506-latest-main-target.log` |
| 其余 tests 目录 | 717 passed；`/tmp/hk20506-main-other.log` |
| Ruff | `src tests` 和两个 HK2-06 CLI 全部通过；最终日志 `/tmp/hk20506-latest-main-ruff.log` |
| mypy | 最终树 440 个源文件通过；`/tmp/hk20506-latest-main-mypy.log`（初始树 435 个源文件亦通过） |
| Node 语法 | `financials.js`、`settings.js` 通过 |
| 合并 diff | 提交前 `git diff --cached --check` 通过 |

各轮存在重叠，数字不相加为唯一用例总量。跳过和排除不记为通过，也不把模拟测试结果计入真实三日观测。最终更新只增加银行模块和 CLI 接线，港美产品代码与初始已测版本一致；对最新树重新执行完整 unit，集成补验覆盖全部银行、材料 CLI、来源策略、API/页面和 HK2-06 路径，未重复无关的完整集成组。最终两个 unit 跳过项为本工作树未包含的 Costco / HSBC 完整原件。

为复验最新银行原件闭环，仅从主 checkout 只读复制并逐文件核验 16 份已有 JSON/PDF 到本工作树忽略目录，不复制数据库、不请求网络；清单为 `data/hk20506-main-acceptance/latest-main-bank-evidence-copy.json`。

## 关闭状态与来源边界

对合并代码单独执行了带 socket/DNS 及 `.env` 读取拦截的真实 CLI 默认预检：
SEC 仅列计划六家，东财仅列计划三家；实际 `company_keys=[]`，两源请求均为 0。
全局 `ONLINE_REFRESH_ENABLED=False`，Settings 的 SEC、HK 运维开关均默认 false。
source policy 的 SHA 与 `30e8203` 一致，仍为许可 unknown / 技术 not_accepted；
独立数据库 sentinel 字节不变，没有打开既有数据库。
证据：本工作树 `data/hk20506-main-acceptance/closed-preflight/proof.json`。

两源真实观测仍各 **0/3**。HK2-05 已实现默认关闭的运行门禁；HK2-06 只交付准备阶段，
来源范围与请求差距见 [34](34-hk2-06-source-review.md)，条件式三日计划见 [35](35-hk2-06-observation-plan.md)。
本次合并不代表 SEC 或东财的真实验收通过，也不满足 HK2-07 在线部分或 HK2-08 M2 的启用条件。

## 主 checkout 的 P4 保留

主 checkout 保持 main，原 P4 与银行后续未提交文件不进入本次提交。与本轮重叠的四个文件为
`config.py`、`settings/registry.py`、`web/static/settings.js`、`web/templates/stock.html`。
先在忽略目录保存原文件与 base/incoming/merged 三方结果，预检均无冲突；Python AST、Node 和模板语法单独核验。
其余已修改及未跟踪路径只记录哈希，不改写。

推进 main 前重新读取当前改动，不能用较早快照覆盖后来编辑。保留完整 working-tree patch、原 index、
四个重叠文件原件及逐路径前后哈希；持有 index 锁，检查 index 无暂存工作，并以旧 main SHA 为条件更新引用。
只更新合并涉及的路径，四个共享文件写入干净的三方结果；未重叠的用户文件不由本轮合并写入，未跟踪文件不得被覆盖。
主 checkout 有并行编辑：最初严格哈希检查发现其它 P4 测试文件变化时安全回退，main 未推进。
重新取最新快照后，对本轮不写入路径的外部编辑只记录前后哈希，不回滚这些新编辑；四个共享文件仍须在写入前保持快照一致。
不使用 stash/reset，不暂时移除整套 P4 工作；main HEAD/索引更新后用户改动仍未暂存。

此 Git 保留流程先在临时仓库完成成功及引用更新失败回退演练，验证未提交/未跟踪内容保留、
索引锁清理和旧 main 不被意外推进。实际推进证明保存在本工作树
`data/hk20506-main-acceptance/main-preservation/proof.json` 及该目录下的前后哈希、patch 和原件。
该本地备份目录为私有权限且忽略，不进入 Git。

本轮门禁验证的是提交后的干净代码树。主 checkout 继续保有 P4 未提交工作，
不能把本轮结果宣称为 P4 整体发布验收。没有 push、归档用户工作树或操作生产环境。
