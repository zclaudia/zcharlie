# UI v2 落地实现计划

2026-10-14。设计稿：桌面 [ui-v2-mockup.html](ui-v2-mockup.html)，移动端 [ui-v2-mobile-mockup.html](ui-v2-mobile-mockup.html)（走查截图在 `mobile-audit/`）。

已定决策：
- 桌面：左侧分组侧栏 + 顶栏（面包屑 / 全局搜索 / 主题切换），内容区最宽 1280px，AI 页全宽。
- 移动端：底部 4 栏 **今日 / 漏斗 / AI / 更多**。漏斗页顶部分段切换 候选 / 观察 / 持仓；其余系统页收进「更多」底部弹层。
- AI 页底部 4 栏常驻，键盘弹起（输入框聚焦）时隐藏。
- 驳回 / 忽略理由弹层只放文本框，**不做**常用理由胶囊（未定）。
- 色彩语义不变：蓝 = 主信息，橙 = 需注意（唯一暖色），紫 = AI 生成，红涨绿跌只用于价格。

## 分阶段与归属

所有子任务在 `main` 上直接改，不各自提交；每个子任务完成后由主会话审 diff 并提交。

| 阶段 | 子任务 | 负责文件（只改这些） |
|---|---|---|
| 1 | **T1 外壳与基础** | `app.css`、`base.html`、`login.html`、`error.html`、`nav.js`、新增 `shell.js`、新增 `static/pages/*.css`（建空文件 + 搬迁现有页面规则）、各模板里引入页面 CSS 的那一行 |
| 2 | **T2 概览 · 运行中心 · 单股** | `overview.html`、`runs.html`、`stock.html`、`app.js`、`pricechart.js`、`financials.js`、`industry_peers.css`、`industry_peers.js`（仅样式相关）、`pages/overview.css`、`pages/runs.css`、`pages/stock.css` |
| 2 | **T3 候选池 · 候选详情 · 模板** | `candidates.html`、`candidate.html`、`templates.html`、`template.html`、`candidates.js`、`templates.js`、`pages/candidates.css`、`pages/templates.css` |
| 2 | **T4 观察池 · 持仓池** | `watchlist.html`、`portfolio.html`、`watchlist.js`、`portfolio.js`、`rule_review.js`、`pages/watchlist.css`、`pages/portfolio.css` |
| 2 | **T5 数据健康 · 研究方法 · 设置** | `health.html`、`methods.html`、`settings.html`、`health.js`、`methods.js`、`settings.js`、`pages/health.css`、`pages/methods.css`、`pages/settings.css` |
| 2 | **T6 AI 页** | `agent.html`、`agent.js`、`agent_controls.js`、`agent_views.js`、`agent_portfolio.js`、`pages/agent.css` |
| 3 | **T7 收尾** | 清理 `app.css` 死规则、跨页一致性走查、全量 Python + 浏览器测试、文档 |

**共享文件只读**（阶段 2 任何子任务都不改）：`app.css`、`base.html`、`shell.js`、`nav.js`、`ui.js`、`format.js`、`dom.js`、`api.js`、`combobox.js`、`research_format.js`。阶段 2 发现共享层缺东西，在报告里写明需求，由主会话 / T7 补。

## T1 交付的公共约定（阶段 2 依赖）

- **Token**：v2 token 写在 `app.css` 的 `:root`（亮 / 暗两套，暗色同时支持 `prefers-color-scheme` 和 `[data-theme="dark"]`）。旧名（`--c-*`、`--sp-*`、`--r-*`、`--fs-*`、`--bg`、`--surface`…）保留为别名，`industry_peers.css` 依赖它们。
- **组件类**（名字以 T1 最终实现为准，T1 在 `app.css` 顶部注释列出清单）：卡片、页头、KPI 条、徽章（中性 / 主信息 / 需注意 / AI × 实心 / 描边 / 圆点）、按钮（主要 / 次要 / 幽灵 / 危险幽灵，尺寸 sm / md / lg）、分段控件、下划线标签页、横滑胶囊、表格、提示条、空状态、列表行（移动端两行列表）、底部弹层 / 右侧抽屉、底部固定操作条。
- **页面 CSS**：`base.html` 提供 `{% block page_css %}`；每个页面模板引入自己的 `/app/static/pages/<page>.css`。
- **移动端外壳**（< 56rem）：顶部 app bar（标题 + `{% block appbar_actions %}`）、底部 4 栏；详情页（单股、候选详情、模板详情）用 `{% block mobile_back %}` 提供返回按钮并隐藏底部 4 栏。漏斗分段控件是可 include 的片段，候选 / 观察 / 持仓页移动端使用。
- **底部遮挡**：CSS 变量 `--tabbar-h`（无底部栏时为 0），固定在底部的元素用 `bottom: calc(var(--tabbar-h) + …)`。
- **AI 页兼容**：`agent.js` 会调用 `.nav-toggle` 的 click 打开站点菜单，T1 保留一个 `.nav-toggle`（打开「更多」弹层）。

## 测试与验收约束（全部子任务）

完整清单见本次走查记录；最容易踩的：

- 不准出现 `<style`、` style=`、`onclick=`、内联 `<script>`；JS 里不准有 `.style`、`innerHTML`、`http://`、`https://`。CSP 只允许 `'self'`，不能用外部字体 / 图片 / `data:` URI。
- 侧栏当前链接必须保持精确形式 `<a href="X" class="active" aria-current="page">`（属性顺序、class 只有 `active`）。
- 页面文案保留：`今日概览`、`>设置<`、`研究辅助，非投资建议`（每页，包括错误页）、`<noscript>`、`data-state="loading"`。
- 模板与 JS 不准出现交易词（买入 / 卖出 / 建仓 / 加仓 / 减仓 / 清仓 / 止损 / 抄底 / 满仓 / 割肉；agent / portfolio 文件除外）；A 股单股页 HTML 不准出现子串 `financial`；`candidates.js` / `format.js` / `health.js` / `templates.js` 不准出现 `score`、`strength`；候选页不准出现「拼音」。
- 现有元素 id、`data-*` 钩子、JS 里被测试点名的函数名和字符串一律保留（例如 `app.js` 的 `resolveEvidence`、`healthBucket`；`app.css` 必须字面含 `.combo-list` 和 `.combo-list[hidden]`）。
- 1440 与 390 宽度下 `scrollWidth <= innerWidth`（浏览器测试普遍断言）。

运行：

```bash
.venv/bin/pytest tests/integration/test_web_app.py tests/integration/test_web_client_contract.py tests/integration/test_web_candidates.py tests/integration/test_web_health.py tests/integration/test_web_settings.py tests/integration/test_web_stock_financials.py tests/integration/test_web_stock_peer_section.py tests/integration/test_evidence_navigation.py tests/integration/test_agent_workbench.py tests/integration/test_security_hardening.py tests/integration/test_security_checks.py tests/integration/test_api_rule_templates.py tests/test_method_evolution.py -q
```

```bash
tests/browser/run.sh agent_workbench.mjs
```

浏览器脚本用固定端口，不能并行跑同一个脚本；`agent_methods.mjs` / `agent_purposes.mjs` 会改写 `docs/product/probes/` 下的 PNG，跑完用 `git checkout docs/product/probes` 还原。

视觉验收：共享开发服务 `http://127.0.0.1:8012`（`.claude/launch.json` 的 `zcharlie-ui-dev`，真实数据副本）；截图脚本由主会话提供，桌面 1440×900 + 移动 390×844，同时报告横向溢出和 JS 报错。

## 实施结果（2026-10-14）

各子任务交付：

- **T1 壳层**：侧栏 + 顶栏 + 手机 app bar / 底部 4 栏 / 更多弹层、命令面板、`.sheet` / `.drawer` 弹层、token 与共享组件（`app.css` 顶部注释是组件清单）。
- **T2**：概览、运行中心、单股研究页（A 股）。**T3**：候选池、候选详情、规则模板（列表 / 详情）。**T4**：观察池、持仓池。**T5**：数据健康、研究方法、设置。**T6**：AI 工作台。
- **T7 收口**：把页面各自定义的重复组件并入 `app.css`（股票标识 `.stock-ident` / `.stock-cell`、分数细条 `.tbar` + `.score-inline`、状态点 `.sdot`、无框统计 `.stat`、提醒行 `.attn-*`、抽屉表单 `.drawer-form`、价格区间分段控件 `.pc-tabs`、全局 `[hidden]`）；`stockIdent` / `thinBar` 移到 `ui.js`；删除死规则（`.health-table`、`.add-panel`、`.floating-bar*`、`.inline-form`、`.error-cell`、页面自带的 `[hidden]` 兜底、`.wp-desk` / `.wp-mob` 等）；统一手机页头（静态副标题隐藏，带数据的用 `p.keep`）；`.only-desktop` 不再把 flex / grid 容器改成 block；浅色模式 `--ink-3` 加深、新增 `--warn-ink`（橙色徽章对比度 ≥ 4.5）；手机触控目标补齐（390 宽下可见交互元素 < 32px 的由 61 个降到 0 个有效目标）。

相对设计稿的有意偏差：

- 概览：近期结论显示业务分（运行列表只有 `business_score`）；运行阶段时间线按当前阶段推断（无逐步耗时、无模型 / token 字段）；持仓漏斗只显示账户数；页头「问 AI」降为次要按钮（视图内唯一主按钮是提醒行的「开始审阅」）。
- 候选 / 模板：参数保存不再带可选 reason；列表卡显示草稿状态徽章，仅在未完成时显示「核对未完成」；详情图表保留 7 个区间（共享 pricechart）；提名抽屉成功后保持打开以显示结果链接；页内「返回」链接删除（桌面有面包屑，手机有 app bar 返回）。
- 观察池 / 持仓池：行操作表里没有「立即运行研究」（观察池 JS 没有单股运行 API）；已启用规则的重新起草先显示模板选择；手机用 matchMedia 在 56rem 以下渲染行列表而不是表格；录入事实的提交按钮文案保持「提交」（浏览器测试依赖），抽屉保存后保持打开显示 `#portfolio-fact-result`；总资产无法估值时显示「—」；已全部卖出的持仓仍列出（数量 0，沿用原行为）；桌面观察池表约 960px 宽，< 80rem 隐藏「置信度 / 刷新」列，< 70rem 再隐藏「近 120 日 / 最近变化」列；没有账户时头部「录入事实」降为次要按钮；手机筛选条未做吸顶。
- 数据健康 / 方法 / 设置：健康页「重试」改为「查看」链接（无重试 API）；采集任务筛选未做；手机用原地 `<details>` 而不是二级页面；方法页手机端步骤全部展开（浏览器测试要填步骤 2）。
- AI：任务进度只有 3 段（排队 / 执行 / 完成）；「在运行中心查看」链接到 `/app/runs`（无 run id）；页签横跨整个工作台；AI 页隐藏站点页脚（免责声明在侧栏与输入框下方）；897–1088px 研究栏是浮层；「组合 / 约定与记忆」内容仍是 JS 生成的长段落，只套了卡片样式（「约定与记忆」工具条现在有当前项高亮）。

遗留 / 后续：

- `ui.js` 的 `bar()` 与 `.b-track` / `.b-fill` 已无调用方（未动，等 JS 清理一并删）。
- `pricechart.js` 仍用 `.style` 设刻度位置（CSSOM 不受 CSP 限制，测试只约束 `health.js`）。
- 数据健康 / 研究方法页的页头按钮在手机上仍在页头右侧，其余页面放在 app bar（`appbar_actions`），可统一。
- 浅色模式 `--ink-3` 在 `.kpi.zero` 数值与 `.verify-no` 上约 4.3–4.5:1，大字号可接受。
- 概览 / 观察池 / 运行中心的移动端行列表与 `list-row` 仍是两套两行结构（`.wl-main`、`.run-row`），可再合并。
- 候选卡与候选详情仍有大量沿用 `--c-*` 旧别名的规则，可逐步换成 v2 token。
