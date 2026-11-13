# wmshare 分红接入验收

日期：2026-11-13。对应 [接入方案](../product/wmshare-dividend-integration.md)。

后续已完成日常实例迁移、服务重启与采集启用，任务 #805 实采 9 股成功，消耗 22,769 EC。
见 [生产采集验收](wmshare-dividend-live-2026-11-13.md)；本文保留此前隔离验收的状态和记录。

## 交付

- 复用 `dividend_snapshot`、现有 worker、任务制品和周频调度；wmshare 每股只取历史与平台快照两项。
- 仅采集启用的 A 股观察池，支持股票页单股刷新。周任务继续采集 baostock 送转资料，避免重复采集观察池。
- 现金股息仍由现有 `dividend_yield_ttm` 计算；完整 wmshare 现金事件与 baostock 送转按日期合并，缺覆盖时回退原有来源。
- 事件按供应商隔离版本；迁移 068 保留旧事件 ID、版本和历史引用。新数据遵守实际抓取时间，不重写既有研究。
- 平台 TTM 和派息率独立保存、独立显示，不进入现金股息因子。待确认记录不计入现金。
- 历史或平台任一部分失败时，分别保留上次成功值；缺失、零值和待确认各自展示。
- 设置页新增凭证与启用项，默认关闭；未修改全市场筛选接口、用户凭证或日常数据库。

## 真实响应回放与页面

使用已保存的公开响应 [测试样本](../../tests/fixtures/wmshare/dividends-real.json)，通过解析器、存储、原有因子和正常股票页验收。
该样本包含先前实验的近期历史响应；用于核对数值及字段语义，不代表重新执行了正式的 `limit=500` 请求。
baostock 行情、送转与覆盖在验收数据库中显式构造，不冒充生产采集结果。

| 样本 | 验收结果 |
|---|---|
| 招商银行 | 每股现金 2.016，价格 42.63，现金股息率 4.73%；平台 TTM 2.35%、派息率 13.53% 单列 |
| 中国银行 | 已实施现金合计 0.2263；无真实除权日的 0.119 仅在待确认区域展示 |
| 南方航空 | 资料覆盖完整时现金为 0，平台为 0，均显示 0.00% |
| 无资料／停用观察项 | 显示“未采到”，刷新按钮停用并说明原因 |

上述真实价格、平台行情日期为 11 月 12 日，页面计算日为 11 月 13 日；页面不显示同日“口径差异”标记。
同日差异超过 0.3 个百分点、派息率超过 100% 的分支另由因子与 DOM 测试覆盖。

正常应用在隔离数据库启动，浏览器分别使用 1440×1000、390×844 验收。
核对年度明细、现金与平台两种口径、待确认、零与缺失、单股刷新入队和重复提交幂等。
修复了手机分红卡片顺序及分区锚点遮挡；两种尺寸均无横向溢出。
最后页面无控制台错误；重启验收服务期间的一次旧页轮询断连不属于业务错误。
应用禁止访问外网且未产生上游连接尝试；本次验收新增 EC 消耗为 **0**。

页面截图保存在本机忽略目录 `.dev/wmshare-acceptance/`：
`cmb-desktop.png`、`cmb-mobile.png`、`boc-mobile-top.png`、`boc-mobile-pending.png`。

## 自动验证

覆盖解析、供应商版本隔离、PIT、现金与送转、部分失败、待确认、迁移、队列、观察池范围、设置和调度。
重点命令：

```sh
.venv/bin/python -m pytest -q tests/unit/test_wmshare_dividends.py tests/unit/test_wmshare_collector.py tests/integration/test_wmshare_dividend_flow.py tests/integration/test_api_dividends.py
node tests/browser/dividends_dom.mjs
.venv/bin/python -m mypy src
.venv/bin/python -m ruff check src/investment_agent/dividends src/investment_agent/providers/wmshare_dividends.py src/investment_agent/api/routes/dividends.py
```

最终联合回归通过 169 项（1 项按既有标记排除）；另补跑字段来源、CLI、股票页原有区块回归 69 项，
合计 238 项 Python 测试通过。DOM 测试通过 2 项。
完整类型检查通过，覆盖 588 个源码文件；Ruff 与差异空白检查通过。
另一次更广的采集、调度、设置回归通过 189 项（2 项按既有标记排除，与上述部分重叠）。

## 启用现有实例

实现与验收均已完成；日常实例尚未执行迁移或开启付费采集。

1. 用实例原有环境安装依赖并执行 `ia migrate up`，迁移到 068；使用与 API、worker 相同的 `DATABASE_URL`。
2. 重启 API、worker、scheduler，在“设置 → 数据源 → wmshare 分红”开启采集。
3. 沿用已配置的 `WMSHARE_CLIENT_ID`、`WMSHARE_CLIENT_SECRET`；凭证未在数据库保存时继续读环境值，也可通过现有设置页加密保存。
4. 在启用观察池股票页点击“刷新分红”，于运行记录查看 `ec_by_ticker`、`ec_known_total`、未知消耗项和分项错误；之后随现有周任务更新。

已有实例的普通运行设置以数据库为准，后改 `.env` 的启用项不会自动覆盖它；应通过设置页或
`ia settings set wmshare_dividends_enabled true` 开启。无需重新导入所有设置。
