# PT-001/002 契约与AG22金样独立审查

审查者：银行方向worker（非该批作者）。审查范围为stress_contracts.py、stress_canonical.py、sample_builders.py、test_stress_contracts.py、portfolio_stress manifest与AG22-v2／legacy-v1两份金样。只读业务源及金样；未修改作者代码、fixture、真实账户或配置。

## 结论

发现2项问题，均已先发送主代理，由原作者回修后经本审查者独立复核关闭。当前本批无遗留阻断项；此记录不把PT-006/007的真实owner／账户／原件回读等后续功能当成已经交付。

| 编号 | 级别与定位 | 已复现问题 | 关闭条件 |
|---|---|---|---|
| PT-C1 | 阻断完整性，stress_contracts.py:224及:316 | FrozenPath允许cash=null且cash_unknown_reason=cash_missing但coverage=complete。既存partial_result仅把state改complete，StressStudyView也接受，形成缺现金却标完整的状态。 | complete路径拒绝未知cash及未知数量／非零持仓缺价；有cash_missing或未知整体结果时read-model不能complete，保持结构化partial。增加上述有效输入变异反例，保留合法zero持仓及partial场景。 |
| PT-C2 | 阻断冻结输入一致性，stress_contracts.py:257 | FrozenStressInputs允许非零holding(quantity=10, price=999, price_ref=market_snapshot#999)同时prices=[]。该已知价未绑定任何FrozenPrice副本，可绕过已有captured_at/trade_date校验。 | 对已知非零holding.price/ref，要求同listing FrozenPrice存在且ref、price严格相等；捕获与估值时间仍校验副本。缺来源须保持未知或拒绝，不能旁路。补缺副本／错listing／错ref／错价及合法来源反例。来源真实性、owner及账户关系仍由PT-006/007实际回读承担，DTO通过不构成可信来源证明。 |

## 已核对部分

- InputDecimal先限制字符串长度／整数bit_length，Decimal构造后检查finite、指数与系数，再计算40位／18小数边界；未发现先展开百万指数或先float转换通路。规范化明确区分原文字符串与typed Decimal；UTC转换拒绝naive datetime，JSON键排序、UTF-8及紧凑格式固定，非有限值／浮点拒绝。
- 请求限制4条路径、20个冲击、50个证券，冲击比例闭区间[-1,10]，重复同粒度目标和路径拒绝；假设来源固定user_scenario。PlanOptionPath保留plan、option与calculation／scenario，不能用任意数量代替原保存路径。
- 两份金样有synthetic／production_data=false标识。sample_builders实际调用原PortfolioService.create_account/append/snapshot、InvestorService.create_mandate、ValuationService.ensure/get和PortfolioCalculationService.save/get，仅固定时钟／UUID／合成交易规则及源行；没有手填SavedCalculation或计算输出冒充原链验证。
- 独立重跑完整生成链，与磁盘两份JSON逐字段及manifest字节SHA256／canonical hash／原calculation input_hash一致。AG22-v2估值价12与snapshot价10明确区分，fee=1已计入卖出现金1119／NAV2199；新增持仓费用2后NAV2198；missing_fee的cash、fees、NAV均null。原Task／预算为0，SavedCalculation实际2行。
- legacy_v1确由原save/get产生，记录unsupported_basis；未知method_version与跨account/valuation只在manifest声明后续适配边界，本轮未冒称已有adapter验证通过。计划option与真实账户/provider明确未验证。

## 命令与结果

```sh
NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/portfolio_stress/test_stress_contracts.py -m 'not network' -q
```

结果：32 passed in 2.18s，退出0。Ruff仅检查上述4个Python文件，All checks passed，退出0。

额外反例使用同一早加载guard的只读Python inline probe：从既有frozen(1)构造器／portfolio.partial_result复制后作单字段变异。输出为`missing_cash_path complete`、`missing_cash_view complete`、`known_holding_price_without_frozen_source 0`，证明上述输入确被当前DTO接受；无源文件修改、网络、provider、真实账户、安装或E2E。


## 回修复核关闭

原作者修复后的stress_contracts.py SHA256为`620802c9f5cf937043e28a6d7971d071a8600bd6c47275cdd6dc5ec3de4b33f7`，已由本审查者从实际文件重算一致；两份原金样及manifest未改。

- PT-C1已关闭：complete路径要求现金／数量已知，非零持仓价格完整；零持仓仍免价格。read-model的cash_missing及未知整体金额不能标complete；零NAV时仅loss_ratio未定义仍可保留，未以扩大数字完整性解决。已读新增cash／quantity／price／scope反例。
- PT-C2已关闭：所有已知holding价必须匹配同listing、market_snapshot引用及revision、Decimal值的FrozenPrice副本，再由既有时间检查统一约束。已读新增missing／wrong_value／wrong_version／wrong_listing反例。

独立重跑同一精确guarded测试文件：40 passed in 2.00s，退出0；Ruff上述4文件再次通过。原审查3个最小变异probe独立重跑，全部改为ValidationError拒绝（输出3次original_review_probe_rejected）。没有修改业务源／金样或启用真实账户、网络、provider、安装或E2E。
