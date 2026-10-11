# AG20 首请求预留分解（离线）

只读 mode=ro 重构既存修复Task可信上下文，native Pydantic AI/OpenAI SDK + MockTransport捕获编码请求；没有网络、真实Provider、DB写入或新模型审计。只保存计数，不保存提示/私有history/thinking/secret。计数与实际SDK消息结构核验，不把FunctionModel token推定为真实token。

|公司|指令UTF8|用户prompt|history|消息JSON|参数repr|输入预留|输出预留|总预留|实际SDK payload|公司token余量|
|---|---|---|---|---|---|---|---|---|---|
|招商银行|4827|223|0|5580|40010|45590|4096|49686|37822|38461|
|海康威视|4824|223|0|5577|40007|45584|4096|49680|37819|38437|
|国电南瑞|4827|223|0|5580|40010|45590|4096|49686|37822|37153|

SDK每个请求只有一份可信instruction（4827/4824字节）、一个输出schema system（12319字节）和用户prompt223字节。参数repr包含function_tools21745、output_object13237、instruction_parts4895/4892及Python属性描述；消息JSON又包含同一instruction。输出schema compactJSON12069；实际tools compactJSON18141，最大prepare_company_view工具7037，save_progress4335，reverseDCF2472。

重复计instruction由SDK Model._get_instruction_parts源码与实际SDK编码共同确认：params.instruction_parts存在时优先它，只有None才使用消息instructions。当前估算含内部repr且重复指令，实际SDK未发送那些描述；但仅改成SDK payload+4096仍41918/41915，大于公司余量，不能声称单项估算修复足够。

最小修复评估（尚未实现/调用）：只在params.instruction_parts非None时估算排除消息instructions，fallback保留；原生tool.prepare在同Task尚无committed完整研究plan时暂缓prepare_company_view schema。真实prepare函数已有同样persisted_research_plan_required_before_publication_preparation强制前置，schema准备只是对齐可执行阶段；server grant/CAS/证据/七维/语义门槛原样。其他发现/capture/save与最终output schema保留；保存后同一native run自动恢复prepare，不另造循环。估计首请求保守预留36363/36360/36363，低于现余量；后续历史/恢复schema仍受真实累计预算，可能预算中止但可留下已保存partial。

父review前不实现、无真实调用；不按经验tokens/bytes比值，不推断未知供应商tokenizer，不扩大原24/150k/8/50k授权。修复验证的0HTTP尝试仍保留。

独立最小方案native编码复核：仅在临时diagnostic里过滤未ready的prepare工具、消息JSON排除重复instructions，production未改；消息JSON518、params31512/31509，精确预留36126/36123/36126，SDKpayload30784/30781。此处仍每字节保守估算，并非tokenizer测量；对应公司余量保持38461/38437/37153。
