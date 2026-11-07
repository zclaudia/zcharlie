# 跨功能集成、代码Review与最终离线验收

状态：全部待执行。本任务包收口四条功能线，不能用各方向的fixture适配测试替代。执行规则与文件锁见[总计划](../execution-plan.md)。所有代码Review由未编写被审主体的代理完成，主代理复核结论。

只组合必要的实际领域服务接缝：临时SQLite中的生产者/事件/投影，或进程内API，或独立DOM。不得启动浏览器、真实provider、真实账户、真实全栈服务或真实E2E；不将API和浏览器串接为完整用户旅程。外部解析与模型使用明确fixture，核心事务/CAS/准入/公式/来源解析使用实际代码。

表中`E/`为本目录`evidence/integration/`，`S/`为`src/investment_agent/`；证据文件执行时再创建。估算为人日。

## 任务卡

| ID | 类型 | 任务与交付物 | 允许写文件或新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| XA-01 | 集成 | owner=集成；核对并完成四方向来源/贡献适配的真实注册，移除仅测试用占位接线 | `S/research_events/contracts.py`、`S/research_events/registry.py`、`S/research_events/resolvers.py`；`S/research_inbox/adapter_registry.py`、`S/research_inbox/adapters.py`仅topic/账户/更正映射；`E/registration-manifest.md` | QT-050, IT-050, BT-050, PT-055, SV-06, PK-02 | 每个实际producer能由实际resolver回读；topic/账户保持正确subject；source_corrected可消费；无“后续再接”的设计内来源；不扩大ObjectRef正式采信，不新增第二outbox | 1 |
| XA-02 | 测试 | 银行检查→实际收件箱投影的局部服务集成 | `tests/next_iterations/test_bank_inbox_integration.py`（新）；`E/bank-inbox.md` | XA-01 | 以本地金样/synthetic未来事实驱动真实check/事件/投影；初次、重复、旧已读后新更正分别验证；解释未审核不变已证实支持；无新模型调用 | 0.75 |
| XA-03 | 测试 | 压力/决定核对→实际账户事项投影的局部集成 | `tests/next_iterations/test_portfolio_inbox_integration.py`（新）；`E/portfolio-inbox.md` | XA-01 | due/completed/partial/superseded归同一账户观察；旧确认不吞新结果；账户事件不拆股、不调用公司manual；压力默认context不占每日待办；权限撤销实时拒动作 | 0.75 |
| XA-04 | 测试 | 可信更正→研究实验/收件箱的失效接缝及正式证据隔离 | `tests/next_iterations/test_quality_source_integration.py`（新）；`E/source-isolation.md` | XA-01 | 真实保存更正发事件并命中旧精确ref；重复消费幂等、无EV时直接检查仍拒相关读取；无记录不能冒认撤回；实验输出不进入正式搜索/监测/发布/收件箱/记忆 | 1 |
| XA-05 | 测试 | 四方向HTTP错误、owner与功能开关合同矩阵 | `tests/next_iterations/test_feature_contract_matrix.py`（新）；`E/api-contracts.md` | XA-01 | 进程内ASGI逐路由检查detail/FieldError数组、feature_disabled503、旧writes_disabled403、历史可读、原key收据恢复、跨owner不泄露；新开关不启动模型/采集/外发 | 0.75 |
| XA-06 | 集成 | owner=集成；检查整个迁移链与模型/索引/不可变保护、关功能兼容和无删除回退路径 | `tests/next_iterations/test_combined_migrations.py`（新）；`E/migrations.md`；若需修复仅尚未发布的本轮迁移及新模型，先登记精确finding | XA-01 | 从实际基线到最终head、空库及含旧fixture库均通过；唯一head；现有simulation差异由其owner保留；模型/create_all与迁移一致；不会为回退删含用户记录的表 | 1 |
| XA-07 | 集成 | owner=集成；完整静态资产/包装/入口验证，接入专用离线检查与CI清单 | `tests/next_iterations/test_all_feature_assets.py`、`.github/workflows/next-iterations-offline.yml`（新）；`scripts/check_next_iterations.py`仅全范围allowlist；`E/build-and-commands.md` | XA-01, PK-02 | 实际四功能模板/JS/CSS均入临时wheel且路径可解析；新增测试目录与DOM用例都纳入清单，数量非零；workflow不含browser/E2E/network或服务部署；现有CI不删除、不将整套browser作隐含门槛 | 1 |
| XA-08 | Review | 独立总代码Review：跨方向调用链、共享文件diff与全部证据 | 仅`E/final-code-review.md`；业务及测试只读 | XA-02, XA-03, XA-04, XA-05, XA-06, XA-07 | 复核原22设计问题在实现中的关闭证据；检查冻结输入/并发/CAS/lease/预算/时点/引用/分片/资料更正；每个发现有位置/触发/影响/关闭条件；不是只读测试摘要 | 1 |
| XA-09 | 回修 | 按总审逐条拆分并关闭缺陷；跨模块问题由相关owner分文件修复 | 仅XA-08点名且原任务已允许的文件；热点owner=集成；`E/final-fixes.md` | XA-08 | 问题→责任人→diff→反例测试→独立复核链齐全；阻断项0；超出1人日的单问题继续细拆，不由验收代理顺手补实现；预算为预留不是上限 | 1.5 |
| XA-10 | 测试 | 对最终集成提交执行确定的离线回归与静态检查，验证测试隔离 | 仅`E/final-checks.json`及日志；业务/测试只读 | XA-09 | wrapper覆盖所有新套件及受影响旧回归；guard跨目录/子进程诱饵通过；零意外外连；输出收集数/命令/退出码/基线差异；针对新改动复验而非机械重复无关全仓 | 0.75 |
| XA-11 | 验收 | 独立最终工程验收，核对四功能全部范围与已排除真实E2E边界 | 仅`E/final-acceptance.md`、`E/coverage.json` | XA-10 | 全任务及四方向原场景逐条有实现/测试/Review证据；无stub/TODO代替设计内交付；配置/迁移/操作资料齐全；实际视觉/真实模型质量/生产运行保持未验证，不伪造已测结论 | 1 |
| XA-12 | 验收 | 主代理最终签收及移交，更新任务状态与交付摘要 | 本目录`task-index.json`状态；`E/handoff.md`；总计划完成记录 | XA-11 | 所有必需任务done且阻断问题0；列出入口、开关、实际迁移、回退、验证命令及未验证范围；代码归属清楚；不执行生产启用、真实试用或交易 | 0.5 |

## 最终覆盖要求

| 交叉边界 | 责任卡 | 核对重点 |
|---|---|---|
| 银行与证券事项 | XA-01/02 | immutable check、事件唯一键、period与case版本、旧已读/新更正 |
| 决定与账户事项 | XA-01/03 | account subject、binding及核对版本、due/partial/superseded、无股票manual |
| 来源更正与实验 | SV-01至06、QT-046、XA-04 | 真生产者、可信owner、精确correction_refs、无付费重启、直接检查不依赖事件 |
| 实验与正式研究 | QT-032/033、XA-04/08 | 正式搜索、上下文、监测、发布、记忆与收件箱没有实验内容 |
| 全局错误/授权/开关 | XA-05/08 | 不改历史code语义、历史读取和机械结账可用、请求重放不越权 |
| schema/部署产物 | PK-01/02、XA-06/07 | 单迁移链、已存在并行工作保留、嵌套CSS入包、默认关闭但接线完整 |
| 证据与验收 | XA-08至12 | 独立审查/修复/复核、非空allowlist、隔离有效、未验证内容如实声明 |

“跨功能集成测试”只验证局部服务协作，不是本计划禁止的真实E2E。不得把四功能从界面到真实模型/外部数据/账户的完整旅程改名后加回验收。
