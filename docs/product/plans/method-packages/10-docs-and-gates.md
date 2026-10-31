# 卡 10:文档更新与全量门禁

前置:卡 01–09 全部完成。

## 文档

1. `README.md`:
   - CLI 表新增 `ia methods` 行:`validate`、`sync`、`list`、`show`、`status`、`activate-global`,
     用途「方法包:校验/导入/查看/全局启用(不移动 per-listing head)」。
   - 「完整专题」行的方法包链接去掉「草案,未实现」标注,改为「M1–M3 已实现;包级提案与影子(M4)未实现」。
   - 简短一段(放「规则与筛选」附近或单独小节):方法包是什么、生产默认仍 v1、显式启用入口。
2. `docs/product/investor-agent-method-packages.md` 状态行更新:M1–M3 已实现(2026-10-30),
   M4(第 7 节)未实现;§11 交付表加完成标记。
3. 本计划目录 `00-plan.md` 加执行结果小节(各卡完成情况、已知偏差)。

## 全量门禁(必须全绿,失败即修)

```bash
.venv/bin/ruff check .
env API_KEY=dummy-test-key-123456 .venv/bin/python -m mypy src/investment_agent
env API_KEY=dummy-test-key-123456 .venv/bin/python -m pytest -q \
  --deselect tests/unit/test_import_graph.py::test_no_new_package_edges_beyond_baseline
```

**已知既有失败(基线漂移,不是本特性引入,禁止"修复")**:
`test_import_graph.py::test_no_new_package_edges_beyond_baseline` 在本轮改动前就失败
(既有边 `candidates -> snapshots`、`rules -> snapshots` 未入 ratchet 基线,来自更早的提交)。
处理方式:全量命令带 `--deselect` 跳过它,在报告与 `00-plan.md` 执行结果里如实注明;
**不要**再生成 `import_graph_baseline.json`(接受新边需要用户作为评审人确认),也不要改该测试。
本特性新增模块都在既有包内(investor / agent / cli),若门禁发现方法包代码引入了**新的**包间边,
先调整模块归属消掉,而不是动基线。

全量其余失败时:先定位是否本特性引入;是则修复;与既有未提交修改(`mobile/`、`listing.py` 等)
相关的失败**不得触碰那些文件**,在报告中说明。

## 报告

输出:各门禁数字(测试通过数)、新增文件清单、改动文件清单、与设计文档的偏差列表(若有)、
v1 不变性的证据(测试名)。
