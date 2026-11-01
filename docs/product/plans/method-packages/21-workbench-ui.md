# 卡 21:方法工作台前端(四块渲染、候选影子操作、全局默认 head)

设计依据:§9、§12 开放问题 4。前置:卡 20。改动仅限
`web/templates/methods.html`、`web/static/methods.js`、`web/static/pages/methods.css`;
后端 API 不动。config-only 提案的现有 UI 流程保持不变。

## 1. 包提案详情(渲染 §9 四块 + claims)

包提案卡片(概要已带 `package_key`)在既有 evidence 区之后追加:

- **candidate**:包 key@version、content_hash 前 8、discipline 开关摘要。
- **structure_diff**:按 `new_blocking_steps / outputs / dimension_minimums /
  depends_on / discipline / gap_tightenings` 分组列 id,`old_package_kind` 标注
  旧方法形态(legacy/package);`truncated` 时显示「仅展示前 N 条」。
- **replay**:逐样本一行(角色/适用/三态),`not_replayable` 项折叠列出;
  顶部汇总 `requires_candidate_shadow` 待覆盖项。
- **candidate_shadows**:已登记影子(task_id、expected、outcome、matched、覆盖项);
  `candidate_shadow_claims` 中未 registered 的 claim 显示任务号与「登记结果」按钮。

## 2. 候选影子操作入口(包提案 `status="shadow"` 且评估通过)

- 单发:prompt 输入 + expected(pass/block)选择 +「发起候选影子任务」按钮 →
  `POST /investor/methods/proposals/{id}/candidate-shadow`(randomUUID mutation)。
- 批量:多行 prompt 文本域(每行一条,≤4)+ expected 选择 +「批量发起」→
  `POST .../candidate-shadow-batch`。
- 结果登记:每个未 registered claim 一键 `POST /investor/methods/candidate-shadow-results`
  (task_id + expected_revision 取当前 detail 的 proposal.revision)。
- 提示文案:影子任务是真实 worker 任务、占用研究预算、无发布/通知/账本副作用;
  离线影子(既有按钮)不能替代候选影子。

## 3. 包提案批准入口

批准按钮出现条件从「评估通过 && 前瞻影子通过」(config-only)扩展为包提案的
「评估通过 && 最新 matched 正例覆盖全部 requires_candidate_shadow && 存在 matched
反例」(按 detail 的 replay/candidate_shadows 数据镜像展示;服务端仍为权威,
失败按 409 文案提示)。批准/拒绝仍走既有 `/decisions`。

## 4. 全局默认方法 head

`list_methods` 的 `default_head` 有值时在「方法版本记录」区显示
「全局默认方法(company-research)· revision N · 包 key@version」;为空时显示
「未设置:新公司研究/复核任务使用 legacy v1 基线」。per-listing 回退区保持现状。

## 5. 包提案创建入口

第 2 步表单新增可选「候选包 YAML」文本域:填写时提交 `{..., package_yaml}`
(config 不发,由服务端以 discipline 填充),三个开关行隐藏;留空走既有
config-only 表单。提交前本地校验行数与空值,失败用 `method-status` 提示。

## 验收

- `ruff check .`、`mypy`(web 静态文件不在 mypy 范围)、全量相关 pytest 不受影响。
- 主 Agent 以浏览器实测:本地起 api,造一个包提案(测试脚本或直接调 API),
  逐块核对渲染与操作入口;config-only 页面回归不变。
