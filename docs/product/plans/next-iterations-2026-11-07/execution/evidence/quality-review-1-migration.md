# QT-007 独立审查：迁移、制品 guard 与开关

日期：2026-11-07。作者：implement_research_entry（未参与本次 root 的 QT-007 改动）。结论：**初审的一个 P1 已由 root 回修，并经本审查者独立复核关闭；本段审查范围内无剩余阻断**。此审查只覆盖 root 编写的集成内容，不自审 QT-001–QT-006，也不签署完整 QT-009。

## 范围与验证

只读审查 `storage/models/research_quality.py` 末尾 `QUALITY_ARTIFACT_DDL`、冻结迁移 `065_research_quality.py`、`config.py`/`settings/registry.py` 新开关、`storage/models/__init__.py` 注册、`test_quality_settings.py` 与 `test_quality_migrations.py`。未修改实现、迁移或测试；未读 dotenv/真实库，未调用 provider。

```sh
NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/integration/test_quality_migrations.py tests/unit/test_quality_settings.py -m 'not network' -q
```

结果：`11 passed in 2.68s`（迁移 9 + flags/注册 2）。未运行旧 dotenv 路径测试。额外 SQL 复现仅在既有 offline guard 的 `NEXT_ITERATIONS_SANDBOX` 下新建临时 SQLite，直接使用 AST 读取的 065 冻结 guard 字符串，无修改/关闭 guard。

## P1：UPDATE OR REPLACE 可删除 quality 冲突受害行（已关闭）

位置：模型 `QUALITY_ARTIFACT_DDL` 的 update guard（本次快照第 188–191 行），065 `_STATEMENTS` 中对应 update guard（第 35 行）。owner：root / QT-007 原作者。

初审快照的 update guard 仅判断被更新行的 `OLD.kind` 和 `NEW.kind`。当普通 `method_version` 行保持普通 kind，使用 `UPDATE OR REPLACE` 将其 UUID 或隐藏 rowid 改为已有 quality 行的身份时，二者仍为普通 kind，update guard 不阻断。SQLite 会删除冲突的 quality 受害行；`recursive_triggers=OFF` 时 delete guard 无法保护该隐式删除，insert guard 也不会为 UPDATE 执行。

复现初始表（与原 MethodArtifact 布局相同、三个冻结 guard 均已安装）：

```sql
PRAGMA recursive_triggers=OFF;
INSERT INTO investor_method_artifact (id,owner_id,kind,payload)
VALUES ('ordinary','owner','method_version','{}');
INSERT INTO investor_method_artifact (id,owner_id,kind,payload)
VALUES ('quality','owner','quality_blind_map','{}');
```

两条路径分别在独立临时库运行：

```sql
UPDATE OR REPLACE investor_method_artifact
SET id='quality' WHERE id='ordinary';

UPDATE OR REPLACE investor_method_artifact
SET rowid=(SELECT rowid FROM investor_method_artifact WHERE id='quality')
WHERE id='ordinary';
```

实测输出（`rowid,id,kind`）：

- 两库原始记录均为 `[(1,'ordinary','method_version'),(2,'quality','quality_blind_map')]`。
- UUID 路径执行后只剩 `[(1,'quality','method_version')]`。
- rowid 路径执行后只剩 `[(2,'ordinary','method_version')]`。

这会丢失不可变制品，且删除后其 kind 不再为 quality，后续 downgrade 的六 kind 保留检查也无法识别已丢失历史。现有测试覆盖直接 UPDATE old/new kind 转换及 INSERT OR REPLACE 的 UUID/rowid 冲突，但没有覆盖 UPDATE OR REPLACE 的冲突受害行。

最小回修建议：update guard 除 OLD/NEW kind 外，还检查目标 `NEW.id` 或 `NEW.rowid` 所命中的现存 quality 受害行；模型 DDL 与 065 冻结 SQL 同步。不要通过扩大所有普通制品的不可变约束解决，也不依赖启用 recursive triggers。

退出证据：六个 quality kind 分别作为受害行，以 UUID、rowid 两种 UPDATE OR REPLACE 路径（可含 `_rowid_`/`oid` 别名）均被拒绝，原受害行/普通行完整保留；普通到普通原允许 SQL 行为仍可用；重跑精确迁移/flags 测试及模型/迁移 trigger 一致性。原作者回修后由非作者复核。

## 原作者回修与独立关闭

2026-11-07，root 在 update guard 增加现存受害行检查：`EXISTS` 查询目标 `NEW.id` 或 `NEW.rowid` 所命中的 quality 行；模型 DDL 与 065 冻结 SQL 同步。迁移测试在六 kind 参数内分别增加 UUID 和 rowid 的 UPDATE OR REPLACE 反例。

本审查者再次只读比对上述实现、冻结 SQL 与新增反例，重新运行相同精确命令：`11 passed in 2.63s`，包含模型/迁移 13 trigger 一致性、六 kind 保护、拒绝有数据降级、空降级/升级及两个开关。另在独立 offline 临时 SQLite 中复跑原始 UUID/rowid 复现，并额外检查 `_rowid_`、`oid`：四种写入均抛 `quality artifacts are immutable`，原两行逐字段完整保留。普通行到另一普通行的 UPDATE OR REPLACE 仍成功，quality 行完整。

因此 P1 关闭；本次独立迁移/开关审查无剩余阻断。不扩大为 QT-001–QT-006 作者自审、完整 QT-009、真实研究质量或生产验收。

回修复核 SHA-256：

| 文件 | SHA-256 |
|---|---|
| models/research_quality.py | `88e9cd769327ede03ba52022149d01122e6e7b41a8795f672f8e6732511fe75f` |
| 065_research_quality.py | `1e8a3b204a5becb75c5256d5d715987ff00df5855556e034054b3a3c6732e774` |
| test_quality_migrations.py | `1e6011d04eeb3258c94919715e091e20a88044e34e4f009bd12bbc69591091bc` |

## 其余观察

- 迁移无运行时 ORM 导入，链为 `064 → 065`、单 head；测试比较五表字段、PK、checks、unique、indexes、FK 和 13 个 trigger，与 `create_all` 一致。三个历史表均为 WITHOUT ROWID。
- downgrade 在删 trigger/表之前检查五表任一已有记录及六类 quality 制品；有记录时拒绝并保留版本 065。空降级/再升级保留既有普通 MethodArtifact；上述 P1 修复之前不能据此宣布历史完整性通过。
- 两个开关在 Settings 与 RuntimeSettings 中均为独立 bool、默认 false；registry 中各登记一次 env source、live、非 secret，ENV_KEYS 由 SPECS 生成，无遗漏。标签未暗示开关能替代原授权/预算；实际新写/发送边界接线属于后续任务。
- 新模型模块已在 imports/`__all__` 注册，保留 simulation、events、bank topics、portfolio stress 既有并行注册；未改原 MethodArtifact 表布局或普通 kind 的 SQL 级契约。

## 初审快照

| 文件 | SHA-256 |
|---|---|
| models/research_quality.py | `fb0f1f50ebef512eabe86368f2affb1be5c714e6a2695b6febd798fdb5887267` |
| 065_research_quality.py | `99b644739d4d5246b6e07db61b08249f14168d874a432b9366fbd92ee5da9ff0` |
| config.py | `11d071f0486bc86aa10df6885a7862e0c133dacde15a7195dac228a5298a1b2d` |
| settings/registry.py | `a3920d38a7577a2e14d560c5c279b82420d592d1ee6b3285bae4d073bd53d1c1` |
| models/__init__.py | `d35e27bafb51fc2c41621b12abdb38fdff9efa3825f0c560835b00abd1bb3665` |
| test_quality_settings.py | `9de64a6ce2b5d4446a8f8b2061f05e9cedcae79a46f13893dc99062b9758b6ee` |
| test_quality_migrations.py | `fcf3a63bbeff9dcae0b7228470e0f2d935925072dbfe6c0e38992eed1364f649` |
