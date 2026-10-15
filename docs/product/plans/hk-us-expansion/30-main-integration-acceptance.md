# 港美扩展合入 main 的联合验收

日期：2026-10-16（Asia/Shanghai）。合并父版本为本地 main `c452a51` 和
`codex/hkus-archive` 的 M1 收口提交 `4860987`。远端 origin 已 fetch，远端 main 为本地 main 的祖先；
本次在现有最新本地 main 上合并，不回退银行研究或部署运维改动。

Git 自动合并成功，无冲突。复核 `web/routes.py` 同时保留行业页面及港美财务状态/时间展示，
备份手册同时保留 ai-server 现有运维说明和新的离线 M1 备份恢复流程。没有执行部署脚本、生产迁移或数据导入。

主 checkout 原有两份规划草稿先复制并保存到独立 stash，逐文件校验哈希一致；
其规划入口和内容已有分支中的新版记录，未用旧草稿覆盖当前实施进度。浏览器临时制品保留，未加入提交。

## 联合门禁

所有测试使用 main 工作区源码、既有 `.venv`、隔离的 `/tmp/hkus-main-merge-*` 数据库与 DATA_DIR；
测试 API_KEY 使用假值。没有连接生产库。

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| `pytest -q tests/unit` | 2270 passed / 2 skipped / 6 deselected | `/tmp/hkus-main-merge-unit.log` |
| `pytest -q tests/integration` 首轮 | 647 passed / 4 skipped / 1 failed；失败定位与补验见下 | `/tmp/hkus-main-merge-integration.log` |
| 失败用例及 049 迁移补验 | 4 passed；含首次失败的完整黄金链路及三项迁移测试 | `/tmp/hkus-main-target-fixed.log` |
| 其余 tests | 717 passed | `/tmp/hkus-main-merge-other.log` |
| Ruff | 通过 | `/tmp/hkus-main-merge-ruff.log` |
| `mypy src` | 388 个源文件通过 | `/tmp/hkus-main-merge-mypy.log` |
| `git diff --cached --check` | 通过 | 合并提交前执行 |

联合回归发现旧的 `test_golden_chain_migration_to_agent_tool_reads_one_batch` 把迁移 head 写死为 048，
而港美归档新增了 049。单独复现同一断言后，将测试改为严格比较数据库实际版本与 Alembic 当前 head，
仍保留同行表可读、19 家原件导入、黄金统计、API 和 agent 引用的完整后续断言；未修改应用逻辑或黄金数值。
该用例与三项 049 迁移测试修复后共 4 passed，退出 0。没有重复执行已通过的其余集成用例；
合计 648 个唯一集成用例通过，全部三组共 **3635 个唯一用例通过 / 6 skipped / 6 deselected**。
两项单元跳过因本 checkout 未包含港美原始抓取制品，四项集成跳过因未配置网关构建目录；
6 项联网测试按离线配置排除。

合并后的代码另外对 HK2-08 的实际恢复包运行断网只读 `--require-m1` 检查，退出 0：
8 家有效复核资料，40 个当前复核版本支持的计算，迁移 head 049，在线刷新为 false。
证据见 `/tmp/hkus-main-merge-release.json`，网络拒绝日志不存在或为空。

## 合并与发布状态

港美 HK2-01～04、HK2-07 离线及 HK2-08 M1 代码合入本地 main；之前阶段记录
[29](29-hk2-08-m1-acceptance.md) 保留当时“未合入 main”的历史状态，本记录追加当前状态。
在线刷新仍为 `ONLINE_REFRESH_ENABLED=False`，生产数据库、部署和远端 push 均未操作。
HK2-05/06 仍阻塞在线 M2，行情、估值和分红能力没有因合并开启。
