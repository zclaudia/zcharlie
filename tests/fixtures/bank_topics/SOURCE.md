# 招行年度专题本地金样本（BT-009）

此 fixture 只引用已保存在仓库研究证据目录中的原字节，不复制 PDF，不下载、不安装、不调用模型或真实账户。`manifest.json` 的路径是离线验收输入，不是运行时 original ref；正式导入须另经 owner-scoped 原件库校验及绑定。缺失本地 PDF 时完整性测试应失败，不以 skip 冒充通过。

已实际回读 `research/industry-data-validation/evidence/run-02/raw/official/600036.SH_2025.pdf`：32,081,947 bytes，SHA-256 `abe612a273468072b176dd51ea460c1e1596f8ca729cbc6db3fa28ba9a57ea79`，350 页，与既存 run-02 manifest、filing metadata、HTTP metadata 三处一致。原文提取使用既有 `bank_page_text` 的 pypdf 与随项目提供的 Adobe-GB1-UCS2 内存映射；原字节不变。当前无 Poppler 命令，未安装依赖或进行渲染目视验收，`visual_render_review=false`。行列和页标签按原 PDF 文本及 layout 提取复核。

## 原文定位与口径

PDF 页从 1 计数。PDF22 对应印刷21，章节 `3.2.5 净利息收入`，段首明确“本集团”。年度表头从左到右为 2025 年、2024 年，金额单位“人民币百万元，百分比除外”。“净利息收入”在各年度“利息收入”列为 `215,593`、`211,277`；“净利息收益率”在“平均收益率%”列为 `1.87`、`1.98`，不能取下一行文字中的净利差 `1.78`、`1.86`。

PDF23 对应印刷22，首表标题段同样明确“本集团”，期间为“2025 年对比2024 年”，金额单位“人民币百万元”。末行“净利息收入变动”的规模、利率、增（减）净值依次为 `16,798`、`(12,482)`、`4,316`。括号表示负数。表前规则明确规模与利率共同引起的变化计入规模影响；不能称为剔除了交互项的纯数量效应。该页随后是“2025 年10-12 月”与“2025 年7-9 月”的季度年化表，不能混入年度值。

PDF3／印刷2定义“本集团”为招商银行及其子公司。PDF143／印刷142的财务报表编制基础明确执行企业会计准则，年度自公历1月1日至12月31日止。FY2024 使用 FY2025 同表比较列；未读入另一份 FY2024 原件，也不推定比较值是否另有重述。

每个 locator 保存最小必要摘录及其 UTF-8 SHA-256。摘录先按 Python `str.split()` 折叠空白再用一个 ASCII 空格拼接；完整性测试从原 PDF 重新提取后验证摘录、原始行和列。`expected.json` 的原文值保留千分位、百分数和括号，标准化值全部为字符串；派生数仅为 Decimal 恒等计算，不构成前瞻结论。

## 公开、取得与本次核验时间

既存 CNINFO filing metadata 的公告标识为 `1225047590`，公告日期与 adjunctUrl 日期为 2026-03-28。这里只保留 `published_date=2026-03-28`、`published_precision=date`、`published_at=null`；即使旧 fixture 曾把日期转换为午夜，也不把该转换当真实公告时刻。方案引用的银行官网 URL 路径日期为 20260327，但本轮没有该 URL 的抓取记录，未据此改写公开日期。保留存量 CNINFO 来源 URL；这不是对官网链接可用性的重新验证。

既存 HTTP 元数据记录 `fetched_at_utc=2026-10-11T15:10:33.840106+00:00`，可据此记录此次取得的 timestamp 精度，不能推断是系统第一次取得。HTTP Last-Modified 与 PDF CreationDate 均不作为公开时间。日期精度的公开上界需运行时按 Asia/Shanghai 当日结束计算，不能制造精确发布时刻。

本次原文核验 `checked_at=2026-11-06T18:29:12+00:00`，actor 为 `codex-local-pdf-review`。fixture 验证状态仅表示本地原字节与文本行列经过核对，不自动提升为 owner-scoped 生产事实。

## 测试状态

测试文件：`tests/next_iterations/test_bank_topic_fixture_integrity.py`。BASE-03 经主代理验收后，使用共享早加载 plugin 和显式文件清单执行，15项通过。命令及退出状态记录在执行证据 `docs/product/plans/next-iterations-2026-11-07/execution/evidence/bank-fixture.md`；未运行真实 E2E。
