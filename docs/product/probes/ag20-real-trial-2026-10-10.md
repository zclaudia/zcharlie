# AG20 真实模型隔离试次汇总

当前结果：三轮审计全部保留，[首轮原始JSON](ag20-real-initial-2026-10-10.json)按原字节归档，[统一审计JSON](ag20-real-trial-2026-10-10.json)嵌入三轮完整公开记录。工程回归通过与真实研究未完成分别记录；没有合格研究答案、正式观点或人工质量通过。

|轮次|真实HTTP|输入/输出token|结果|
|---|---|---|---|
|首轮真实试用|3，均200|33144 / 2805|三项failed，未交付研究|
|repair-1 零HTTP预留轮|0|0 / 0|首请求保守预留超累计余量，未付费|
|repair-2 最后真实修复轮|3，均200|26688 / 13032|schema可调用，三项progress.question数值/身份文本门槛拒绝|
|累计|6|59832 / 15837 =75669|各公司24790/24613/26266<50000，总<150000|

Provider已发送max_tokens4096却报告输出4356/4156/4520超过参数上界；真实usage完整结算，不假设原因或供应商遵约。money cost未知null；余18requests/74331tokens不再使用，临时隔离密钥清除，正式设置/原库未写、Agent/background保持关闭。

[预留与SDK编码诊断](ag20-reserve-diagnostic-2026-10-10.md)、[零HTTP前次](ag20-real-repair-2026-10-10.md)、[最后真实轮](ag20-real-final-repair-2026-10-10.md)。数字/身份不能直接放question/gaps等Text；该字段写无量化描述，具体数字/身份放progress.content引用节点。工具异常安全字段透传已补工程回归；旧海康尝试不改写为新通过。

额外协议限制：试用用户正文和同Task可信assembly只有listing ID，没有目标公司名/canonical ticker，且没有identity发现工具；get_snapshot却要求canonical ticker。只读审计确认这项身份上下文覆盖缺口，不能把全部问题仅归模型；直接实测拒绝原因仍是progress.question未引用数量/代码，不声称身份缺口必然导致本次失败。该工程缺口已最小修复：仅已授权grant/binding目标注入服务端当前canonical name/full ticker/exact listing_ref，无新查询工具或权限扩张，缺失unavailable；默认native worker离线SDK请求/真实SQLite来源、同名/错scope/历史绑定验证通过。此修复未再调用真实模型，不能改写本报告历史失败；后续方法与模型质量改进待处理。当前无可供人工硬打分的研究结果，人工量表/用户理解/A1a开放继续pending。

以下保留首轮历史说明（不是当前总额）：

用户明确授权临时复用当前供应商与模型；effective legacy tier3只读映射到隔离Agent。deepseek-flash / Pydantic AI2.31.1 / prompted json_object / DeepSeek reasoning profile。配置指纹 317123a5f92685a0811dab6ab9f5d2e5e1f08f6deb4ac6999f2d00538b7179ea。

本次真实HTTP能力可达，但三公司研究未通过，不签署人工质量，不自动补跑，不开放A1a。

|公司|Task/停止原因|HTTP|输入/输出token|模型延迟ms|Task耗时s|正式观点|
|---|---|---|---|---|---|---|
|招商银行|failed / failed|1，200|11050 / 489|2362|2.554|无|
|海康威视|failed / failed|1，200|11047 / 516|2201|2.268|无|
|国电南瑞|failed / evidence_blocked|1，200|11047 / 1800|8688|8.774|无|

总计真实HTTP 3次，输入33144 / 输出2805；money cost未知null。所有实际材料receipt为空（hash是空集合hash），没有把资料包hash当实际研究材料。

招商银行：模型把listing引用作为get_evidence的证据引用，可信工具停止，InvestorError。海康威视：同样把listing引用作为证据，并将case作为subject_ref；可信工具停止，InvestorError。国电南瑞：模型结构化输出解析成功（AgentModelRun success=true），但ResearchPolicy完成校验拒绝（ExecutionDeniedError/evidence_blocked），Task failed。模型运行success绝不冒充研究success。

本次没有公开assistant研究回答、没有research_progress/正式观点产物。立场、依据、反证、下一步的Agent答案尚未交付；可审阅的是真实失败及边界证据。原材料截止与缺口见此前真实资料包。第三项具体字段级拒绝原因未被当前公共错误审计保留，只能确认可信完成边界拒绝，不猜测细节或重建原始输出。

下一步：在离线复现中完善证据引用类型与错误诊断的可理解性，再由父任务明确决定是否授权新的有限试次；本次失败试次/usage不删除、不以新结果替换。人工量表和用户复述继续pending。

凭据未入报告，正式配置未写，原库hash/stat不变，DCF保持关闭。

试次结束后已将隔离Agent/background开关关闭，并清除隔离设置及变更表中的临时agent_api_key记录；保留Task/动作/模型usage和失败审计。正式库hash/stat再次不变。
