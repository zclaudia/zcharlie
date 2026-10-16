# P2 工程验收记录

日期：2026-10-16（Asia/Shanghai）。状态：P2独立工程验收通过（15项完成）；不构成生产启用或P3/P4交付。

归档更新（2026-10-17）：用户后续授权将P2实现、14组修复及验收文档整理为独立Git提交；范围隔离与提交前验证见[提交归档记录](submission.md)。下文“未提交/推送”为各验收阶段的历史状态，当前提交授权不包含推送、生产迁移、启用开关或推进P3/P4。

## 全面复审后的最终验收

本轮另完成数据模型、匹配、生命周期、并发恢复及兼容性的全面复审，14组问题/边界修复见[后续复审记录](followup-review-2026-10-16.md)，逐组反例、撤回的无依据限制、原始日志与源码快照见[本轮摘要](review-validation-summary.json)。下面原验收数字保留为首次交付时点，不能与本轮相加。

本轮主代理结果：全量unit **2435 passed / 2 skipped / 6 network默认排除**，全量integration **732 passed**，根目录 **717 passed**；最终专项 **132 passed**（与完整分区重叠）。P2/P1真实浏览器串行1440/390、Ruff、427源文件mypy、JS语法及051单head通过。原始材料skip保持诚实缺口；表达式索引反射警告以实际DDL及EXPLAIN补验，没有放宽元数据门禁。

当前验收仍只涵盖P2独立补充事项：未知/冲突不冒充核验结论，P1评分/排序不接P2，P3/P4及生产启用均未推进。没有生产迁移或运行时开关写入，本轮未提交/推送；并行移动端等修改/提交保留。

## 实施与门禁

按[15项计划](../account-fit-p2-plan.md)完成独立P2实现；实际文件、任务顺序与依赖见[执行分工](execution.md)。三个GPT‑6 Luna子代理实现/交叉复核，主代理负责应用服务整合、逐接缝审查和最终验收。生产设置未写入、生产库未迁移、未提交或推送。

| 门禁 | 本轮实际检查 |
| --- | --- |
| G0 | 五表、原七维/DSL、严格Decimal、稳定series、原件能力矩阵及pair/4MiB独立上限冻结；合成JSON压力形状不是合法完整清单容量证明 |
| G1 | 051单head；fresh/050升级/无P2降级往返；P1 payload/hash/entries保留；外键ON/OFF直接SQL含NULL/孤儿/UPDATE/DELETE/REPLACE；有P2应用/审计/仅零成员failed run或收据拒绝降级 |
| G2 | 真SQLite严格来源、同日/取得时间、截断/冲突未知传播、应用前序CAS、历史精确冻结和只读current；来源删除及不支持方法版本反例 |
| G3 | 真HTTP生命周期/事件、共享收据、并发批准/事件版本、真实worker；部分提交崩溃、item_failed恢复、失租回滚/接管与运行中关闭开关 |
| G4 | P2与P1浏览器、静态/导入图、全量离线单元/集成和根目录测试；最终结果见下表 |

## 场景追踪

以下是实际执行模块与断言的映射，不是24个独立测试的计数。一个场景横跨多条断言，一个测试也可能覆盖多个场景。JSON期望文件只作独立输入/期望，不冒充已执行测试。

| 场景 | 实际证据与判定边界 |
| --- | --- |
| F01/F02 | matching类别/个股与精确system/version、CSRC冲突及SW层级；service共享issue分页分组；缺分类/产品地区主体关系unknown，不猜名称 |
| F03/F22 | contracts/evaluation/matching：Decimal、单位/深度、三值嵌套、approx/冲突/行业fallback，旧质量不扩枚举；旧screen/因子随全量回归 |
| F04/F06 | acceptance/lifecycle、service：active与draft、失败/旧验证、reject后新revision、暂停/退役/退出context保留原未决引用；时间到界由current-limits限制 |
| F05 | concurrency：两个真实HTTP批准writer与事件revision writer；一成功一409，并核对持久head/generation/audit/receipt/run只提交一次 |
| F07/F09 | evaluation/source-boundaries：用户报告不得verified；事实、公司暴露和影响分离；无逐规则评审不reviewed/resolved，不宣称有通用可信政策适配 |
| F08 | service相关新event使当前结果stale并列unbound；无关event不使该主体滥失效；event revision受cutoff与owner限制 |
| F10/F23 | inputs/source-boundaries：严格datetime、晚捕获/backfill/date精度、可变行及后来登记的旧日期事件；冻结原件而不声称可任意重建过去 |
| F11/F18 | acceptance/commands：同key收据/异body或P1命令撞key、受控exact refs、execution mode/manifest篡改、history UTC、validation fail提交诊断；默认关闭仍可重放原收据 |
| F12/F19 | acceptance与service：两个run同前序冲突；首项提交后崩溃/失租，应用和检查点原子回滚；实际ResearchWorker接管；终结item_failed不重试；replay不推进live链 |
| F13 | inputs/contracts/service/run：零成员内部run、来源cap/4MiB、未绑定事件、旧issue、单项失败；coverage pending/mixed不冒充完整安全 |
| F14 | migration_051及历史migration_050：P1数据不改，SQL/索引/触发器和升级降级守卫，051独立固定DDL不借当前ORM重建历史迁移 |
| F15/F21 | api_research/acceptance/Web：通用all/显式P2 kind/详情均挡raw manifest；专用接口鉴权，关闭仍精确读；无新模型批准工具，不把operator凭据说成真人认证 |
| F16 | supplemental_rules.mjs和Web Node专项：真实HTTP管理/单股，丢回执、受理后存储失败与reload、run独立跟踪、迟到响应、精确引用、分组分页、1440/390；P1账户排序/单股浏览器另跑 |
| F17/F20 | service与acceptance：revision/issue_key/context变化仍同series、跨run/as_of mixed、事件/规则registry cursor失效、固定ordinal批次分页；GET不产生数据库写入 |
| F24 | service/acceptance：精确原run/manifest/time授权回放、mode和摘要篡改拒绝、不支持方法拒绝、源删后冻结回放保留原件；原live链保持 |

## 验证记录

| 主代理验证 | 结果 | 原始记录 |
| --- | --- | --- |
| 全量单元 `pytest -q tests/unit` | 2408 passed、2 skipped、6 network按默认标记排除；417.61秒；2410个JUnit testcase/177模块、身份无重复 | `/tmp/zcharlie-p2-final-unit.Fxmi8J/unit.log`、`unit.xml` |
| 全量集成 `pytest -q tests/integration` | 716 passed；无失败/错误/跳过/排除；712.76秒；716个唯一JUnit testcase/127模块；包含本地gateway契约 | `/tmp/zcharlie-p2-final-integration.RKUuyn/integration.log`、`integration.xml` |
| 根目录 `pytest -q tests/test_*.py` | 717 passed；156.34秒；717个唯一JUnit testcase/37模块 | `/tmp/zcharlie-p2-root.5trTfq/root.log`、`root.xml` |
| 最终专项及导入图 | 114 passed；32.67秒；含P2 unit/integration、051、旧Web财报兼容、research旁路、worker注册及导入图 | `/tmp/zcharlie-p2-final-closure.log`、`/tmp/zcharlie-p2-final-closure.xml` |
| 静态 | Ruff `src tests alembic`、mypy全部426源文件、两份P2 JS语法、`git diff --check`通过；051单head | 最终代码执行 |
| 真实浏览器 | 主代理最终分别串行运行P2、P1脚本，两者1440/390均通过；P2覆盖双存储失败reload、服务端run身份、ABA、分页和实际样式 | `/tmp/zcharlie-p2-browser-final-p2.log`、`/tmp/zcharlie-p2-browser-final-p1.log` |

所有测试使用惰性假key、独立临时DB/DATA_DIR；integration的`ZCLAUDIA_GATEWAY_DIR`指本机相邻仓库，并非部署网关。2个skip分别缺少clean checkout不携带的Costco/HSBC原始captured材料，未伪造替代；6个network排除来自仓库默认pytest配置。全量单元开始收集后新增了来源删除/冻结回放、不可用方法版本两项独立反例，且最后收敛了分类不确定性及UI恢复；最终114项专项覆盖这些最终边界和实际JS。完整分区与后补专项的时间边界分开记录，不把旧进程已导入的代码冒充最终快照。

最后保留工作区并行mobile与Web嵌入返回交互的agent.js/shell.js修改，未纳入P2文件责任；针对这些共存边界另补Web client契约45 passed（8.00秒，`/tmp/zcharlie-p2-parallel-web.log`、`/tmp/zcharlie-p2-parallel-web.xml`）。测试生成的既有P1截图恢复，仅保留单独P2证据。

上述专项与完整分区有重复，不累加。首轮专项106、导入图8及子代理74/78等用于定位和开发，不另计总体通过数。

首轮全量unit为2400 passed/2 failed/2 skipped/6 network排除；两处固定历史夹具误用当前updated_at，已固定夹具而不放松未来知识阻断。首轮integration为708 passed/3 failed：严格事件hash/来源夹具、运行期间修订的UI恢复断言、以及新面板误进入HK/US页面。后者已按A股边界修复，另补单股样式引用和computed-style断言。首轮P1浏览器desktop通过、390布局变化时点击落在form背景，按实际就绪状态和稳定位置修正测试同步。主代理一次组合复跑与子代理P2临时端口重叠而bind失败，确认进程退出后分别串行重跑P2/P1，两条命令均exit=0；不把端口冲突当作通过，也不掩盖失败记录。所有首轮失败均保留记录，不被最终数字覆盖。

## 能力限制与启用边界

- 通用监管政策原件核验、公司产品/地区暴露关系及逐规则正式解除评审尚无可靠现成适配；公开事件仅user_report，这些输入保持pending/unknown。合成fixture不是生产来源就绪证明。
- 每次100个listing、2,000个pair及4MiB均需满足。真实来源形状扩展探针2,000pair为7,115,544字节，因此先触字节上限，不能承诺满2,000pair都可接收；超限明确拒绝，不截断。
- current为各series最新live集合，可能mixed；分组只展示与当前页相交的事项，但保留全范围每条basis及全部精确应用/规则/事件引用。不同severity或影响推断分别显示，不选第一条代替整体、不累加分数。
- P1账户适配/候选排序及正式公司Recommendation未接P2，P3/P4不在本轮。三个相关功能生产默认仍false；测试开启只限临时进程/数据库。未对真实公司/政策给出新结论。
