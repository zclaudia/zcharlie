# P2 合成浏览器证据

日期：2026-10-16（Asia/Shanghai）。运行 `tests/browser/run.sh supplemental_rules.mjs`；使用实际Web/API、临时SQLite和生产ResearchWorker，材料及公司均为合成数据。测试所开启的P2设置只存在于临时fixture进程与临时数据库。

| 页面 | 1440px | 390px |
| --- | --- | --- |
| 规则与事件管理 | [桌面](synthetic-management-1440.png) | [窄屏](synthetic-management-390.png) |
| A股补充事项 | [桌面](synthetic-stock-1440.png) | [窄屏](synthetic-stock-390.png) |

截图来自真实HTTP应用的合成保存结果，不是手绘或静态假页面。浏览器另在脱离文档的节点上测试完整分组依据、精确证据引用、分页按钮及待处理/失败成员区间；这些渲染专项与真实后台生命周期分别取证，不把投影输入当作真实来源能力。主代理人工检查390px截图发现单股页未加载P2样式表，已补引用及实际computed-style断言后重跑并重新检查，长hash可换行。

这组证据仅覆盖P2独立管理和补充事项读取；公司正式观点、P1账户排序整合、可信监管核验及P3语义解除仍未接入。没有修改生产数据库或开启生产开关。

本轮全面复审后又串行重跑P2/P1浏览器，新增“相同run编号但本地操作身份不同，刷新仍保留accepted回执”的真实浏览器反例。P2截图更新为本轮合成结果；P1历史截图按复跑前备份恢复。
