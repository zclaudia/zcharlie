# P4 阶段 A 浏览器证据

2026-10-17。由 `tests/browser/supplemental_updates.mjs` 在 OS 临时 SQLite/目录启动真实应用、受控导入和 P2/P4 worker 生成；没有注册付费 provider/agent handler，没有访问真实披露源或生产数据库。页面中的价格/账户/证据均为 fixture，截图不是来源可信性、研究质量或生产启用验收。

| 图 | 场景 |
| --- | --- |
| [local-1440](local-1440.png) / [local-390](local-390.png) | 已声明精确本地 refs，真实 worker+fenced settlement 完成，待处理成员 0；来源完整仅指声明的已保存输入 |
| [uncovered-1440](uncovered-1440.png) / [uncovered-390](uncovered-390.png) | 切换另一证券，未声明来源，不继承前主体信息，不显示“没有风险” |
| [source-failed-1440](source-failed-1440.png) / [source-failed-390](source-failed-390.png) | 已保存成功证明后注入来源失败，保留最后成功与水位并展示失败原因，不解除旧问题 |
| [closed-390](closed-390.png) | 临时库关闭 updates 后仍只读展示原来源与状态 |

脚本检查桌面/移动宽度溢出、主流程 page error，以及来源失败时保留既有证据的投影。P1/P2/P3 旧截图在串行回归后恢复开始时原档，仅本目录新增 P4 探针。
