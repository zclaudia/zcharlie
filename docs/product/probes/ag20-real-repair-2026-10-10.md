# AG20 唯一修复验证轮

首轮失败记录仍在 ag20-real-trial-2026-10-10.json；本轮没有覆盖或删除任何旧Task、ledger或usage。用户原授权经父任务明确放行，在同一隔离目录执行一次 --retry-existing。

修复了 get_evidence 输入引用类型与用途说明：evidence UUID、正整数 listing 主体；原生参数重试最多两次，预算/权限错误仍终止。离线五请求回归验证错误引用可修复，发现真实SQLite快照→capture候选→合法读取→保存明确七维缺口 partial；该 FunctionModel 结果不是三家公司真实模型结论。

国电首轮拒绝字段不可追溯：parsed draft 的完成验证失败发生在保存checkpoint的同一事务，失败回滚，未保留候选。没有猜测或重建初轮输出。现在拒绝候选仅私有保存 code/fieldpath/draft；公开安全字段提示和下一步，不公开原始thinking。数字文本拒绝测试明确定位 progress.gaps.0；引用/节点错误也保留准确路径。

|公司|前次累计剩余请求/token|新Task|结果|新增HTTP/token|
|---|---|---|---|---|
|招商银行|7 / 38461|b966ccfc-b9d1-44aa-9610-c3180e3789f4|failed / budget_exhausted|0 / 0|
|海康威视|7 / 38437|fcb9db2d-e79e-458c-878f-f49c59a823f1|failed / budget_exhausted|0 / 0|
|国电南瑞|7 / 37153|fe09a9b6-a297-4745-a4fd-cfc6034408d0|failed / budget_exhausted|0 / 0|

三项都在首个HTTP发送前被保守预算预留拒绝。runtime按消息JSON字节数加工具/输出schema字符串字节数估算输入，另预留4096输出；完整研究schema的预留超过各公司剩余37–38k。实际上一轮输入约11k不等于本轮可安全降低预留；没有放宽额度、改变估算、重置跨日额度或额外付费轮次。该限制阻塞了真实模型修复后的语义验证。

AgentModelRun http_attempts=0/http_responses=[]、framework_requests=0、usage=null，表示根本未发送；Task未消费requests/tokens，不能把缺少Provider usage写成额外付费未知调用。旧3个真实HTTP/35949token总额保持。总授权余量21requests/114051tokens并未使用，成本仍未知null，绝不填0美元。

每新Task parent_task_id关联旧失败Task；真实BudgetEntry lifetime累计扣减，unknown/sent/reserved拒绝；新Task cap为公司余量与全试用余量min，原day ledger不清。离线跨日测试证实旧day支出仍扣减，原始结果完整保留，修复结果文件存在后再执行被拒绝。

本轮没有Agent公开研究答案/资料receipt/research_progress/正式观点，不能请求人工硬打分。人工量表、真实S03偏好配对、用户理解反馈、A1a开放继续pending；工程回归通过不代表产品阶段通过。

本轮结束已清除隔离临时密钥及对应setting change，关闭隔离Agent/background。原库021 hash/stat未变，DCF关闭。实际模型配置指纹与首轮一致。审计JSON保留实际model runs、Task/parent、消息、材料receipt/hash与累计额度。
