# P2契约与资源冻结（G0）

日期：2026-10-16。范围：AF-P2-01。输入为主设计第5.1–5.3节、P2计划第2–3.6节及当前代码探针。本文件固定 P2 的第一版边界；不是生产来源能力声明或生产开关授权。

## 契约冻结

- 一个 `RuleDefinition` 表示不可变内容revision；生命周期、active/draft槽位与验证状态放在head和审计事件。selector判别为listing/company/category；context仅为company_research、candidate、account_fit、holding_review。类别名单须带来源，行业代码连同system与scheme version保存，显式产品/地区名单必须声明完整性。all/any至多两层。
- rule_type仅metric_condition、research_requirement、event_impact；definition为判别联合。效果仅annotate、require_evidence、require_review、limit_conclusion。七个维度复用`investor.contracts.Dimension`。规则的内容/语义hash排除生命周期；series身份固定为owner、listing、规则UUID、context，不包含revision、issue_key或evaluation_purpose。
- 指标比较复用`rules/schema.py`的`< <= > >= == != between in`及all/any形状；不调用旧screen内部求值。P2阈值使用Decimal字符串/整数，禁止float输入。叶子按因子注册信息校验单位和参数。因子来源的float保留hex值、quality、flags、来源与精度；不能证明临界值时结果unknown。all/any遵从计划3.4三值表。
- ResearchEvent是事件业务版本，含policy_stage、核验级别、冲突以及发布/生效/取得/可见时间；公开写入权限只允许user_report。服务端created_at和核验适配器结果控制可见性，客户端时间不得回填。用户材料不能提升verification，事件影响评估中的事实、公司暴露、影响推断分别保存。
- live接收时由服务端选as_of并冻结完整有界listing/context/rule pair、事件、分类和来源快照、前序live未决引用、generation、方法版本及哈希。replay只接受精确P2 run/manifest/input_as_of绑定，不能补今日输入。历史回放不进入live前序链。
- RuleApplication保留不可变app UUID、operation、精确rule/listing/context、generation、manifest/input hash、模式、序列与前序。缺逐规则内容绑定评审时semantic_status只能mechanical或pending，不能resolved。
- CAS冲突为409，形状、非法来源/时间、容量及非法历史绑定为422，错owner/不存在为404，认证后的关闭写入为403。数据缺失写成unknown，不抛503。服务端从认证与已保存主体构造授权上下文，body中的actor/approved/verified不授予权限。

## 来源就绪矩阵

| 输入 | 当前代码与证据 | P2结论 |
| --- | --- | --- |
| 七维枚举、旧ConditionGroup和操作符 | `investor/contracts.py`与`rules/schema.py`已有固定维度和DSL；数值canonical会转float | 已有结构；需独立Decimal DTO/适配，旧hash与screen结果不改 |
| 因子定义/计算 | `factors/registry.py`、`catalog.py`及pure factor计算；FactorValue包含quality、flags、inputs_ref | 已有元数据和计算；需适配严格datetime来源读取。missing/conflict/不可信approx为unknown |
| 财务、行情、分红、逾期 | 有对应只读repository和PIT快照；部分读取器按日终并允许backfill | 需P2专用严格参数/原行适配。当前没有满足全部场景的严格datetime读取接口 |
| 行业分类与company/listing映射 | `IndustryClassification`有system/code和有效区间/captured_at；区间行可变 | 可读但不是不可变revision。冻结实际行、取得时间、内容hash；无法证明覆盖时unknown |
| 已保存证据/材料 | `EvidenceService`可精确读回受控类型，主体与来源有既有校验；ResearchMaterial范围受限 | 仅复用精确读回。不能以`source_ref`或用户批准代替内容/主体/范围核验 |
| 可信监管/业务事件输入 | `evidence/events.py`目前产出业绩预告等candidate；没有通用原件适配 | 通用政策原件适配不可用。保持candidate/user_report/pending，不导入为verified |
| 用户研究事件 | 新增P2 ResearchEventRevision可存user_report、冲突和明确时间 | 已有代码无此业务revision；由P2受控写链新增；不能提升核验级别 |
| 逐规则语义解除评审 | 现有SemanticReview绑定candidate/material/method，不绑定rule/application及解除范围 | 不可用于P2 resolved；缺失时机械结果保留未决 |
| 主体权限 | API key/同源cookie确认operator凭据，不证明真人身份 | 可用于受控本地operator写入；不把body身份或模型工具视为批准能力 |

## 容量与溢出

| 范围 | 第一版上限 | 超限行为 |
| --- | ---: | --- |
| 单次listing数 | 100 | admission前422 |
| 单页 | 100 | 422；cursor固定owner/过滤/快照 |
| 冻结pair数上限 | 2,000 | admission前422；另受manifest字节上限约束 |
| Manifest UTF-8序列化体积 | 4 MiB | admission前422；不截断冻结输入 |
| selector表达式 | 两层all/any；显式成员2,000 | 定义校验失败并保存诊断 |
| 指标组/叶子 | 非空组；最大深度4；最多64叶子 | 定义校验失败并保存诊断 |
| 每叶子参数/`in`成员 | 参数32；`in`值64 | 定义校验失败并保存诊断 |
| 当前规则/事件/既有live series扫描 | 每类最多2,000行 | 规则/series显式评估超限422；事件超限保留coverage_pending；current降为unknown，不报告完整覆盖 |
| 单主体各类原始来源扫描 | 每类最多10,000行 | 读取limit+1探测溢出；受影响因子/分类保持unknown，冻结扫描诊断；不推断未出现即不存在 |
| 自动重评 | 每条变更最多100个listing、2,000个pair | 写入generation并标记未覆盖数量为unknown/已知下界；不因扫描溢出阻断暂停/退役 |

两个冻结上限共同生效，2,000不是可保证全部接收的pair承诺。G2的实际服务生成合法pair后进行只序列化、不写库的扩展探针：100为356,244字节，500为1,779,044字节，1,000为3,557,544字节，2,000为7,115,544字节。因此最大配置的实际来源快照会先触发4 MiB manifest限制；能接收多少pair取决于其来源字段大小，超过任一上限均在admission前明确422，不截断。

`tests/support/supplemental_rules.py::synthetic_manifest_stress_shape`另构造确定性JSON压力形状，序列化为4,183,886字节。它含有synthetic padding，缺少真实rule/subject来源负载，并非合法DTO或高熵模拟；该测量仅用于固定UTF-8排序紧凑序列化路径的回归，不能证明合法manifest容量、2,000-pair可接收性或任意来源原件的上界。实际服务探针中的pair重复保存source snapshot，形成当前实现的体积成本；G0/G2明确接受此容量限制，本期不以共享source pool压缩清单。不得用压缩体积代替真实序列化的UTF-8载荷体积。

实现端应在构造冻结快照前执行行数和pair上限，不能先全库读取再截断。被截断的集合禁止标记完整覆盖。GET对缺行返回零代视图；当上限令实际计数不可知时只报告扫描数下界及pending。2,000条业务对象及每主体每类10,000行原始来源是各自独立的扫描止损线，不是数据库全量规模承诺；G2需以真实SQLite查询与最大合法快照验证耗时、体积、索引和无副作用，再按证据修订此冻结并重跑相关验收。

## 独立样本

`tests/fixtures/supplemental_rules_v1/expected.json`为P2-F01至F24的独立期望，不依赖P2 service。`tests/support/supplemental_rules.py`只载入该文件并产生容量探针。样本全部合成；不据真实公司、政策或产品作结论。核心未知/风险边界为：F02分类与名单覆盖未知、F03质量和三值阈值、F06过期规则保留旧issue、F07事实/暴露/推断分离、F09无逐规则评审不解除、F10/F23时间不倒流、F13空规则不代表无风险、F15权限与开关边界及F24回放精确绑定。

## G0结论

契约形状、时间/核验权限、P3解除边界、固定series身份及独立pair/字节硬上限已冻结；两者取先触发者，pair上限不构成满额接收保证。G2实测显示最大来源快照时4 MiB限制先于2,000 pair触发。尚未提供通用可信政策/业务事件采集，因此这类输入会诚实停留在pending/unknown；P2工程不依赖P3正式发布评审才能运行。查询扫描界限及source snapshot完整性仍按真实存储验收。
