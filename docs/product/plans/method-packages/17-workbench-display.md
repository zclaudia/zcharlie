# 卡 17:工作台展示(结构 diff、回放三态、影子覆盖)

设计依据:§9(提案工作台展示部分)。前置:卡 13、15、16。

## api/routes/methods.py 扩展(只读展示,无新写端点)

1. `proposal_detail`(`evidence()`)对包提案增补:
   - `candidate`:package_key、version、content_hash 前 8、discipline;
   - `structure_diff`:基于卡 11 函数生成的旧→新结构对比(新增 blocking 步骤及 id、
     outputs 增强、维度最低状态变化、依赖新增、discipline 变化;文本限界,照现有
     bounded excerpt 惯例);
   - `replay`:各样本三态与 not_replayable 项、累计 `requires_candidate_shadow`;
   - `candidate_shadows`:每个影子的 task_id、expected、outcome、覆盖项(取自卡 15 工件)。
2. `list_methods` 的 proposals 概要增补 `package_key`(包提案时)。
3. 响应体量:复用现有 2MB 上限与 bounded excerpt 规则;不复制全部冻结工件。

## 测试(`tests/unit/test_method_replay.py` 或工作台测试所在文件扩展)

- 包提案 detail 含四块且字段正确;config-only 提案 detail 形状不变(回归);
- list 概要带 package_key;无泄漏超限(断言响应不含完整渲染指令/原始工件)。

## 验收

三门禁 + 目标测试全绿;不改前端(静态页面本轮不动,展示块给后续 UI 卡用)。
