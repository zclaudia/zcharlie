# 01 受控身份与市场注册表

> 波次 W1 ‖ 可与 02 并行。依赖：无。
> 文件所有权：[`README.md`](README.md) §4 第 01 行。**不得触碰** `alembic/**`、`tests/conftest.py`、`storage/models/**`（属 02）。

**目标：** 建立港美证券的受控身份解析与市场注册表，使"选择已确认的上市证券"这一步不需要新增交易所枚举就完成，且**永不按名称模糊合并公司**。

**决策：** 身份权威是人工核对的名单文件，不是模型输出、不是上游响应。`Market.currency` 只表示市场默认报价币种，报表币种按证券单独保存。

## 代码事实

- `Listing` 唯一键 `(market_code, ticker)`：`src/investment_agent/storage/models/reference.py:50-52`；ticker 存**裸代码**（A 股为 6 位数字）。
- `ListingRepository.upsert_listing` 按 `Company.canonical_name` **精确字符串**匹配公司，且 `canonical_name` **非唯一**，同名多行会抛 `MultipleResultsFound`：`src/investment_agent/storage/repositories/listing.py:19-48`。
- `Company` 无任何外部标识列（无 CIK/ISIN）：`reference.py:36-45`。
- A 股市场表 `AShareMarket`/`A_SHARE_MARKETS`（SH/SZ/BJ → SSE/SZSE/BSE）：`src/investment_agent/domain/a_share.py:9-22`；`format_listing_ticker` 对未知 market 原样返回（`a_share.py:33-37`）。
- `Market` 字段：`code/name/timezone/currency`（`reference.py:18-24`）；迁移 001 **不插入任何市场数据行**，市场行由代码保证。
- 现有 NASDAQ 市场只在测试 fixture 里出现：`tests/conftest.py:92-102`；`watchlist.py:142,154` 对非 A 股代码硬编码回落 `"NASDAQ"`。
- `portfolio/service.py:239,308,623` 与 `portfolio/calculations.py:355` 接受的市场码集合为 A 股专用，本次不改。

## 文件清单

```
新增  src/investment_agent/domain/security.py
新增  config/research_material/pilot_list.yaml
新增  tests/unit/_hk_us_support.py
新增  tests/unit/test_security_identity.py
修改  src/investment_agent/storage/repositories/listing.py
```

## 接口契约（冻结）

```python
class SecurityType(StrEnum):
    ORDINARY = "ordinary"          # 普通股
    ADS = "ads"                    # 美国存托股份

@dataclass(frozen=True)
class SecurityMarket:
    code: str                      # Market.code："HKEX" | "NASDAQ" | "NYSE"
    name: str
    timezone: str
    quote_currency: str

MARKETS: dict[str, SecurityMarket]  # 上述三个；A 股仍由 a_share.py 负责

@dataclass(frozen=True)
class SourceBinding:
    source: str                    # "eastmoney.hk.native.v1" | "sec.companyfacts.v1" | "yahoo.chart.v1"
    symbol: str                    # 该来源使用的代码："00002.HK" / "0002.HK" / CIK 十进制
    note: str = ""

@dataclass(frozen=True)
class ControlledIdentity:
    market_code: str
    ticker: str                    # 存储代码：HK 五位含前导零（"00002"），US 大写（"AAPL"）
    company_key: str               # 名单内人工给定的公司键；两个 listing 共享该键 = 明确同意共用 Company
    company_name: str
    company_name_zh: str
    security_type: SecurityType
    quote_currency: str
    report_currency: str           # 报表币种，独立于报价币种
    fiscal_year_end_note: str      # "12-31" / "52/53 周制，财年最后一个周六"
    sources: tuple[SourceBinding, ...]
    valid_from: date               # 名单核对生效日
    valid_to: date | None = None
    ads_ratio: str | None = None   # 如 TSM "5"（1 ADS = 5 普通股），必须来自来源
    notes: str = ""

def load_pilot_identities(path: Path) -> tuple[ControlledIdentity, ...]
def normalize_ticker(market_code: str, raw: str) -> str       # 未知市场 raise ValueError
def find_identity(*, market_code: str, ticker: str, path: Path) -> ControlledIdentity | None
def source_symbol(identity: ControlledIdentity, source: str) -> str | None
```

`pilot_list.yaml` 必须包含八家：`HKEX/00002`（中电，报告币种 HKD）、`HKEX/00005`（汇丰，USD）、`HKEX/00700`（腾讯，CNY）、`NASDAQ/AAPL`、`NASDAQ/COST`、`NYSE/JPM`、`NYSE/XOM`（以上 USD）、`NYSE/TSM`（`security_type=ads`、`report_currency=TWD`、`ads_ratio="5"`）。每家的 `sources` 至少含 SEC CIK（美股）或 `eastmoney_hk`/`yahoo` 代码（港股）。CIK 必须在注释或 `note` 里写明"发行人标识，不等于股票类别或交易所标识"。

## 仓库侧新增方法

```python
# storage/repositories/listing.py
async def ensure_market(self, market: SecurityMarket) -> Market
    """按 registry 建/更新 Market 行（name/timezone/currency 取 registry 值）。"""

async def ensure_controlled_listings(
    self, identities: Sequence[ControlledIdentity]
) -> dict[tuple[str, str], Listing]
    """按 company_key 分组解析 Company，再建/取 Listing。规则：
    1) 同一 company_key 的多条身份 → 复用同一个 Company（这是**唯一**允许的跨市场共用路径）；
    2) Company 解析优先复用该组已有 listing 的 company；否则精确匹配 canonical_name
       且**只有恰好一行**时才复用；
    3) 匹配到 ≥2 行同名 Company 或已有 listing 指向不同 company 时 raise ValueError，
       绝不合并、也不改指；错误信息包含 market/ticker/company_name；
    4) 不因中英文名相近或 ticker 相似而合并。
    """
```

## Task

- [ ] T1.1 写 `domain/security.py`：`MARKETS`（HKEX `Asia/Hong_Kong`/HKD、NASDAQ `America/New_York`/USD、NYSE 同）、`normalize_ticker`（HK 允许 `00002.HK`/`2`/`0002` → `00002`；US 允许 `aapl.US`/`aapl` → `AAPL`；其他 market 抛错）、`load_pilot_identities`/`find_identity`/`source_symbol`，并用 `validate_pilot_list` 在加载时拒绝缺字段、重复 `(market_code, ticker)`、重复 `source_symbol`、`quote_currency` 与 registry 不一致的条目。
- [ ] T1.2 写 `config/research_material/pilot_list.yaml`（八家，含来源映射与说明；汇丰报表 USD、腾讯报表 CNY、报价均 HKD 必须显式写出）。
- [ ] T1.3 在 `listing.py` 增加 `ensure_market` 与 `ensure_controlled_listings`（上面签名与规则），保持既有方法签名与行为不变。
- [ ] T1.4 `tests/unit/_hk_us_support.py` 提供 `seed_hk_us_markets(session)`、`pilot_identities()`、`seed_pilot_listings(session)`，供后续任务复用（不要改 `tests/conftest.py`）。
- [ ] T1.5 `tests/unit/test_security_identity.py` 覆盖下表用例。

## 验证

| 用例 | 断言 |
| --- | --- |
| 前导零 | `normalize_ticker("HKEX","00002.HK")=="00002"`；`("HKEX","2")=="00002"`；`("HKEX","700")=="00700"`；`("NASDAQ","aapl")=="AAPL"`；`("SSE","600519")` raise |
| 来源代码分源转换 | 汇丰 eastmoney=`00005.HK`、yahoo=`0005.HK`、sec=CIK 十进制；两者不互相复用 |
| 同名不合并 | 两条 `company_key` 不同、`company_name` 相同的身份 → 两个 Company 行（查询 `Company` 计数 = 2） |
| 同 company_key 共用 | 两条共享 key 的身份 → 一行 Company、两个 Listing |
| 歧义拒绝 | 库中已有两个同名 Company 且无本组 listing → `ensure_controlled_listings` raise `ValueError` |
| 已有 listing 冲突 | 已存在 listing 指向别的 company → raise，且**不修改**该行（提交前后 `company_id` 不变） |
| 八家可解析 | `load_pilot_identities` 返回 8 条；`security_type`/`report_currency` 与设计一致；TSM `ads_ratio=="5"` |
| 市场行 | `ensure_market` 幂等，重复调用不新增行；timezone/currency 正确 |

定向：`PYTHONPATH=$PWD/src $PY -m pytest -q tests/unit/test_security_identity.py`

## 提交

```
feat(domain): controlled HK/US security identity and market registry
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总，本文件不写）
