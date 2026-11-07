# BT-009 本地招行金样本

在主代理通知 BASE-03 独立验收通过后，使用已存 PDF 和元数据完成原字节及文本行列核验，未下载或复制原件、安装依赖、联网、调用模型、读取真实账户或运行真实 E2E。

原件为 `research/industry-data-validation/evidence/run-02/raw/official/600036.SH_2025.pdf`，32,081,947 bytes，SHA-256 `abe612a273468072b176dd51ea460c1e1596f8ca729cbc6db3fa28ba9a57ea79`。PDF22/23 分别对应印刷21/22；集团年度两期净利息收入为215,593／211,277百万元、净利息收益率为1.87／1.98%；分解末行为规模16,798、利率(12,482)、净4,316百万元。脚注明确共同变动计入规模影响。集团定义及 CAS／公历年度分别回读 PDF3、PDF143。

公开日期仅采用既存 CNINFO 公告日2026-03-28，保留日期精度及 published_at=null；取得时间采用 HTTP fetched_at_utc=2026-10-11T15:10:33.840106+00:00。原文摘录、SHA256、精确行列及精度说明保存于 `tests/fixtures/bank_topics/`，不生成生产原件引用。只读 pypdf／Adobe-GB1 内存映射提取已核对；环境无 Poppler，未安装或声称完成渲染目视验收。

执行命令（退出0）：

```sh
NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/next_iterations/test_bank_topic_fixture_integrity.py -m 'not network' -q
```

结果：15 passed in 3.46s。共享 plugin 在收集前加载，显式收集非空，未绕过 guard。

同一测试随后与 BT-001/002 局部合同测试共同运行，最终99 passed in 4.42s，退出0（本卡15项，合同84项）。Ruff 对5个方向文件检查通过；mypy 使用 `.venv/bin/python -m mypy` 进行检查（可执行脚本入口的旧 shebang 不再有效），两处标注问题已修复，最终 `Success: no issues found in 5 source files`。未修改共享 runner；运行命令显式列出本卡文件及两份合同测试。
