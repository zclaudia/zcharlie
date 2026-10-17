# AF-P4-08 / 08-P0 离线工程执行记录

2026-10-17，Asia/Shanghai。仅用户明确授权的08-P0工程；S0/真实来源/生产运行未授权、未执行。实际契约见[冻结](stage-b-p0-freeze.md)，复核见[交叉复审](stage-b-p0-review.md)，最终验证结果见[验收](stage-b-p0-acceptance.md)。

## 最终行为

复用现有P4三窄表、journal/CAS、native worker lease/heartbeat和逐HTTP阶段账。新增严格V2 campaign授权、跨日/换grant不可清零的累计与分阶段用量、精确action清单、只手动exact-run派发、无网络机械恢复和单独绑定原poll的显式采集resume；没有新增表/迁移/生产配置。

raw S0入口先要求独立精确operation批准，按原index/key单hop，实际发送前持久reserve/sent；不会用普通poll自证来源合同。原始响应只进隔离quarantine与安全审计，错误/撤权/暂停/unknown停止且保留用量/未决。可见sink失败和withheld原因，已知捕获/已sent不再HTTP；不承诺采集成功或全网覆盖。

来源profile另有默认空、可撤销的服务端registry，非测试普通handler只能认可受签精确hash及V2手动授权。完成window proof仍candidate，当前批准撤销后读面/绑定/发布同步受限。原有P2/P3冻结清单、知识窗、审查绑定和发布闭环不改。

## 文件与层次

基础来源adapter在evidence；raw/campaign预算与授权编排在supplemental_rules；调用ResearchWorker的窄入口在research。具体文件与hash由最终snapshot列出。旧V1文档类型与grant DTO不扩字段，不新增面向API/env/model的批准或启用入口。未来操作是受审查的内部服务端函数与明确批准记录，不增加自动daemon/通用新闻平台或研究/交易权限。

## 验证过程的解释

分工先跑契约/真实SQLite/受控HTTP专项，主代理交叉审查时发现并修正S0循环、重复发送许可、generic worker领取、raw多动作占根、旧备份同inode、wire兼容、当前批准和读面hash等接缝。原生账与campaign账跨两个会话/重启、并发预留及故障注入通过后进入完整回归。

unit首轮出现import graph两条新增边，通过移动编排模块解决，未接受P0新边。integration对应布局修订主动中止后重跑；旧失败/中止日志保留，不冒称通过。临时pytest插件拒绝所有非loopback socket连接，network标记用例仍默认排除；browser使用临时DB和loopback fixture，每份脚本只重定向截图目录，不触碰历史证据。

验证期间并行bank年度滚动、HK材料准备和mobile工作继续推进，包括新的提交、文件及baseline中的materials→settings边；这些不归属P0。最终P4专项177、integration972、roots717及五browser通过；P0 Ruff、全源码mypy466通过。unit2670通过但1项并行银行PDF失败，另全库Ruff银行矩阵测试I001；P0专属工程签收，不能称共享全库全绿。最后窄恢复补丁由最终专项/静态复验覆盖，全量日志与时点分开记，不累加。没有生产数据库、实际来源HTTP、生产开关、提交或推送操作。
