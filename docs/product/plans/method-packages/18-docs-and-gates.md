# 卡 18:文档更新与全量门禁

前置:卡 11–17 全部完成。

## 文档

1. `README.md`「方法包」小节:状态改为「M1–M4 已实现」,补一段包级提案与影子
   (工作台入口、候选包前瞻影子由用户显式发起、批准门槛:实质失败样本 + 反例 +
   影子全覆盖 + evidence_hash 绑定);「完整专题」行同步去掉 M4 未实现标注。
2. `docs/product/investor-agent-method-packages.md` 状态行与 §11 交付表:M4 完成标记
   (含日期);如实现与设计有偏差,在对应节加「实现备注」。
3. `docs/product/plans/method-packages/m4-plan.md` 加执行结果小节(各卡完成情况、偏差)。

## 全量门禁(命令与既有失败处理照 00-plan 全局约束,import graph 既有失败 --deselect)

```bash
.venv/bin/ruff check .
env API_KEY=dummy-test-key-123456 .venv/bin/python -m mypy src/investment_agent
env API_KEY=dummy-test-key-123456 .venv/bin/python -m pytest -q \
  --deselect tests/unit/test_import_graph.py::test_no_new_package_edges_beyond_baseline
```

失败处置:m4-plan 全局约束 6/7/8(受保护并行工作、config-only 零回归、M1–M3 语义不动)。

## 报告

各门禁数字、新增/改动文件清单、偏差列表、v1 与 config-only 不变性证据(测试名)、
M4 关键正反例清单(实质 block、非实质拒批、影子全覆盖门槛、影子无发布副作用)。
