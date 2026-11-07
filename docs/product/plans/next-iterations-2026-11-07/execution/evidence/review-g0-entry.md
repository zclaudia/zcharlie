# G0 首次研究入口独立审查

审查者：银行方向实现 worker（未参与 G0 业务代码作者工作）。范围仅 QT-011/012 入口服务、schema、路由、组件、模板/CSS、对应测试，以及主代理 QT-013 的窄集成差异；本记录不审查或签署作者自己的银行批次，也不代替质量实验核心的 Review。

## 审查结论

发现2项问题，均已由主代理回修并由本审查者复核关闭。当前 G0 有界切片未留阻断项；发布许可、模型研究质量、真实布局与真实外部资源兼容性仍未验证，不以此记录声称质量功能全方向完成。

| 编号 | 发现与后果 | 关闭条件及复核证据 | 状态 |
|---|---|---|---|
| G0-R1，阻断 | `web/static/research_entry.js` 原 catch 将404以及重试时401/403/422等视为确定拒绝，清空未确认的原消息标识。首次消息可能已提交但 ack 丢失，后续 retry404不能证明原消息未提交；释放 key 后允许新消息可能重复研究。 | 主代理修改为：所有 retry 错误保留原pending；404始终保留；仅首次确定401/403/422或execution_denied才允许释放。审查者追加独立DOM反例“lost ack→retry404→reload→retry”，验证全过程同会话、同client_message_id、同body，且未确认时禁止编辑及新提交。现4项DOM测试全部通过。 | 已关闭 |
| G0-R2，准确性 | `research_quality/entry.py` 的证据查询最多100条，出现truncated时仍表述“没有可用的已核验证据”，超出了本次实际检查窗口。 | 主代理将文案限定为“本次已检查窗口中没有可用的已核验证据”，保留evidence_window_truncated，不宣称全量覆盖。已回读修改后的服务代码。 | 已关闭 |

## 边界核对

- 预检只接listing_ref/template_key/question，拒绝额外owner、模型或授权输入。listing必须精确revision1且无field_path，既有A股身份从本地Listing解析；不把输入文字当授权。
- 快照经既有SnapshotQueryService取得，保存证据先按owner和listing过滤，再经EvidenceService.get_evidence执行公开／取得／created_at可见性、subject及更正限制；未把候选快照计入已核验证据计数。当前单用户owner由服务端提供，没有声称新增多租户权限能力。
- MethodService.resolve_binding仅作当前已生效head/default/baseline读取；模型摘要只验证既有配置的可用形状，不调用provider，不回传凭证、endpoint或整份配置。缺模型保留明确缺口。
- `research_entry`路由复用verify_api_key_or_session，POST会话认证仍受同源规则保护。依赖在handler抛InvestorError时统一转译；feature_disabled明确503，旧writes_disabled仍403。原message API自身的execution_denied=409保持既有合约，没有擅自改变成503或403。
- 预检SQL跟踪未出现INSERT/UPDATE/DELETE；没有Task、grant、预算或方法binding新增。UI仅预检不发送研究；勾选完整company_research并点击开始才经原conversation/message入口发送，未接受ExecutionGrant、method_override或实验授权。
- 原MessageInput的client_message_id冻结保存于sessionStorage，重试使用同body；存储失败在消息发送前停止。预检编辑失效、异步旧响应、模型不可用和文本转义均有局部DOM验证。
- 集成只增加router注册、显式错误码、原Agent页模板/CSS及bootResearchEntry挂载。原Agent compose保留；CSS提供收起/滚动及高度上限。没有以DOM/CSS检查声称390/1440真实视觉布局验收。

## 实际执行

全部使用 BASE-03 早加载离线guard，显式文件清单，未启动lifespan、worker、完整服务栈、浏览器、executor、provider、模型、真实账户或网络。既有测试独立重跑：

```sh
NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/unit/test_quality_entry.py tests/unit/test_quality_entry_wiring.py -m 'not network' -q
```

结果：16 passed in 7.88s，退出0（预检11＋wiring5）。

DOM由同一Python guard安装后以subprocess调用Node，继承共享NODE_OPTIONS/bootstrap：

```text
node --test --experimental-test-isolation=none --test-reporter=tap tests/unit/test_quality_entry_dom.mjs
```

结果：4 tests / 4 pass / 0 fail，退出0；第4项为本次独立补充的404丢ack恢复反例。

QT-041独立原message准入及实际wheel资产验收另见 [quality-g0-acceptance.md](quality-g0-acceptance.md)，不把mock组件与ASGI串成完整全栈旅程。
