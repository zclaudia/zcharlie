# QT-004：保存结果的 Pydantic Evals 薄适配

日期：2026-11-07。结论：固定环境的 API 可用，可作为可选本地展示/断言适配；核心评分、分母、不可变报告仍采用本项目纯函数。此次没有安装或升级依赖，没有模型 judge，也没有建立正式实验执行器。

## 版本与依据

在 offline guard 下读取安装元数据：`pydantic-ai==2.31.1`、`pydantic-evals==2.31.1`、`pydantic==2.13.5`。先查询了 [官方概念文档](https://pydantic.dev/docs/ai/evals/evals/) 与 [v2.31.1 的 dataset 源码](https://github.com/pydantic/pydantic-ai/blob/v2.31.1/pydantic_evals/pydantic_evals/dataset.py)，再核对已安装包签名与源码。文档说明 Dataset/Case/evaluator 的组合；固定版本源码提供 evaluate 的 repeat、retry_task、retry_evaluators 控制。当前文档不能替代固定版本运行证据。

本地实测发现 `Dataset` 要求显式 `name`；补齐后通过。`evaluate` 支持 async 读取函数、`max_concurrency=1`、`progress=False`、`repeat=1`；重试参数显式传 `None`。固定版 `_run_task` 仅在 retry 配置为真时包裹 tenacity。测试不依赖额外重试包，不配置 Logfire 导出。

## 实际运行范围

测试先通过原 ConversationService、原 ResearchWorker/AgentExecutor、FunctionModel 在临时 SQLite 生成并提交一个成功 explanation 与一个非法输出导致的真实失败 Task；通过原 TaskService/research_artifacts 回读状态、停止原因与制品数量。没有手写成功/失败状态。两条记录保存在 Evals 开始前。

传给 Dataset 的函数只读取内存中冻结的保存结果投影，不持有数据库 session、admit、worker、模型或研究提交函数。EqualsExpected 仅检查投影保持一致。失败研究仍是一条明确的 `failed` 数据，不能把 Evals 的两项断言通过称为两次研究成功；这里也不把研究失败混为 Evals 执行异常。报告保留两条结果，原 Task/BudgetEntry 计数和模型调用次数在 evaluate 前后相同，每条投影只读一次。

这证明保存结果可以本地适配；不证明研究质量、真实 provider、真实付费、正式 quality_result DTO 接线或完整实验隔离。外部 repeat/retry 不允许包住付费 Task，后续适配仍只能接收已保存结果。

## 验证

```sh
NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/unit/test_quality_evals_adapter.py -m 'not network' -q
.venv/bin/python -m ruff check tests/unit/test_quality_evals_adapter.py
```

结果：`1 passed in 3.35s`；ruff 通过。所有运行使用 BASE-03 guard，无外部 provider 调用。
