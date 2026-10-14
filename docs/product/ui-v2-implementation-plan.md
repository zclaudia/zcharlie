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
