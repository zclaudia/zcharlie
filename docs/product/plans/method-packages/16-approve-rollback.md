# 卡 16:批准与回退扩展

设计依据:§7.4。前置:卡 13、15。

## evidence_hash 构成(包提案)

`MethodService.evidence()` 对包提案的 payload 增补候选侧绑定字段,`fingerprint` 覆盖:

- 候选 `package content_hash`、`rendered_method_hash`、`renderer_version`、`validator_version`;
- 历史评估(最后一次 evaluation 的 id + fingerprint);
- 最新 `candidate_shadow` 工件的 id + fingerprint(前瞻影子);
- (离线影子仍是工程证据,不进绑定,但在 summary 中展示。)

`MethodDecision.evidence_hash` 校验语义不变(请求携带的 hash 与服务端重算一致才可批准)——
因此**批准前必须已有最新前瞻影子**;无 candidate_shadow 或其未覆盖全部
`requires_candidate_shadow` 项 → `invalid_reference`(消息列出缺口)。
另须存在至少一个 `expected="block"` 的通过反例(被包级校验拒绝)。

## decide() 扩展(仅包提案;config-only 路径不变)

批准时:

1. 追加 method version 工件,payload 的 binding 为 v2(method_ref=新 version、
   package_ref=提案冻结工件、package_hash、config=package.discipline、listing_ref=提案范围)。
2. move head(per-listing)、retire 同 head 旧 active(现状逻辑);**不触碰 MethodDefaultHead**
   (全局默认只经 activate-global,§5)。
3. event 工件冻结:候选包哈希、渲染哈希、评估/影子引用、决策请求(现状结构扩展)。

## rollback 扩展

现有 `rollback` 按 method_ref 找目标 version;补:v2 目标(version 工件 payload 的 binding 带
package_ref)回退时新 version 工件照抄目标 binding(含包字段,回退到包版本),历史任务 receipt
与发布 method_ref 不变(现状语义,测试钉住)。`known()` 已能读 v2 version;确认
`target.listing_ref` 一致性校验对 v2 同样生效。

## 测试(`tests/test_method_evolution.py` 新增)

1. 完整链路(承接卡 15 的端到端测试造数):评估 passed + 影子全覆盖 + 反例存在 →
   批准成功:head 指向 v2 binding、包工件不变、同 head 旧提案 retired、
   MethodDefaultHead 行数与 revision 不变。
2. 缺前瞻影子 / requires_candidate_shadow 有缺口 / 无 block 反例 → 批准拒绝;
   evidence_hash 与服务端重算不一致 → stale_revision(现状)。
3. 批准后新任务(per-listing)freeze 到 v2 receipt(复用卡 04 测试模式断言四要素),
   包内容与提案冻结一致。
4. 回退:v2 → v1 基线、v2 → 更早 v2,各追加新 version;历史任务 method 标记不变。
5. config-only 提案批准/回退既有测试零改动通过。

## 验收

三门禁 + 目标测试全绿。
