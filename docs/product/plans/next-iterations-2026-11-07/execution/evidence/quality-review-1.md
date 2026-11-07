# 质量方向第一批审查与回修

范围：QT001–QT010，不包含尚未实现的实验运行、盲评、API与页面。主要作者为 implement_research_entry；root 审查其合同、兼容性、fixture、ORM、仓储与事务测试。root 编写的 QT007 质量制品保护、迁移和设置由另一作者交叉审查，见 [迁移审查](quality-review-1-migration.md)。未运行真实 E2E 或真实模型。

## 发现及关闭

| 编号 | 严重度 | 触发与影响 | 原作者修复及独立复验 |
|---|---|---|---|
| Q1-01 | P1 | 原合成 snapshot 数字是字符串；原 materializer 只接受整数，错误 fixture 可让元数据 verified 而数值为空 | QT003 的原生运行链发现；作者改严格整数并升级 dataset v1.1 身份/hash，保留旧版本失效说明。root 复跑12项 dataset 检查，使用真实 EvidenceService/NumericResolver 读取54个已存材料；缺反证仍由原validator拒绝 |
| Q1-02 | P1 | 普通 MethodArtifact 行执行 UPDATE OR REPLACE，把 UUID 或隐藏 rowid 指向 quality 行；recursive_triggers OFF 时 quality 行可能被静默删除 | root 在模型和065的 UPDATE guard 增加冲突受害行检查，新增六种kind×UUID/rowid反例。非作者独立验证 UUID/rowid/_rowid_/oid 均拒绝、旧普通行行为保持；11项迁移/设置通过。见交叉审查记录 |

无剩余阻断项。QT010 回修闭合，允许进入冻结材料与生命周期模块；这不是完整实验执行批准或研究质量结论。

## 核对与证据

- QualityArtifactRef 与财务 ObjectRef 分离，blind_map 独立私有类型；公开读取必须匹配kind、owner、UUID、revision和hash。
- 原提案、admit、步骤验证和反证读取真实经过项目服务；没有降低验证器或移动原方法head。QT003 的成功/失败与原错误码见 [兼容记录](quality-compatibility.md)。
- 五表索引与历史触发器；receipt先验证当前权限和请求对象，再重放原状态/refs。SQLite真实外层写事务先于SAVEPOINT，回滚不残留制品、CAS或准入事实。
- `test_quality_storage.py`以原普通解释Task验证同一外层事务的Task/grant/method binding/BudgetEntry与质量索引全成全无；它没有伪装成尚未交付的实验admit。
- 两开关独立默认false，runtime字段/注册与bootstrap一致。065仅新建本批结构及窄保护；含质量历史或制品时拒绝破坏性downgrade，旧普通MethodArtifact保留。
- Evals 2.31.1薄适配只评已保存fixture，不包裹Task重试或调用模型，见 [适配记录](quality-evals-spike.md)。

主代理命令：`.venv/bin/python scripts/check_next_iterations.py --scope QT-storage --offline`，31 passed（9.06s）。Q1-02回修后主代理再跑 `tests/integration/test_quality_migrations.py tests/unit/test_quality_settings.py`，11 passed（2.87s）；非作者11 passed（2.63s）。相关旧settings结构检查连同仓储/模型共45 passed，旧dotenv专项读取被offline guard阻断，明确排除该无关测试，不改测试或放宽隔离。

尚未验证：封存材料的实际隔离、实验双臂准入/执行、预算故障恢复、污染栅栏、盲评与报告；后续批次分别验收。当前制品表的泛型存储接口不等同于服务端执行权限。
