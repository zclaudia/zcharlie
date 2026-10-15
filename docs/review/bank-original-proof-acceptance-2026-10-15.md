# 银行归母净资产与原件核验验收

日期：2026-10-15。三个 GPT-6 Luna 子代理分别负责官方证据、银行核验规则、审计导入；主代理独立审查、补反例、执行整链与回归。

## 实际覆盖

FY2025 固定 10 家银行、每家 5 个字段，共 50 条审计记录：39 verified、9 candidate、2 scope_mismatch。这是明确研究样本，不代表完整银行行业。

| 字段 | 通过原件核验 | 保留候选 | 明确单体并排除集团比较 |
|---|---:|---:|---:|
| 归母总权益 | 10 | 0 | 0 |
| 年度加权 ROE | 7 | 3 | 0 |
| 净息差 / 净利息收益率 | 8 | 2 | 0 |
| 不良贷款率 | 7 | 2 | 1 |
| 拨备覆盖率 | 7 | 2 | 1 |

候选为农业银行四项比率、宁波银行四项比率、平安银行 ROE，集团口径或年度列证据仍不足。平安年报 p21 明确监管指标除资本充足率外均为本行口径，因此其 p20 不良率、拨备覆盖率保留原值与证明，规范集团字段置空并排除比较。未知范围保留 candidate，不能冒充已知单体范围。

9 家归母权益直接取合并报表归母合计；平安明确标记 `derived_parent_total`，由集团定义、100% 持股、少数股东权益须单列的会计政策、当期合并表四页证明推导集团总权益等于归母权益。总权益行本身不能通过普通归母权益规则。普通股权益、本行权益、含少数股东权益的合计均不能替代。

主代理逐家查看了全部 10 份归母权益关键页，核对当期集团列与金额单位，独立记录各家规范金额作为整链测试断言。视觉审计清单及截图保存在 ignored 证据目录 `research/industry-data-validation/evidence/bank-proof-2026-10-15/qa/root-parent-equity/`。完整字段清单见 [审计 fixture](../../tests/fixtures/bank_proofs_v2/audit-manifest.json)，来源说明见[字段证据说明](../product/plans/industry-expansion/bank-proof-evidence.md)。

## 实现与独立审查

- 显式 `--audit-path` 和 `--originals-root` 才开启银行原件核验；旧供应商导入保持候选行为。全 50 项输入在写库前检查，缺原件、伪造范围或数值冲突拒绝全批写入。
- `bank-annual-original/1` 与旧白酒规则分开。原件身份、字节 SHA256、具体页、完整字段标签、实际集团当期列、金额单位、脚注、范围证明及审查时间均绑定到字段。保存证据和严格引用读取原件时再次验证。
- 招商、建行等报告的 Identity-H 字体缺少 ToUnicode；按实际字体 Adobe-GB1 声明应用标准映射，包含嵌套 Form 字体。原 PDF 字节与哈希不变，不增加 fontTools 依赖。映射及许可证已验证包含在 wheel 中。
- 主代理复现并修复三类独立绕过：声明集团列但读取实际本行首列、借用无关千元说明改变百万金额、缩短字段标签以隐藏普通股或扣非限定。另补审查时间与 audit 时间不一致的拒绝，以及原件字节损坏读回拒绝。
- 快照追加版本、路径迁移与重复导入幂等；已冻结旧候选批次保留。完整 10 家的新批次按真实覆盖返回；严格引用验收另建明确列出排除原因的 7 家完整核验子样本，不将候选混入已验证结论。

规则针对本轮 FY2025 报告表式；续页、集团叙述和派生权益均采用有界规则及实 PDF 回归，不宣称任意年度、银行、PDF 均能自动核验。

## 验证

主代理真实整链覆盖：供应商旧候选导入及冻结 → 审计追加快照 → 原件字段读回 → 重复导入 → 字节损坏拒绝 → 新批次与旧批次隔离 → 7 家完整核验子样本四指标的 `require_verified_inputs=True` 成功。

全量测试分三个互斥组执行，共 3366 passed、4 skipped、6 deselected：

| 测试组 | 结果 | 耗时 |
|---|---|---:|
| `tests/unit` | 2043 passed、6 deselected | 286.96 秒 |
| `tests/integration` | 606 passed、4 skipped | 527.85 秒 |
| 其余 `tests` | 717 passed | 214.46 秒 |

4 项跳过均需要外部已构建的 zclaudia-gateway checkout；6 项联网测试按项目默认标记排除。不是银行原件缺失跳过。本机实 PDF 测试使用全部精确原件执行。

全量执行期间，主代理另补一个“扣非限定放在 ROE 标签前”的反例并收紧同一行标签规则；末次改动后，银行导入、核验规则、字体解码、全部主代理反例和真实 PDF 整链共 51 项专项测试全部通过（193.43 秒）。mypy 376 个源文件通过，全部修改范围 Ruff 与 whitespace 检查通过。

备份恢复使用正式 `tools/backup_research.py` 和 `tools/verify_backup.py`，备份隔离验收库与三份批次制品。恢复到新目录后，10 份 PDF 的 SHA256 全部匹配，39 个已验证字段全部重新核验通过，7 家完整核验子样本的四项严格指标引用全部通过。不是只检查备份文件存在。

## 复现入口与边界

以下为使用本地证据的导入命令，验收只在隔离临时库执行：

```sh
ia peer-annual import-bank \
  --evidence-root research/industry-data-validation/evidence/run-02 \
  --report-period 2025-12-31 \
  --audit-path tests/fixtures/bank_proofs_v2/audit-manifest.json \
  --originals-root research/industry-data-validation/evidence/bank-proof-2026-10-15
```

原 PDF 位于 ignored 研究目录，不随代码提交；缺少精确原件时实 PDF 测试明确跳过，不能把合成 fixture 当成真实年报。白酒、旧银行候选路径及在线银行采集拒绝门禁保持兼容。本轮未启用线上采集或调度，未调用真实 LLM，未导入真实业务库、提交代码或部署。
