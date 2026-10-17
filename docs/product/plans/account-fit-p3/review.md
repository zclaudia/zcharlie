# P3 主代理整合复审记录

日期：2026-10-17（Asia/Shanghai）。范围：glm-5.3-flash 子代理四批分项实现（foundations 01–03、closure 04–06、consumers 07–09、界面 10）之后，主代理对共享接缝的逐段复读、冲突裁决与修复验收。依据[分层设计](../../account-fit-and-supplemental-rules-design.md)、[实施计划](../account-fit-p3-plan.md)与[冻结记录](p3-freeze.md)。本轮不重复子代理已记录的任务级验收，只列跨任务接缝、发现与裁决。

## 共享接缝复读结论（均经主代理亲自读码确认）

| 接缝 | 结论 |
| --- | --- |
| publish write() 闭环段（investor/publication.py L611-740） | check(rule_context)→current_limits∪rule_limits→落 review→closure 在 `begin_nested()` 保存点（异常回滚转 check 错误）→check_ref 落库→失败落 draft→成功才分配确定性 published manifest UUID 并附加服务端 critical 依赖→依赖行/事件/case CAS/outbox。单事务语义、失败保持未决、`limits_recheck` 正常发布为空——与计划 3.4 一致 |
| check() 反伪造（L364-367、L414-485） | 无绑定而 semantic 自报 coverage→错误；有绑定要求精确 manifest/hash/binding_ref/材质 hash 镜像；逐规则评审完整性按冻结判定；directional 禁 unknown/unbound_new 且 open issues 必须闭合 |
| 读取路径（`_dependency_limits` L925-929） | rule_coverage 分支解析 published 行并带 `at` 时点委托 `_rule_coverage_limits`；get_recommendation 既有 BEGIN 快照内执行；扩展/维护路径共用同一重核 |
| admit 钩子（agent/execution.py::bind_research_rules） | 首次受理、freeze_task 之后同事务；单主体边界与 freeze_task 一致；binder 失败→`ExecutionDeniedError` 不静默降级；重 admission 复用原绑定（task_id 唯一+确定性 UUID） |
| 整案门槛（plans.py L187/L385-387） | `_buy_fit_limits` 只作用于 buy/add；reduce/exit/wait 零新增前置；开关关闭逐字节（既有 ag23 测试不加开关全绿） |
| 052 迁移与降级 | 三类 P3 数据守卫（rule_coverage 行、binding 行、PublicationDependency 引用）各自拒绝降级；050/051 文件未改写；单 head 052 |
| 导入方向 | investor 层零 supplemental import；closure/published 构造落仓储层（storage→supplemental 合法边）；新包对包边仅 agent→supplemental（bind 钩子所需），import graph 基线已审阅更新 |

## 发现、裁决与修复

| 编号 | 发现/裁决 | 处置 |
| --- | --- | --- |
| R1 | AF-P3-06 交付的 `list_latest_live_rule_application_payloads` 关联子查询未用 `aliased`：同 series 多 application 时被替代行仍可读为 latest-live，会让 closure 前序 CAS 与 coverage 读取取错前序 | AF-P3-07 发现并修复（aliased+确定性排序），补同序列多版本反例；06 全部既有测试复跑仍绿 |
| R2 | 任务书要求 closure 构造放 `supplemental_rules/service.py` 且 `_dependency_limits` 直调 coverage 函数，与 p3-freeze「investor 禁止 import supplemental_rules」红线冲突 | 裁决：语义不变、落点移至仓储层（`save_publication_closure`/`save_published_rule_coverage`），investor 侧经仓储间接调用；两侧谓词在 AF-P3-07 收敛为 `publication_limits.coverage_limit_reasons` 单实现 |
| R3 | 计划页面文案「不构成买入建议」与既有 UI 守卫（禁「买入」二字）冲突 | 裁决：文案升级为语义更强的「不构成买卖建议」，六处 UI 与断言同步，守卫不削弱 |
| R4 | P3 契约面使评审请求序列化每请求增约 2KB，令测试夹具 `investor_agent_task_tokens=100000` 落入 finish_only 储备翻转线、末轮工具被隐藏 | 裁决：仅测试支撑夹具提至 200000 并注释根因；生产预算与断言语义不变；生产化前应按真实形状重估预算（记入遗留） |
| R5 | 浏览器运行会重写既有 P1/P2 探针截图 | 两次恢复 P1/P2 截图至归档版本；P3 仅保留自己的 5 张合成截图（bound 仅 1440、revisit/legacy 双档为脚本有意设计） |
| R6 | 环境缺口两处：网关契约 4 项需 `ZCLAUDIA_GATEWAY_DIR`；根目录 `test_method_evolution.py` 需 `API_KEY`（缺省时 Settings 校验失败） | 均为本机环境依赖非代码回归；最终全量带 `API_KEY=test-key` 与网关目录执行；与既有测试门禁惯例（惰性凭据覆盖）一致 |
| R7 | `RuleCoverageManifest` 需要显式 `task_id` 字段承载 052 部分表达式索引路径（计划 DTO 清单遗漏） | 按冻结记录补充；服务层填充并经迁移测试固定 |
| R8 | 真值表口径差异：financial warning 是否可 ready | 裁决沿设计 6.2：warning→limited，永不 ready；DTO 校验仅约束 ready/limited⊆{pass,warning} |
| R9 | 开关开启但 coverage 未发布的 legacy 主体 buy/add 被整案拒绝 | 裁决保留严格语义：缺失≠零规则，需新研究重发布；与设计「缺少覆盖必须 unknown/incomplete」一致 |

## 与计划的偏离汇总（语义保持，均已记录）

1. closure/published 构造落仓储层而非 service.py（R2，导入方向）。
2. `RuleCoverageManifest.task_id` 显式字段（R7，索引承载）。
3. RuleReviewItem 的 proposal_hash 严格双向配对（冻结记录，强于计划单侧要求）。
4. `test_api_supplemental_rules.py` 计划名不存在，P2 对应断言实际在 `test_supplemental_acceptance.py`（按现实执行）。
5. `admission_as_of` 锚定 `task.binding.as_of` 而非钩子墙钟（保证与 prepare cutoff 的时序恒成立）。
6. 未知匹配在 binding 层以 `matching_manifest_hash` 摘要表达、在 coverage 条目层以显式 status 表达（BindingRuleEntry 无状态字段）。

未发现需要放宽 P2 语义的修改；P2 机械禁令、不可变守卫、只读快照与收据边界全部保留并有负例回归。
