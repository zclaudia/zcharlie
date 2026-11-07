# BASE-01 执行基线

状态：基线清单已记录；环境与命令检查见同目录environment.json。

HEAD：`1f62fa4ac3dd22be5ef65550ae6ac0444098b313`。工作区已有模拟交易并行修改，精确路径及初始hash见baseline.json；hash仅用于归属检查，不冻结其他任务的合法后续变化。

本轮实现只领取执行任务卡的文件范围。共享接线、Alembic链、全局注册由主代理持锁。现有061_simulation.py及所有原有dirty内容均保留；下一迁移必须读取当时实际head。

测试使用BASE-03离线runner、临时SQLite和惰性凭证；不运行tests/browser、真实E2E、生产服务或外部provider。不安装或升级项目依赖。已有环境若缺必要工具先报告真实缺口，不伪造通过。

候选命令：`.venv/bin/python scripts/check_next_iterations.py --scope <ID> --offline`；Ruff/mypy仅检查受影响文件；Node组件走共享bootstrap。初始新增范围尚无实现，故本卡不宣称业务基线测试通过；相邻回归由各实现卡按改动选择，已有失败单独记录。
