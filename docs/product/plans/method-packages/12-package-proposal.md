# 卡 12:包提案创建与候选冻结

设计依据:§7 首段。前置:卡 11。改动:`investor/methods.py` + `tests/test_method_evolution.py`
(新增用例,不改既有断言)。

## 契约

`CreateProposal` 增加可选 `package: MethodPackageDefinition | None = None`( investor/method_packages
的契约直接复用)。约束:

- `package is not None` 时:`config` 必须逐字段等于 `package.discipline`(违反报
  `invalid_reference`,消息说明两者不一致);package 走 `validate_definition`,错误聚合进
  `InvestorError`。
- 旧方法解析:`old_method` 为 `FIXED_METHOD`(或无 package 的 v1 version)→ 用
  `legacy_package_for(review 里的 old config)`;v2 version → `known()` 读包工件。
- **§8 判定**:`strengthening_violations(old_pkg, package)` 必须为空,且
  `strictly_stronger_requirements` 非空(至少一项研究要求严格增强;仅文案/哈希变化拒绝,
  消息指明「无实质增强」)。config-only 提案(package=None)保持现行 `config.stronger_than`
  路径,行为不变。
- **候选冻结**:通过校验后 `save_package(package, source="proposal", proposal_id=…,
  evidence_hash=None)`(卡 03 已交付的幂等入库),把 `package_ref`、`content_hash`、
  `rendered_method_hash`(经卡 04 `render_method` + sha256)与 `old_package_kind`
  (legacy|package)一并写入 proposal payload 的 `request` 段——候选内容随 proposal version
  冻结,后续 `revise` 不改这些字段;重复 `create` 走既有 `replay` 幂等。

## 测试(`tests/test_method_evolution.py` 新增)

- 合法包提案(在基线包上加一个 blocking 步骤)创建成功,payload 冻结四要素;
  重复 create 幂等返回。
- `config != package.discipline` 拒;违反 §8(删步骤)拒;无实质增强(仅改 description)拒;
  config-only 提案既有路径零变化(现有测试即回归)。

## 验收

三门禁 + 目标测试全绿;`api/routes/methods.py` 无需改动(CreateProposal 已在请求体)。
