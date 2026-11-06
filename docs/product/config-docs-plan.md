# 配置文档化：字段来源 / 来源策略 / 行业分组 / 模板起草进 DB 与专属 UI

状态：Accepted 2026-11-01（决策见 §2）

## 1. 背景

settings-plan 落地后，运行时参数与调度已 DB 化并由 `/app/settings` 管理。对剩余
静态配置的排查（2026-11-01）结论：`config/` 下没有任何文件可被 API/UI 写入，其中
四项仍是"改文件 + git 提交 + 重启三个进程"：

- `config/field_sources.yaml`：字段来源优先级（primary / fallback / tolerance /
  key），影响每一次因子解析与行情 API（约 15 处消费；`factors/field_sources.py:388`
  起 `lru_cache`，`api/views.py:112` 有模块级 `BARS_PROVIDERS` 常量）；
- `config/research_material/source_policy.yaml`：港美来源许可与技术验收审批记录，
  策略哈希进审计（`supplemental_rules/publication_limits.py:225`）；
- `config/industry_groups.yaml`：行业 → 分组映射，决定规则模板适用白名单
  （`rules/validation.py:140`）与因子适用性；
- `config/rule_templates/*.yaml`：模板唯一作者席，UI 只能浏览；rule store 已按
  `(id, version)` 不可变版本化（`storage/models/rules.py`），缺的只是编辑入口。

同时有四类**不适合**进 UI：校准证据（definition / calibration / sanity，研究审计
记录）、参考数据（shenwan 目录，再生成而非手编）、种子数据（pilot_list、methods
基线）、部署资源（日历基线、启动层 env，S-D1 已有决策）。

## 2. 决策

| # | 决策 | 结论 |
|---|---|---|
| CD1 | 范围分诊 | 业务策略面进 DB/UI；校准证据面 UI 只读；参考数据只把"刷新操作"搬进 UI；种子/启动层留文件 |
| CD2 | 复用先例 | 不新建机制：存储 / revision / 乐观锁 / 历史 / 首次导入沿用 settings 实现；结构化文档编辑沿用 `llm_catalog` 先例 |
| CD3 | 解析即校验 | 每个 kind 复用现有加载器（`FieldSourceRegistry` / `IndustryGroupMap` / `load_source_policy`）做保存校验，不另写一套校验模型 |
| CD4 | revision 联动 | 配置文档写入递增既有 `app_setting_meta.revision`；worker 认领前比对、scheduler 60 s 轮询重建的传播机制零改动 |
| CD5 | 文件降级为基线 | 与 `app.yaml` 同语义：首次启动导入一次，之后 DB 唯一真相；基线与 DB 漂移打 warning；提供显式"从基线导入"（带 diff） |
| CD6 | 模板作者席 | rule_templates 在现有不可变版本机制上加"起草新版本 / 激活"；`file_status` 翻转为"DB 当前版 vs 镜像基线"漂移提示 |
| CD7 | 不变式 | industry_groups 变更只影响未来计算（已存快照不动）；source_policy 哈希改对 DB 规范 JSON 取值；全部变更有 actor + 必填备注 |

## 3. 范围清单

### 3.1 进 DB 与 UI 的配置文档

| kind | 来源文件 | 可编辑面 | 生效 | 页面 |
|---|---|---|---|---|
| field_sources | `config/field_sources.yaml` | 调度面（primary / fallback / tolerance / key）表单 + 全文档高级编辑 | next_job（API 读即时） | `/app/data-sources` 标签 A |
| source_policy | `config/research_material/source_policy.yaml` | 全部（审批表单） | next_job | `/app/data-sources` 标签 B |
| industry_groups | `config/industry_groups.yaml` | 分组定义 + 映射表 | live | `/app/industry-groups` |
| rule_templates | `config/rule_templates/*.yaml` | 起草新版本 / 激活（存 rule store，不走 `config_document` 表） | 激活即生效 | `/app/templates` 扩展 |

### 3.2 明确不搬（保持文件）

| 项 | 理由 |
|---|---|
| field_sources 的 definition / calibration / sanity | 校准证据是研究审计记录（"新来源必须先校准"），属版本控制；UI 只读展示 |
| `config/shenwan_2021.yaml` 内容 | 参考数据由 `tools/refresh_shenwan_directory.py` 再生成；仅把刷新操作任务化（WP5，可选） |
| `config/research_material/pilot_list.yaml` | 身份种子，导入后为不可变 `ResearchSourceBinding`，UI 编辑破坏审计链 |
| `config/methods/*.yaml` | 治理已在 UI（提案 / 评估 / 决策 / 回滚），文件是仓库基线 |
| 日历基线 / `a_share_calendar_path` / 启动层 env | S-D1 与 `app.yaml` 头注已决：部署资源与启动层 |

## 4. 设计

### 4.1 代码结构（新包 `investment_agent/config_docs/`）

- `registry.py`：`ConfigDocSpec`（kind、标题、默认文件路径、`parse`、生效方式）
  与 `KINDS` 注册表。
- `store.py`：`ConfigDocStore`——读取（DB ⊕ 基线文件缺省）、写入（parse 校验 +
  单事务 + 递增 revision + 历史）、revision 查询、首次导入、基线 diff。
- `loader.py`：`config_doc(kind)` 供消费方取解析结果；内部 revision 键控 memoize。

### 4.2 存储（一次 migration）

- `config_document(kind PK, value_json, updated_at, updated_by, imported_at)`
- `config_document_change(id, kind, old_value_json, new_value_json, actor, note, changed_at)`
- 模板不新增表：直接写 rule store 的 `rule_template` 下一版本。

### 4.3 首次导入与漂移

- 启动时 `ensure_imported()`（与 settings 同模式）：基线文件解析成功才导入，
  仅导入一次。
- 三个 YAML 文件头加注释"本文件为首次导入基线，运行时以 /app/… 为准"。
- 启动时基线与 DB 不同 → 一条 warning 列出各 kind 漂移（对应
  `settings.env_ignored` 模式）；"从基线重新导入"必须显式触发并展示 diff。

### 4.4 生效与缓存

- 写入递增 `app_setting_meta.revision`（settings 的既有单号）→ worker 认领前
  比对、scheduler 每 60 s 轮询重建的代码零改动；settings 消费方多一次无害重建。
- `factors/field_sources.py:388`、`factors/industry_groups.py:248` 的
  `lru_cache` 改为 `(revision, 解析结果)` memoize；`api/views.py:112` 的模块级
  `BARS_PROVIDERS` 常量改为函数。
- source_policy 每次任务读取，无缓存问题。

### 4.5 API（全部 `verify_api_key_or_session`）

- `GET /config-docs`：各 kind revision / updated_at / 基线漂移状态
- `GET /config-docs/{kind}`：当前值 + revision + 编辑元数据
- `PATCH /config-docs/{kind}`：`{value, note, expected_revision}`；422 逐项校验
  问题、409 冲突；note 必填
- `GET /config-docs/{kind}/history`
- `POST /config-docs/{kind}/baseline-import`
- `POST /config-docs/industry-groups/coverage-check`：复用现有 coverage 校验
- `POST /rule-templates`、`POST /rule-templates/{id}/activate`（模板起草/激活）

### 4.6 Web UI

- `/app/data-sources` 标签 A 字段来源优先级：按 `table` 分组的字段表；每行
  primary 下拉、fallback 有序拖拽、tolerance、key 勾选；definition / 校准徽章 /
  sanity 只读（证据面不可改）；"高级编辑"折叠区为全文档文本编辑器（沿用
  `settings_llm_catalog.js` 组件模式），保存跑完整解析校验；版本号、基线 diff
  横幅、历史抽屉。
- `/app/data-sources` 标签 B 来源信任策略：每来源一卡——licensing 状态、
  company_keys、citation、technical_acceptance（状态、adapter_version、
  reports）；revision id 展示；变更备注强制。
- `/app/industry-groups`：分组定义编辑 + CSRC 码 → 分组映射表（搜索 / 过滤 /
  批量改组）+ 申万覆写；"覆盖率检查"按钮（CSI300 成分全覆盖、申万一级有默认、
  覆写引用目录内代码），未映射高亮 + 快速指派；保存自动 bump `version`。
- `/app/templates` 扩展：详情页"编辑新版本"（结构化文本字段 + screen 段 YAML
  编辑器），保存 = 下一不可变版本；校验复用 `rules/validation.py`
  （`applicable_groups` 对照 DB 版分组）；列表页 `file_status` 翻转为基线漂移 +
  "从基线导入"。
- 约束同现有页：无内联 style（CSP）、DOM 只经 dom.js 构造、中文文案。

### 4.7 CLI

`ia config-docs list | show KIND | import [--force] | history`：无头环境等价物；
编辑主路径是 UI。

### 4.8 可复现性与审计

- research run 元数据记录所用 field_sources 文档 version / 哈希。
- `publication_limits` 的策略哈希改为对 `config_document` 规范 JSON 取哈希，
  语义不变。
- `config_document_change` 全量留痕（old / new + actor + note）。

## 5. 任务拆分

| 任务 | 内容 | 依赖 |
|---|---|---|
| WP1 | config_docs 包（registry / store / loader）、migration、revision 联动、缓存改造（含 `BARS_PROVIDERS`）、field_sources 接入 + `/app/data-sources` 标签 A + run 记录文档版本 | — |
| WP2 | source_policy 接入 + 标签 B + 哈希连续 | WP1 |
| WP3 | industry_groups 接入 + `/app/industry-groups` + coverage-check + PIT 测试 | WP1 |
| WP4 | 模板起草 / 激活 UI + `file_status` 语义翻转 + 从基线导入 | WP3 |
| WP5（可选） | shenwan 刷新任务化（jobs 入队 + `DATA_DIR` 副本 + 未映射码报告） | WP3 |
| WP6 | 文档：operations.md、README、三个 YAML 文件头注释 | WP1–WP4 |

## 6. 验收

- 新库启动：三个 YAML 被导入且 UI 显示与文件一致；再次启动不重复导入；修改基线
  文件后启动出现漂移 warning。
- UI 改某字段 fallback 顺序 → API 因子解析下次读取即用新顺序，worker 下一任务
  生效，均无需重启；"改配置 → worker 重建"有端到端测试。
- 改行业分组映射 → 新的模板筛选 / 校验立即用新映射，已存快照的行业分组不变；
  覆盖率检查能发现并高亮未映射的 CSI300 代码。
- 模板在 UI 起草新版本 → 校验失败给逐项问题；激活后 screen run 使用新版本；
  从基线导入展示 diff。
- 高级编辑粘贴非法 YAML / 未知字段 → 保存被拒（解析即校验）。
- 所有写操作留 actor + note 历史；source_policy 哈希在 publication_limits 连续。
- 全量测试通过；UI 文案遵循现有约束（无交易用语、CSP 合规）。
