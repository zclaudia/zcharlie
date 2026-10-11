# 11 财务页面展示

> 波次 W5 ‖ 可与 12 并行。依赖：09 已合并（读模型与 GET/POST 端点）。
> 文件所有权：见 [`README.md`](README.md) §4 第 11 行。本任务独占 `web/**`。

**目标：** 在公司研究页面（现有 `/app/stocks/{code}`）为港美证券增加财务区块：真实财年列示金额、单位、指标定义、原件核对状态与来源日期；区分**资料覆盖**与**核对覆盖**；业务状态与缺口可见；A 股页面渲染不变。

## 代码事实

- 页面路由与 ticker 校验（当前只接受 A 股）：`src/investment_agent/web/routes.py:228-244`（正则 `\d{6}\.(SH|SZ|BJ)`，否则 400）；模板 `web/templates/stock.html`。
- 模板壳与静态资源：`web/templates/base.html`、`web/static/*.js`（ES module，import 必须写 `./x.js`）。
- CSP 与守卫测试：`api/app.py:38-41` 禁内联样式/脚本；`tests/integration/test_web_candidates.py:120-157` 禁 `style=`/`<style`/innerHTML 族/`http://`/`https://` 字面量；新 JS 文件必须加进 `tests/integration/test_web_client_contract.py:106` 的清单。
- DOM/绘图工具：`web/static/dom.js` 的 `el`、`web/static/ui.js` 的 `svg(tag, attrs, text)`（命名空间字符串已拆分）。
- 会话 cookie 验证法（人工/浏览器复核时用，见 `docs` 内既有做法）：自签 `zcharlie_session`，或用 `http://localhost:<port>` 打开临时实例避开既有 host cookie。
- 读模型与能力位：09 的 `materials/context.py`、`GET /research/listings/{listing_id}/financial-context`、`POST .../refresh`（页面可直接调 API，也可由 web 路由服务端渲染；**二选一并说明**）。

## 文件清单

```
修改  src/investment_agent/web/routes.py
修改  src/investment_agent/web/templates/stock.html
修改  src/investment_agent/web/templates/base.html     （仅当需要新导航/样式钩子）
新增  src/investment_agent/web/static/financials.js
修改  src/investment_agent/web/static/app.css          （仅追加财务区块样式）
新增  tests/integration/test_web_stock_financials.py
修改  tests/integration/test_web_client_contract.py    （把新 JS 加进清单）
```

## 实现

1. **代码解析**：`/app/stocks/{code}` 支持港美展示代码（`00002.HK`/`00005.HK`/`00700.HK`/`AAPL`/`COST`/`JPM`/`XOM`/`TSM`）。解析顺序：先试 A 股 `parse_a_share_ticker`（行为与错误文案**完全不变**），失败后用 `domain/security.py` 的 `normalize_ticker` 按市场解析；两者都失败 → 仍是 400 与原文案。找到 listing 后取 `listing_id`。
2. **头部信息**：展示交易所、证券类型（普通股/ADS）、报价币种、报表币种（四者分开，不把 `Market.currency` 当报表币种）。
3. **财务区块**（仅港美 listing 渲染；A 股渲染路径不改）：
   - 表头按**真实财年**（来自 `fiscal_years`，可显示 `label` 与 `days`，非 12-31 的财年照实显示）。
   - 行：指标/事实，单元格给 `金额 + 单位 + 币种`；缺失显示"缺失 + 原因"，不显示 0。
   - 每行附：指标定义（`definition`/`basis`）、核对状态（候选/已核对）、来源日期（`source_date`）、来源定位（页码/片段）。
   - 两个计数条：资料覆盖（`periods_covered`/`facts_total`）与核对覆盖（`facts_verified`），**分开显示**，不合成一个"完整度"。
   - 指标区展示 `NOT_APPLICABLE`/`MISSING_INPUTS` 的状态与 `notes`（如银行不适用 ROE/现金质量、腾讯 capex 定义不等价）。
4. **状态与下一步**：每个 `statuses` 项显示业务文案（资料待采集 / 部分数据待核 / 指标不适用 / 来源冲突 / 资料待更新 / 当前未覆盖 / 在线刷新未开启）+ 受影响功能 + 下一步。**不显示**供应商端点名、CIK、accession 或运行内部 ID。
5. **刷新按钮**：调用 `POST /research/listings/{listing_id}/refresh`，展示返回的批次与去重结果；在线刷新关闭时按钮可见但结果只表示"已登记采集任务"，页面文案不得声称数据已更新。
6. 文案纪律：不出现"买入/卖出/目标价/评分"等措辞（守卫测试扫描）。

## 验证

- 定向：`tests/integration/test_web_stock_financials.py`
  - 用 `api_client` + `_hk_us_support` + 写入 fixture 资料，`GET /app/stocks/00002.HK`（会话 cookie 或 API key，与既有 web 测试一致）：200；HTML 含"报表币种"、"HKD"、真实财年结束日；含候选/已核对两档计数；`NOT_APPLICABLE` 指标带原因文案。
  - 缺失值渲染：某事实 `raw_value=None` → HTML 出现"缺失"与该原因，HTML 中**不出现**该指标的 `0`。
  - 未覆盖证券：`GET /app/stocks/AAPL`（名单内有身份但无资料）→ 200 且含"资料待采集"；名单外代码 → 与既有 400/404 语义一致。
  - 无上游标识：响应文本不含 `eastmoney`、`sec.gov`、`CIK`、`accession`。
  - A 股回归：既有 `tests/integration/test_web_*.py` 全绿；`GET /app/stocks/600519.SH` 的渲染结果与改动前一致（用 `git stash` 之外的方式：断言关键文案与元素仍存在）。
  - CSP 守卫：`test_web_candidates.py` / `test_web_client_contract.py` 全绿（新 JS 已登记、无内联、无 http 字面量）。
- 真实渲染复核（**必须做**）：起临时实例（`ia migrate up` + 种子 + `ia api serve --port <p>`，`DATA_DIR` 指向临时目录），用 `http://localhost:<p>` 打开 `/app/stocks/00002.HK`，确认财务区块的标题/列/文案与预期一致（用浏览器工具读 DOM 文本即可，不要求截图）；发现渲染与测试不一致时以渲染为准并修测试。临时实例用完关闭。

## 提交

```
feat(web): HK/US financial section on the stock page
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
