# P2 实现复审记录

日期：2026-10-16（Asia/Shanghai）。状态：已完成主代理复审与修复验收。

主代理按任务接缝复核子代理实现。下列问题已修订，并由反例专项、完整离线分区或真实浏览器验证闭环；最终数量、时间边界和能力限制见[工程验收](acceptance.md)。

| 问题 | 修订方向/验证 |
| --- | --- |
| 初始DTO草案混入不相关peer/manifest实体、维度或操作符偏离旧契约 | 固定五表与原七维、原操作符；撤销不相关实体 |
| 应用hash误要求等于pair hash | 应用另绑定pair、manifest、模式及purpose，保留精确恢复 |
| 递归指标组的true/false与triggered/not_triggered混用 | 统一父子传播并增加嵌套反例 |
| 递归类别、来源引用和证据revision边界 | 递归解析，精确kind/id/revision，原件/主体范围校验 |
| HTTP路径偏离计划，草案列表与历史读取缺失 | 按正式路由契约补齐，覆盖实际HTTP |
| 变更触发run的manifest只在可变队列中自校验 | 将清单hash绑定不可变命令收据，逐命令核对mode、purpose、origin和时间 |
| draft状态错误、退役暂停规则及拒绝后的revision分配 | 明确状态转换，内容revision按不可变历史max分配 |
| ORM/迁移SQL中的NULL和空previous_ref绕过 | 冻结迁移DDL，ON/OFF直接SQL反例及create_all一致性 |
| 服务层反向依赖HTTP schema | 纯命令DTO独立，HTTP层只复用数据契约 |
| 当前有效性误受无关规则或事件影响、只验一页 | 主体语义摘要与全声明集合校验，相关变化保守限制 |
| 真实事件载荷hash只比对存储列，未重新计算 | 精确仓储读取重算内容hash；伪造一致列/载荷仍拒绝 |
| 来源扫描截断、可变行时间缺口仅有诊断而因子可能仍已知 | 受影响因子质量降为unknown；独立来源不被无关缺口污染；scan诊断进入coverage/current-limits |
| history直接投影SQLite naive时间戳 | DB读边界按持久UTC规范化；真实HTTP历史回归 |
| current UI误读run字段、精确引用property未序列化、证据数量和分页遗漏 | 按真实DTO显示范围、显式构造exact refs、读取证据id/revision并实现cursor翻页 |
| 去重辅助函数未接入真实个股展示 | typed issue_groups连接current和页面；跨event revision按稳定事件身份聚合，保留每条severity/材料/条件/解除要求，不取第一条代替整体 |
| 候选历史夹具依赖测试运行时更新时间 | 固定夹具updated_at；不放松未来知识阻断 |
| 补充面板误显示HK/US、A股页漏载P2样式 | 按支持市场限制面板；模板引用样式，实际浏览器computed-style及截图人工复查 |
| accepted写入和旧pending清理同时失败，刷新提供重发；恢复警告被覆盖 | 按operation/path/body及服务端operation/manifest精确核验durable run跟踪；双失败reload反例，保留告警 |
| P1窄屏浏览器双击遇布局变化落在form背景 | 等待真实context就绪与稳定位置，窄屏两次重新定位click验证pending拦截；不改P1业务脚本 |

P3正式语义解除评审未实现；纯机械评估不能制造reviewed/resolved。通用可信监管/产品暴露原件适配仍有明确缺口，能力接口及页面须如实显示pending/unknown。

## 导入图复核

主代理逐项核对新增12类包边后更新baseline：api/research调用独立补充事项服务/handler；storage只导入P2纯contracts/canonical，不导入应用服务或HTTP；supplemental_rules读取clock、config、evidence、factors、investor公共契约、jobs协议、settings及storage。其providers依赖仅本地Shenwan目录元数据，不是采集调用。服务命令DTO已移至纯commands.py，未保留supplemental_rules→api的反向依赖。没有放宽绝对分层规则或动态跳过新包。

主代理最终逐项核对JUnit身份、失败/跳过及原始日志，并复核新增导入边、生产默认关闭及文件边界。完整分区与专项重叠不累计。并行mobile及Web嵌入返回交互的agent.js/shell.js改动原样保留、不归入P2；最终另补Web client契约与浏览器兼容检查。测试自动重写的P1旧截图恢复到既有版本，P2合成截图单独存放。

用户随后要求全面复审。本次追加发现、修复、撤回项和最终证据见[后续全面复审](followup-review-2026-10-16.md)，首次记录保留，不以旧通过数字代替后续验收。
