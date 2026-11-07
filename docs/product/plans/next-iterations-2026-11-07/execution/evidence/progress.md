# 实施进度

用户已授权开始实现，当前仍在执行，四方向整体未完成。原子卡状态见 `../task-index.json`；原子卡 done 不替代后续独立 Review、方向验收和跨功能验收。

## 已验证的切片

- BASE-01/02/03 与 PK-01/02：保护用户 simulation 工作、统一网络阻断测试入口、合同和离线 wheel 包装；独立复核通过。
- EV-01 至05：13种闭合事件合同，模型、062迁移、同session append 与可信源resolver。EV01–03套件121项通过；EV04–05套件44项通过。推荐事件的case/episode必须来自正式已发布thesis精确引用，不能信任载荷指定任意历史case。
- QT-011/012/013/041：普通研究入口接入原Agent准入，QT-G0主代理20项Python、4项DOM通过，含实际资产wheel；失联重试与窗口提示两项review已关闭。
- QT-001/002：隔离实验合同、C1反证强化包、12个合成not_run情境（4开发/8留出）已冻结。QT003发现fixture数字原为字符串导致原materializer得到空值，已改为严格整数、升级v1.1身份/hash并通过原EvidenceService/NumericResolver回读；12项dataset测试通过。不把fixture当实验成功。
- BT-001至009：闭合合同/精确fact binding、七表及063迁移、四类引用、历史只读与授权幂等mutation仓储、本地年报金样。主代理BT-foundation210项通过，加mutation14项通过；仓储56项由作者验证。ConditionHead完整性问题已关闭，UUID历史表WITHOUT ROWID阻止隐藏rowid替换。
- PT-001至005：原AG22 save/get合成金样、S1合同、三表及064迁移、正式采信隔离。主代理PT-foundation68项、原publication/AG22回归102项通过；现金缺失和缺FrozenPrice不能标完整，模型rowid问题独立复核关闭。
- IT-001/002：InboxContribution等合同47项；entry/journal/source模型与ReviewCycle显式版本41项通过，mypy通过。journal不可修改/删除/替换，已读与内容版本独立；业务迁移按IT003/EV12依赖尚未交付。
- EV06/07：projection/receipt/cursor同事务、退避/隔离/链序和健康摘要已完成；作者事件套件185项，主代理24项新增检查通过。EV08/09机械tick与专用worker/scheduler接线已完成，主代理35项通过（4.60s）；总50/20/20及2秒超时、取消回滚当前unit、lease和慢研究隔离均有离线证据。仍等待独立EV10–12。
- QT003/004：原提案→准入→读证→方法结算通过，缺反证由原validator拒绝；固定Pydantic Evals 2.31.1可只读已保存结果，不新增Task/预算/模型调用。主代理分别复跑通过。详见quality-compatibility.md、quality-evals-spike.md。
- QT005/006/007：五表、owner/CAS/追加制品/收据仓储、默认关闭的两个开关、065迁移和质量制品窄SQL保护已完成。主代理45项相关模型/仓储/迁移/设置检查通过，1项旧dotenv专用测试因offline guard主动阻断而不纳入本批；未改旧测试或放宽保护。QT008原生Task/预算与新表外层事务集成测试正在独立复跑。
- BT010–013：原件proof、标准化、Decimal分解、精确来源与旧peer/evidence桥接已完成；主代理103项通过（12.43s），含实际本地PDF与合成反例。仅支持已审核的招行FY2025原件profile，未审核新报告仍pending_review。
- PT006/010/011：精确account/snapshot/valuation/mandate解析17项、Decimal压力内核15项通过。缺值null+reason、费用不重复扣、逐股/行业/全局覆盖、极大有限数与ambient Decimal上下文隔离有针对性测试。
- SV01：原件事实保存的owner由当前grant传递，已存在的manifest/page/fact也核owner；8项离线检查通过。SV02真实更正事件生产者尚未完成。

## 当前工作

- EV10–12：非作者故障注入、代码Review和基础验收；完成后推进收件箱迁移/投影。
- 银行BT014–016：基础批审查和离线验收。
- 质量QT008–010：持久化批集成测试与交叉作者审查，之后开始冻结材料包。
- 组合PT007–009：原AG22-v2冻结适配、价格年龄缺失的保守处理、逐路径csrc时点来源副本。
- 其余服务、生产者、API、页面、队列和四方向最终验收尚未完成；不能宣称整体可上线。

迁移当前单head065；未写生产数据库、未启用实际服务。仅临时SQLite、进程内ASGI、mock、DOM、离线包装测试；没有真实E2E或真实模型/provider/行情/账户操作。测试计数分套件报告，有重叠，不相加声称唯一总数。

工作区存在用户同时推进的simulation改动，多个文件相对初始baseline已改变；本任务保留现有内容，不把它们认作本轮交付。061迁移在最近核查中未变；不能宣称所有baseline文件hash均未变化。共享worker/scheduler/compose只追加本任务接线。
