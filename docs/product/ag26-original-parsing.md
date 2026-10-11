# AG26 原文解析、核验与更正传播

工程完成；真实研究质量门仍 pending。本项不扩大生产来源，不触发真实模型或公告下载，不开启 Agent、后台运营或 DCF。迁移 head 仍为 037，使用既有 immutable SavedEvidence，不新增表。

## 支持范围

原件必须是 AG25 保存的、属主及 listing 匹配且 hash 完整的 CNInfo/SZSE PDF 或 UTF-8 HTML。HTML 只返回纯文本，脚本、样式及主动内容不会执行。PDF 使用 optional extra `original-parsing` 中锁定的 pypdf 6.20.0；未安装时明确 unsupported，不伪造解析成功。

支持一个保守的中文财务披露子集：唯一证券代码、唯一明确的年度日期范围（例如“报告期：2025年1月1日至2025年12月31日”）、合并利润表或合并现金流量表、明确人民币元/万元/亿元、表格之前连续的明确范围/期间/单位 header block，以及单个简单表格中的“项目/本期发生额/上期发生额”或“本期金额/上期金额”。只提取本期，不推断上期日期。PDF 支持同时写明指标、人民币单位、合并口径及起止日期的完整句子；普通 PDF 视觉表格的文本行不能升级为可信表格。有限指标映射对应已有 NumericResolver 字段，FCFE 不在支持范围内。

内部七列 ISO/enum 表及 `Fact:` 行是测试与诊断语法，不代表普通公告覆盖。多个表格、母公司/个别报表、多个期间或单位、复杂或未闭合表格、跨列/跨行、嵌套表格、隐藏内容、视觉错位均保守返回 needs_data 或候选。不会把全篇出现的“合并”套用到其他表格。

## 边界与资源

输入最多 4 MiB、100 PDF 页、500 文本片段、200,000 字符；每页解码流最多 2 MiB。HTML 最多 10,000 节点、64 层、200 行及每行 16 单元格。数值适配器最多返回 50 个候选（其余原文仍保留在 manifest 和片段读回）；子进程输出最多 1 MiB，墙钟 8 秒、CPU 6 秒、文件描述符 32；单个事件循环最多两个并发解析，取消后等待子进程回收再释放配额。Linux 使用 512 MiB 地址空间限制；macOS 不支持相同 RLIMIT_AS 设置，使用每 50 ms 的 RSS 监控，可能存在一个采样间隔的瞬时超限。进程和临时输出均在退出时回收。

扫描件没有 OCR，明确 `ocr_needed_not_configured`。带图像/Form XObject 的 PDF、加密 PDF、非 UTF-8 HTML、资源超限和解析异常不产生 verified 事实。长年报常超过这些页数/片段限制；当前并非完整年报解析器。原件完整字节仍可通过既有认证附件接口下载。pypdf 自身的文字提取不能证明视觉表格位置，参见[官方提取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。

## 核验与时间

每个候选保留原件 hash、parser version、页/行或段/表格行与单元格 locator、片段 hash、证券代码/期间/范围/单位的原文 header locator 和 hash。Decimal 输入有长度及格式边界，单位换算在显式 80 位精度上下文内进行，不依赖默认精度。verified 还必须与独立既有 SnapshotQueryService 的已核验数值、精确期间、单位，以及显式 consolidated/CNY 元数据一致；缺少任何一项仍是 candidate。模型不能提交 verified 或任意来源 URL。

来源首次发布时间、原件 acquired_at、解析产物 created_at/available_from 分开保存。新下载原件或新解析事实晚于任务固定 cutoff 时，结果要求后续明确用户消息；不自动推进旧任务 cutoff。认证 GET 片段接口只读，历史重建显式只读。解析全部片段的 immutable manifest 作为候选 SavedEvidence 保存，供 AG27 有界索引；读回仍从保存的字节实际解析。

## 冲突与更正

冲突身份按 subject、canonical metric、精确 period、unit、currency、reporting_scope 归一化，与物理 locator 分开。候选与旧 verified 不同只形成“待核差异”，不会把候选当成事实；两个 independently verified 不同形成已核验冲突。既有证据正文不覆盖，当前 get_evidence 的有效状态与 warnings 分开投影；历史 cutoff/visible_at 保持旧视图。关键数值发布及当前意见读取立即受限，并通过 AG15 的幂等 receipt 合并复核。

更正仅支持原文中明确目标声明：CNInfo 实际来源公告 ID，或“本公告更正原公告编号：…”匹配唯一旧原件正文“公告编号：…”。后者是发行人公告编号，不等同于 CNInfo ID。必须同发行人、同 listing、旧原件更早、原件 hash 完整、同指标/期间/范围/单位/币种、旧新事实分别独立核验且值不同。标题“更正”及 prior byte-version 本身不足以建立更正链；重复编号/多版本歧义保留候选。不会支持所有自然语言更正格式。

## 应用路径与 AG27 交接

真实认证 conversation → queue → ResearchPolicy → FunctionModel 已接入 `read_disclosure_original` / `parse_disclosure_original`。用户显式 disclosure_access 转换为 listing/source/count/size/cutoff grant；两种工具共用 max_parses（默认 2、最多 3），相同工具及相同 body 的已提交重放不重复计数，分页 offset 改变计数。解析前释放短数据库事务，解析后重新检查 lease、取消与设置；证据、manifest、复核及动作 receipt 原子提交。feature off 时历史认证只读和精确重放仍可用，新动作不准入。

来源抽屉展示安全原文、locator、候选 provenance、有效冲突/待核差异、分页和认证附件下载。1440/390 的浏览器回归覆盖真实解析结果、XSS 样文本、分页与下载。

AG27 从保存 manifest/原字节进行有界搜索索引，保持 hash、parser version、availability 和授权边界；搜索片段不自动核验，也不能回填模型记忆或猜测报告期。OCR、复杂表格、更多报告适配器、批量全市场覆盖和通知均未实现。AG20 真实试验仍为 6 HTTP / 75,669 tokens / 0 有效研究，FCFE 0/4、trading rules unavailable 保持不变。


## 最终验证

固定 offline 全量：2118 passed、3 skipped、6 deselected，305.91 秒，exit 0。三项 skip 为未配置外部 ZCLAUDIA_GATEWAY_DIR。Ruff、mypy（266 source files）、diff、JavaScript 语法与 1440/390 实际来源浏览器回归均通过。前两次主动中断的全量结果保留在卡片完成记录，不计为通过。
