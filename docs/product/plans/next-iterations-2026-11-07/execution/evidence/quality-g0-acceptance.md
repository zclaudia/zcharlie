# QT-041 G0 独立离线验收

状态：G0有界交付切片通过。验收者未编写G0业务服务或组件；只新增独立验收测试与review发现的DOM反例。本卡不验收质量实验功能、真实模型质量或生产启用。

## 验收范围与证据

新增 `tests/integration/test_quality_entry_acceptance.py`，分别运行原进程内ASGI准入和资产包装两个局部切片，不调用executor/provider，不把它们拼接成全栈旅程。

| 场景 | 实测结果 |
|---|---|
| 没有candidate或mandate，只看预检／创建空会话 | preflight返回真实missing；Task、BudgetEntry、ExecutionGrantRow、TaskMethodBinding、MethodProposal均为0。创建原会话仍无Task。 |
| 用户显式提交原MessageInput | 原message路由202，真实ConversationService→admit创建唯一queued Task、原grant、baseline方法binding和pipeline_run。grant为authenticated_message／interactive／company_research，scope只有既有listing及原case；无mandate／watchlist扩大，没有新增原件采集权限或实验shadow binding。 |
| 不启动执行器 | BudgetEntry、AgentModelRun、TaskAttempt、ToolAction均为0；原Task及research读取端点可访问，研究结果列表为空，未伪造观点或完成状态。 |
| 原消息确认丢失后同key重放 | 同conversation/client_message_id/body返回完整原回执，同一Task/grant/binding；异body使用原key为409；Task和Message数量仍各1。 |
| 预检之后关闭Agent | 原发送准入重新检查当前开关，保持既有execution_denied=409，Task/grant/binding/message/run/budget均未写入。preflight本身feature_disabled=503由wiring独立测试覆盖，不混淆两个API既有错误协议。 |
| G0实际发行资产 | 使用已交付PK helper，将源码及pyproject复制到临时目录、使用现有离线backend构建wheel、ZIP核验3项实际G0资产非空：`templates/_research_entry.html`、`static/research_entry.js`、`static/pages/research_entry.css`。不安装、发布或升级依赖；不是仅检查源码目录存在。 |

命令：

```sh
NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/integration/test_quality_entry_acceptance.py -m 'not network' -q
```

最终结果：4 passed in 8.74s，退出0。Ruff该文件通过；`.venv/bin/python -m mypy tests/integration/test_quality_entry_acceptance.py` 返回 `Success: no issues found in 1 source file`，退出0。

## 复用的独立局部检查

- 预检测试11项＋真实auth／同源／注册wiring5项：16 passed in 7.88s，退出0，使用共享guard及精确文件清单重跑。覆盖owner、subject、未来取得／公开／created_at过滤、候选与verified区分、无写SQL、feature503、401/403/404/422及原页挂载。
- 共享guarded jsdom／Node组件：4项全通过，包含明确勾选才提交、编辑作废预检、旧响应不授权、缺模型阻止发送、文本转义、真实停止原因及反证缺口；新增“lost ack→retry404→reload→retry”保持原key/payload的反例已通过。
- 两项独立Review问题均由业务作者／主代理回修并关闭，见 [review-g0-entry.md](review-g0-entry.md)。

## 未验证

不启动API lifespan、调度器、worker、模型执行器、真实provider、网络、账户、完整服务栈或真实浏览器；无真实E2E。390/1440实际视觉效果、真实模型质量、研究能否形成可信观点、生产吞吐与长期稳定性均未验证。queued Task及发行资产通过并不证明研究或正式发布会成功；G0页面保留该边界。
