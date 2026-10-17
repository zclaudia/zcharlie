# HK2-06 来源使用范围与请求政策审查

审查日期：2026-10-17（Asia/Shanghai）；官方网页访问日期：2026-10-17（Asia/Shanghai）。基线：02daa6d。范围仅限官方政策/开发资料、HK2-05 产物、受控名单与当前代码静态核对；未请求 SEC 或东方财富数据端点。

## 结论

- config/research_material/source_policy.yaml 中 sec.companyfacts.v1 与 eastmoney.hk.native.v1 仍是 licensing.status=unknown、technical_acceptance.status=not_accepted。本文件不改审批，也不把网页阅读或既有调研响应当作新许可或真实观测。
- SEC 官方明确提供 CompanyFacts、Submissions 和历史分片 API，并规定公平访问限速和已声明 User-Agent；SEC FAQ 称 SEC 政府创建内容及 EDGAR 公开申报内容可免费访问和再使用。因此 SEC 可优先准备合规请求身份及范围核实，但不自动满足项目许可审批，也未在此确认特定缓存、应用展示、再分发或商业产品的具体权利。
- 东方财富 Choice 官方许可协议对个人非商业使用、爬虫、复制转载和衍生服务设置书面授权限制；已查到的资料未说明是否适用于本项目代码配置的证券数据路径 https://datacenter.eastmoney.com/securities/api/data/v1/get。适用范围、端点授权、缓存、展示、再分发和商业权利无充分官方证据。东财实际观测继续阻塞，须先由人工取得覆盖该端点和预期用途的书面答复/授权。
- 官方网页能打开、端点无需登录、既有数据曾被保存、使用非官方包，都不能证明采集、缓存或再使用已获许可。SEC API 文档也不等于对任意请求身份或违反 Fair Access 的访问许可。

## 受控来源及公司绑定

名单依据 config/research_material/pilot_list.yaml。名单是项目人工核对的身份映射，不是供应商授权清单。

| 来源 | 绑定公司与来源标识 | 当前审批 | 本轮范围判断 |
| --- | --- | --- | --- |
| SEC CompanyFacts + Submissions（包括 Submissions 列出的历史 JSON 分片） | 汇丰控股 HSBC (CIK 1089113)、Apple (320193)、Costco (909832)、JPMorgan (19617)、Exxon Mobil (34088)、TSMC (1046179)。CIK 是发行人标识，不代表股票类别或交易所代码。 | unknown / not_accepted | 官方资料确认接口、数据形态和公共访问；FAQ 对政府创建内容及 EDGAR 公开申报内容作宽泛免费访问和再使用说明。具体资料存档、应用展示、第三方提供和商业用例仍须按公司审批流程由责任人确认；在确认前不开在线采集。 |
| 东方财富港股原币三表 | 中电控股 CLP (00002.HK)、汇丰控股 HSBC (00005.HK)、腾讯控股 Tencent (00700.HK)。 | unknown / not_accepted | Choice 产品许可中有个人非商业限制和未经书面同意不得爬虫抓取、复制转载或发展衍生服务的条款；未找到可证明其针对或豁免本端点的官方 API 授权。个人内部使用、复制缓存、应用内展示、对外再分发和商业使用均没有可确认的端点特定许可，实际观测阻塞。 |

## 使用范围矩阵

矩阵记录官方网页直接支持的内容。未知/待确认表示本轮没有足够官方证据批准该用途，不表示已判定法律上绝对禁止。

| 来源 | 个人内部研究 | 本地缓存/留存原响应 | 私有应用内展示 | 对外再分发/公开展示 | 商业使用 |
| --- | --- | --- | --- | --- | --- |
| SEC CompanyFacts/Submissions | SEC FAQ 说明政府创建及 EDGAR 公开申报内容免费访问和再使用；这是允许再使用的官方一般表述。项目审批仍 unknown，需确认采用。 | SEC 通用再使用声明未详述抓取后原始响应保存期限、缓存条件或逐端点限制；作为项目许可证据仍待负责人确认。 | 未找到描述该应用形态的专门许可；不能仅凭公开 API 推出展示权。 | FAQ 有一般再使用表述，但本审查没有确认持续镜像、批量转发及归属展示条件；人工确认。 | FAQ 未单独列举商业授权范围或商业再分发规则；不可将宽泛再使用说明自动等同项目商业许可。人工确认。 |
| 东方财富港股原币 API | 端点专属许可未查得；Choice EULA 是强限制信号，但产品/端点适用关系未核实，状态未知。 | 未查得端点缓存/存档授权；Choice 条款对产品数据复制有限制，不能推断缓存被允许。 | 未查得授权；Choice 条款对信息使用、复制和衍生服务要求书面同意，且适用范围需确认。 | 未查得授权；Choice EULA 对复制、转载、汇编、发表、出版及镜像等要求书面同意。 | 未查得授权；Choice EULA 将其产品相关服务限于个人非商业/非盈利环境。端点适用性仍需确认，不能自行认定获批或最终受该条款管辖。 |

本项目计划代码会将 SEC 与港股材料归档为资料并用于财务上下文；因此代码行为至少涉及请求、存储/缓存和应用内展示。现有计划没有提供经核实的面向公众再分发或商业授权。此用途归纳基于代码与计划，不是授权结论。

## 官方政策与开发资料

以下仅引用监管机构或服务所有者的官方页面，均于 2026-10-17（Asia/Shanghai）访问。

| 官方资料 | 本审查使用的事实 | 未从该资料推出的结论 |
| --- | --- | --- |
| [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | data.sec.gov 提供无需认证/API key 的 JSON API；列出按 CIK 的 Submissions、CompanyFacts 路径；Submissions 可列出额外历史文件名和日期范围；还提供 nightly bulk ZIP。自动访问应遵守 SEC 隐私与安全政策。 | 无需 key 不代表不受 Fair Access 约束，也不构成项目特定商业授权；该页不承诺任意请求身份、无条件抓取或具体缓存期限。 |
| [SEC Developer Resources / Fair Access](https://www.sec.gov/about/developer-resources) | 只下载所需内容并控制请求；当前每用户总量上限为每秒 10 次，无论请求来自多少台机器。SEC 保留封锁过量请求 IP 的权利，不允许未分类 bot 自动爬取。 | 10 req/s 是上限而非建议速率、配额或数据使用许可。 |
| [SEC Webmaster FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions) | “Is content on sec.gov free?”称 SEC 政府创建内容和 EDGAR 公共申报内容免费访问和再使用。程序下载问题要求在请求头声明 User-Agent，示例含公司名/产品和 AdminContact email。Access Denied 项指示联系 webmaster@sec.gov，并附错误信息和 IP。 | 未据此确认本项目商用展示、再分发和缓存的具体条件；遇到拒绝时联系 SEC 不授权更换身份、绕过拒绝或继续重试。 |
| [data.sec.gov 官方入口](https://data.sec.gov/) | 确认提供 SEC EDGAR API 公共访问，并要求自动访问遵守 SEC Privacy and Security Policy。 | 公共访问不代表任意采集强度或 UA 都可接受。 |
| [东方财富用户服务协议](https://about.eastmoney.com/home/protocol)（生效/更新 2025-07-18） | 覆盖东方财富 APP、PC、WEB、WAP 等产品；特定服务可另有补充协议；说明提供股票等行情信息及部分付费服务。 | 未明确本项目调用的证券路径属于哪一产品、是否有 API 授权、限速、UA、Retry-After 或自动化采集规则。 |
| [东方财富产品最终用户许可协议（Choice）](https://choice.eastmoney.com/html/userprotocol/userprotocol.html) | 条款说明产品服务限个人非商业/非盈利活动；3.2.3 对相关信息使用、复制、修改、转载、汇编、发表、出版、镜像和衍生服务等要求书面同意；3.2.6 明确未经书面同意不得使用爬虫从该软件服务器直接抓取数据或资料；3.8 数据和信息供个人参考。 | 协议标题及文本指向 Choice 产品。本轮没有官方材料证明其适用于本项目直接调用的证券数据路径，也没有发现该 API 的专属例外/许可。不能外推为端点范围的最终法律解释；但明确限制意味着不能假定已有授权。 |
| [datacenter.eastmoney.com/robots.txt](https://datacenter.eastmoney.com/robots.txt)、[eastmoney.com/robots.txt](https://eastmoney.com/robots.txt)、[www.eastmoney.com/robots.txt](https://www.eastmoney.com/robots.txt) | 本轮官方网页读取工具无法成功读取这些 robots 路径（不可访问/超时）。 | robots 状态 unknown；不能称为允许或禁止，也不能把未成功读取当成授权。采集前由人工核查来源所有者规则并取得端点许可；若规则或拒绝响应不允许，应停止。 |

## 当前实现与请求要求差距

代码核对：src/investment_agent/materials/handlers.py、src/investment_agent/materials/us_sec.py、src/investment_agent/materials/hk_native.py 与 config/research_material/source_policy.yaml。

| 项目 | 官方要求/证据 | 当前实现 | 差距及安全处理 |
| --- | --- | --- | --- |
| SEC 请求身份 | SEC Webmaster FAQ 要求声明 User-Agent，示例含公司/产品名和管理员邮箱。 | HttpxMaterialFetcher 统一发送 zcharlie-research/0.1 (personal research)，没有联系邮箱。us_sec.py 自带 “contact in config” 文字，但实际通用 fetcher 的 _headers() 使用统一字符串，不能视为已有联系人的 UA。 | 明确差距。实际观测前配置可监控的项目/组织身份和有效邮箱到真实请求头，并留存证据。 |
| 请求速率与聚合 | SEC 用户总量 ≤10 req/s，跨机器计。SEC 要求有效控制负载。 | 一个进程按 host 共享 limiter；SEC 最短间隔 0.5 秒（约 2 req/s），Eastmoney 为 1 秒；未见跨进程/机器限额器。 | 单实例低于 SEC 上限，但不能证明部署总流量满足聚合要求。上线前统计 SEC 全部消费者并实行跨进程/机器限额；未知东财配额时，不能把内部 1 秒当作官方允许速率。 |
| 429 / Retry-After | 查到的 SEC Fair Access 页面未规定具体 Retry-After 策略；东财官方端点政策未查得。 | 429 时解析秒数或 HTTP 日期，最多等待 5 秒，然后抛瞬态错误并进入有限重试。 | 短等待会遵从；超过 5 秒的 Retry-After 被截短，不能保证遵守上游更长指示。来源规则未确认前，若响应要求等待超过本地上限，应暂停/终止该来源，避免过早再请求。 |
| 403 / Access Denied | SEC FAQ 对 Access Denied 指示联系 SEC 并提供错误文本/IP。东财无官方 403/API 处理规则证据。 | 所有 4xx（含 403）映射为终止本次来源的数据错误，不按瞬态任务重试；响应归档。 | SEC 当前终止行为符合不绕过拒绝的处理方向。保持 403 停止，不换 IP、不伪装轮换 UA、不重试；人工保留错误并走 SEC 指定渠道。东财也先停止。 |
| 数据授权与门禁 | 两源当前许可 unknown、技术验收 not_accepted。 | HK2-05 默认策略关闭；ONLINE_REFRESH_ENABLED=False，来源运维开关默认 false。 | 当前行为符合审查结论。不得因本文件或网页访问修改许可状态、运行开关或启动采集。 |

## 事实、推断和人工确认

**直接事实：** SEC 官方提供这些 JSON API 和历史分片说明；公布用户级合计 10 req/s 公平访问上限；FAQ 对 SEC 政府创建内容及 EDGAR 公开申报内容说明免费访问和再使用，并要求声明 User-Agent。Choice EULA 明文限制其产品的爬虫、个人非商业使用和复制/衍生等用途。当前代码统一 UA 缺少联系邮箱、单 worker 限速为 0.5 秒/1 秒、429 等待上限 5 秒、403 终止。

**审慎推断：** SEC 适合先做合规准备，因为官方接口与公平访问规则有文档；这不是许可通过。Choice EULA 是否管辖本项目证券路径不能由该文件直接确定，但它使“端点显然可爬取”的假设不成立。Eastmoney 端点政策证据不足，须阻止真实观测。

**需人工确认：**

1. SEC：责任主体、UA 联系邮箱、SEC 再使用 FAQ 对目标应用实际覆盖范围（缓存、私有展示、对外分发和商用）；以及生产所有消费者的聚合限速方案。
2. Eastmoney：通过正式渠道书面确认证券数据路径的供应产品、自动化访问/逐公司三表采集许可、速率和并发、UA/联系方法、429/Retry-After 与 403 处置、原始响应及派生字段的留存/缓存、私有展示、对外分发和商业使用权限，并确认 Choice EULA 与该路径的关系。仅端点连通或公开可访问不够。
3. 两源：项目负责人依据证据更新受控审批；未更新前维持 unknown / not_accepted，不执行真实来源观测。若许可仅限个人研究，产品用途须约束在书面确认的范围内。

本审查仅检查官方政策网页和仓库代码/配置；没有运行线上采集、真实财报接口请求、数据库操作或运行期开关操作。官方文档阅读本身不是数据接口抓取，也不构成来源授权。
