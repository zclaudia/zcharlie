# 卡 09:`ia methods` CLI

设计依据:§5、§9。前置:卡 01–04。

## 新文件 `src/investment_agent/cli/commands/methods.py`

Typer app,完全照 `cli/commands/rules.py` 的结构(`_run`/`run_inline` bootstrap、rich 输出、
`--project-dir` 处理方式照 rules 的目录参数):

| 命令 | 行为 |
|---|---|
| `validate <file>` | `load_package_file`,全部错误一次打印(exit 1);合法打印 package_key/content_hash |
| `sync [--dir config/methods]` | 逐文件校验并 `save_package(source="repo-sync")`,报告「已存在(幂等)/新版本 vN」;**绝不移动任何 head**;任一文件非法则整体失败不入库 |
| `list` | 表格:package_key、version、content_hash 前 8、来源、created_at |
| `show <key@version \| key>` | 打印 canonical definition(YAML dump,照 rules show 的风格) |
| `status [--channel company-research]` | 全局默认 head(channel、revision、binding 概要)与 per-listing 覆盖列表 |
| `activate-global --package <key@version> --expected-revision <n> --reason <text>` | 调 `MethodService.activate_global`,actor=`"authenticated_user"`(CLI 即用户本人,与 decide/rollback 的调用方一致);打印新 head revision 与 method version id;幂等 replay 提示照现有惯例 |

在 CLI 注册处挂载(读 `cli/__init__.py` / `cli/main` 的现有注册方式,新增 `methods` 组)。

## 测试(`tests/unit/test_methods_cli.py`,照现有 CLI 命令测试的 CliRunner 模式;先看 rules/snapshot 的测试怎么起内存库)

- validate:合法文件 exit 0 + 打印 hash;非法文件聚合错误 exit 1。
- sync:首跑入库;重复跑幂等(无新版本);坏文件不入库;sync 后 `MethodDefaultHead` 与全部
  `MethodHead` 均未变化(显式断言两张表行数/revision 不变)。
- activate-global:revision 不符退出非零;成功后 status 显示新 head;per-listing head 不动。

## 验收

三门禁 + 目标测试全绿;`ia methods --help` 可见(手动 smoke 由验收阶段执行)。
