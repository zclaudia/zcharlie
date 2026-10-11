# AG20 最后真实修复验证轮

在原用户24 requests /150000tokens授权与公司8 requests /50000tokens累计限制内，父明确放行一次真正有HTTP的最终修复验证；同目录 repair-2 task.parent_task_id指向repair-1零HTTP Task，其parent再指向初轮失败。所有初轮、零HTTP前次、最终轮审计保留，无复用终态Task或清ledger；不再付费重试。

已修复保守预留的重复instructions，并用原生tool.prepare在真实服务原已要求的committed七维研究计划存在前暂缓prepare_company_view；save后同native Agent下一请求自动恢复。其他tool/output schema、授权/CAS/七维/数值/语义门槛保留，没有tokenizer经验比率或自造工具循环。SDK离线计数首预留36126/36123/36126，实际SDK编码30784/30781，真实网络恢复可调用。

|公司|输入/输出token|HTTP与延迟ms|Task/停止原因|累计公司tokens|可审阅研究产物|
|---|---|---|---|---|---|
|招商银行|8895 / 4356|1，200；22007|failed / evidence_blocked|24790 /50000|无 research_progress/正式观点|
|海康威视|8894 / 4156|1，200；21246|failed / failed|24613 /50000|无 research_progress/正式观点|
|国电南瑞|8899 / 4520|1，200；21790|failed / evidence_blocked|26266 /50000|无 research_progress/正式观点|

本轮3 HTTP200、26688输入/13032输出，共39720tokens；累计首轮6 HTTP、59832输入/15837输出，共75669tokens，未突破150000总限额，各公司累计均未突破50000。预算余18请求/74331tokens，保留且不再使用。money_cost_usd未知null，价格未验证，不能填0。

三家公司均因 research_numeric_text_requires_reference / progress.question 拒绝：模型在Text问题字段写入证券代码或数量，未通过既有数值/身份引用门槛。招商/国电在完成校验被拒，已保存私有候选诊断且公开安全字段/下一步；海康尝试save_research_progress，准备动作存在但事务未提交，runtime当时包装ResearchValidationError丢safefield，公开未形成回答；从持久prepared business经相同只读验证恢复确切safefield，原模型输出未重建或公开。

最终工程已修复工具异常safe code/field_errors透传并补离线回归，历史海康Task与审计保持原实际状态，不伪装为新通过。文本错误的可执行修复：question/gaps等Text字段仅写无量化描述，具体数字/证券身份放progress.content的numeric_claim/entity_ref引用节点，保留来源/单位/期间；不能把对象塞入Text字段。正常无量化中文描述仍可保存。

三次已发送max_tokens=4096，但Provider usage报告4356/4156/4520 completion_tokens，均超过请求输出限额，原因未经证实。保守reservation和发送参数不能保证供应商遵约；报告实际超量并完整结算，本轮公司与总累计限额未突破。此Provider输出遵约尚未验收，不能将参数上限描述为实测供应商硬保证。

实际材料receipt仍为空，资料包hash不是本次实际用料hash；无真实支持/反证或方向判断。安全错误解释是执行结果说明，不是研究答案通过。方法/模型仍未在三样本完成研究，需要后续质量改进；人工研究量表、真实S03配对、用户理解与A1a开放继续pending，目前没有可要求人工硬打分的正式结果。

模型/方法/冻结材料版本、真实AgentModelRun HTTP/usage/latency、Task lineage及safe诊断见JSON。deepseek-flash/Pydantic AI2.31.1/fixed company_research.v1，config fingerprint与初轮相同；观察截止固定2026-10-10T04:00:00Z。隔离临时密钥及setting change已清、Agent/background关闭；源021 hash/stat未变，未commit/reset或启用生产，FCFE0/4/DCF off。

最终工程验证：safe工具字段透传及可执行Text提示专项8 passed4.46s，正常中文研究问题可保存partial；最终固定full1951 passed、3 skipped、6 deselected183.69s，三个skip缺外部Node gateway checkout；Ruff/mypy247/diff通过。原Model异常和旧Task不回填成新通过，人工仍pending。

最终身份工程bug修复验证：`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/integration/test_agent_company_acceptance.py tests/test_agent_research.py tests/test_investor_publication.py tests/test_investor_monitoring.py` **122 passed，31.69s**；`.venv/bin/ruff check src tests tools`、`.venv/bin/mypy src`（247 files）及`git diff --check`通过。最后固定`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` **1955 passed、3 skipped、6 deselected，195.63s**，三个skip仍为外部Node gateway checkout未提供。身份修复后无真实模型调用；真实研究/人工/A1a门槛继续pending。隔离所有ciphertext凭据行及对应secret设置change已清除，Task/Action/ModelRun审计保留；源库021 stat/hash不变，Agent/background默认off。
