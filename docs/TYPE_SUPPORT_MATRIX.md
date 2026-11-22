# rsqlx 与上游 sqlx 类型支持对照

本文档对照 **sqlx 0.9.0**（上游 Rust 库）与 **rsqlx 0.9.3**（Python 异步驱动）的类型支持。
rsqlx 是 sqlx 在 Python 侧的异步应用，不是 sqlx 源码的 fork；类型差异主要来自
Python 动态类型与 Rust 静态类型的映射边界，而非 sqlx 能力缺失。

图例：

- ✅ 完全支持
- ⚠️ 部分支持（精度、时区或语义有损失）
- ❌ 不支持（运行期明确抛 `InterfaceError`，提示用 `cast(col as text)`）
- ⛔ 未暴露（Rust 生态概念或产品范围外，不构成 rsqlx 缺陷）
- ➕ rsqlx 额外提供的便利能力

---

## 1. PostgreSQL

| 类型族 | sqlx 0.9.0 | rsqlx | 状态 |
|---|---|---|---|
| BOOL | `bool` | → `bool` | ✅ |
| INT2 / INT4 / INT8 | i16 / i32 / i64 | → `int` | ✅ |
| `"CHAR"`（单字节） | i8 | → `str`（落入字符串分支） | ⚠️ |
| OID | `Oid` | 无分支，明确报错 | ❌ |
| FLOAT4 / FLOAT8 | f32 / f64 | → `float` | ✅ |
| NUMERIC | BigDecimal + rust_decimal | 仅 rust_decimal → `Decimal`；超 96 位精度不可表达 | ⚠️ |
| TEXT / VARCHAR / CHAR / NAME / BPCHAR / UNKNOWN | String | → `str` | ✅ |
| CITEXT | `Citext` | 无分支，明确报错 | ❌ |
| BYTEA | `Vec<u8>` | → `bytes` | ✅ |
| VOID | `()` | → `None` | ✅ |
| UUID | `Uuid` | → `uuid.UUID`；UUID 对象可绑定 | ✅ |
| JSON / JSONB | `Json<T>` / `Value` / `RawValue` | → `dict` / `list` / 标量；绑定 dict 为单个 jsonb | ✅ |
| JSONB[] | `Vec<Json<T>>` | 读取 → `list`；写入用 `rsqlx.JsonArray([...])` | ✅（见第 4 节） |
| DATE / TIME / TIMESTAMP / TIMESTAMPTZ | chrono / time 类型 | → `date` / `time` / naive / aware `datetime` | ✅ |
| TIMETZ | `PgTimeTz` | 无分支，明确报错 | ❌ |
| INTERVAL | `PgInterval` | 明确报错 | ❌ |
| MONEY | `PgMoney` | 明确报错 | ❌ |
| INET / CIDR | ipnet / ipnetwork | 明确报错 | ❌ |
| MACADDR | mac_address | 明确报错 | ❌ |
| BIT / VARBIT | bit_vec | 明确报错 | ❌ |
| 几何（POINT / LINE / LSEG / BOX / PATH / POLYGON / CIRCLE 等） | PgCube 等 | 明确报错 | ❌ |
| HSTORE / LTREE / LQUERY | PgHstore / PgLTree / PgLQuery | 明确报错 | ❌ |
| range（INT4/8RANGE、TSRANGE、TSTZRANGE、DATERANGE、NUMRANGE） | Range<T> | 明确报错 | ❌ |
| domain / composite / enum / 匿名 record | `#[derive(Type)]` | 自定义 type_name 落入 default，明确报错 | ❌ |
| 元素数组（BOOL[]/INTn[]/FLOATn[]/TEXT[]/BYTEA[]/DATE[]/TIME[]/TIMESTAMP[]/TIMESTAMPTZ[]/NUMERIC[]/UUID[]/JSON[]/JSONB[]） | 原生数组 | 全部支持解码；标量列表可绑定为原生数组 | ✅ |
| 其它元素数组（OID[]/CITEXT[]/INET[]/MACADDR[]/BIT[]/INTERVAL[]/MONEY[]/range[]/几何[]/ltree[]/domain[]） | 原生数组 | 明确报错 | ❌ |
| 无类型 NULL 绑定 | 需显式类型 | `PgUntypedNull`（OID 705）让服务端推断，行为类似 psycopg2 | ➕ |

## 2. MySQL

| 类型族 | sqlx 0.9.0 | rsqlx | 状态 |
|---|---|---|---|
| BOOL / TINYINT(1) | bool | → `bool` | ✅ |
| 有符号 / 无符号整数（TINY/SMALL/MEDIUM/INT/BIG，含 UNSIGNED） | i8..i64 / u8..u64 | → `int` | ✅ |
| YEAR | Year | → `int` | ✅ |
| FLOAT / DOUBLE | f32 / f64 | → `float` | ✅ |
| DECIMAL | BigDecimal + rust_decimal | 仅 rust_decimal → `Decimal` | ⚠️ |
| CHAR / VARCHAR / TEXT 系列 / ENUM / SET | String | → `str` | ✅ |
| BIT | u8 / u64 | → `int`（本版本修复过解码 bug） | ✅ |
| BINARY / VARBINARY / BLOB 系列 | `Vec<u8>` | → `bytes` | ✅ |
| JSON | `Json<T>` / `Value` | → `dict` / `list` / 标量 | ✅ |
| DATE / DATETIME | NaiveDate / NaiveDateTime | → `date` / naive `datetime`；零日期经 `Option` 得 `None` | ✅ |
| TIME（含微秒、负数、±838:59:59） | `MySqlTime` 全范围 | 仅 `NaiveTime`（time-of-day）；超范围/负值报错 | ⚠️ |
| TIMESTAMP | `DateTime<Utc/Local>` | 解为 `DateTime<Utc>` 后转 naive → **时区丢失** | ⚠️ |
| UUID | `Uuid` ↔ BINARY(16)；Hyphenated ↔ CHAR(36) | 绑定 ✅（二进制）；解码 BINARY 只返回 `bytes`，**不还原 `uuid.UUID`** | ⚠️ |
| INET4 / INET6（MariaDB） | Ipv4Addr / Ipv6Addr | 明确报错 | ❌ |
| GEOMETRY | 上游无 Rust 解码器 | 明确报 `unsupported MySQL type 'GEOMETRY'` | ❌ |
| 原生数组 | MySQL 无此类型 | Python 列表绑定为单个 JSON 数组值 | ➕ |
| Arc / Box / Rc / Cow / Text 适配器 | 测试覆盖 | 无 Python 对应概念 | ⛔ |

## 3. SQLite

SQLite 侧按 **runtime storage class** 分发，declared type 仅用于日期时间嗅探。

| 类型族 | sqlx 0.9.0 | rsqlx | 状态 |
|---|---|---|---|
| NULL | `Option<T>` | → `None` | ✅ |
| BOOLEAN | bool | runtime `INTEGER` → `int`（非 `bool`） | ✅ |
| 整数族 | i8..i64 / u8..u32，u64 仅解码 | runtime `INTEGER` → `i64`；u64 超 i64 上限不可解 | ⚠️ |
| REAL / FLOAT | f32 / f64 | → `float` | ✅ |
| TEXT | String / &str | → `str` | ✅ |
| BLOB | `Vec<u8>` | → `bytes` | ✅ |
| DATETIME / TIMESTAMP | TEXT / INTEGER(unixepoch) / REAL(julianday) 三种格式 | 仅 declared ∈ {DATETIME, TIMESTAMP} 且 runtime=TEXT 时尝试解码；INTEGER/REAL 存的时间戳返回 `int`/`float` | ⚠️ |
| DATE / TIME | TEXT | declared=DATE/TIME 且 runtime=TEXT 时尝试；解析失败回退 `str` | ⚠️ |
| 时区感知 datetime | RFC3339 | 绑定时 `naive_local()` → **时区丢失**；解码无 aware 分支 | ⚠️ |
| JSON | `Json<T>` ↔ TEXT | 解码侧无 JSON 分支：TEXT 原样返回 `str`，不解析为 `dict`/`list` | ⚠️ |
| UUID | `Uuid` ↔ BLOB/TEXT | 绑定 → 字符串 ✅；解码 BLOB/TEXT → `bytes`/`str`，**不还原 `uuid.UUID`** | ⚠️ |
| NUMERIC / Decimal | 上游刻意不支持 | 绑定 Decimal → TEXT；解码回 `str` | ➕/⚠️ |
| 数组 | 无 | Python 列表绑定为 JSON 文本 | ➕ |
| BString（bstr feature） | `BString` ↔ BLOB | 以 `bytes` 返回 | ⛔/✅ |
| Arc / Box / Rc / Cow / Text 适配器 | 测试覆盖 | 无 Python 对应概念 | ⛔ |

## 4. `rsqlx.JsonArray` 使用说明

Python 没有静态类型来区分"单个 JSON 数组值"和"由 JSON 值组成的数组"，因此 rsqlx
保持向后兼容的默认行为，同时提供显式包装器以对齐 sqlx 的 `Vec<Json<T>>` 能力。

| Python 值 | 绑定为 | 目标列 |
|---|---|---|
| `{"k": 1}` | 单个 jsonb 对象 | `JSONB` |
| `[{"k": 1}, {"k": 2}]` | **单个 jsonb 数组值**（默认，行为不变） | `JSONB` |
| `[1, 2, 3]` | 原生 `INT4[]`（纯标量列表） | `INT4[]` / `NUMERIC[]` |
| `rsqlx.JsonArray([{"k": 1}, {"k": 2}, None])` | 原生 `JSONB[]` | `JSONB[]` |

```python
import rsqlx

# PostgreSQL JSONB[] 列
await pool.execute("CREATE TEMP TABLE docs (items JSONB[])")
await pool.execute(
    "INSERT INTO docs VALUES ($1)",
    [rsqlx.JsonArray([{"id": 1, "tags": ["a", "b"]}, {"id": 2}, None])],
)
rows = await pool.fetch("SELECT items FROM docs")
# rows == [{"items": [{"id": 1, "tags": ["a", "b"]}, {"id": 2}, None]}]
```

- `JsonArray` 接受任意可 JSON 序列化元素（dict / list / 标量 / `None`），嵌套结构原样保留
- MySQL / SQLite 无原生 JSON 数组类型，`JsonArray` 退化为普通 JSON 数组值
- 读取 `JSONB[]` / `JSON[]` 列无需包装器，rsqlx 自动解码为 `list`（保留 `None`）
- 上游依据：`sqlx-postgres/src/types/json.rs` 中 `impl<T> PgHasArrayType for Json<T>`

## 5. 通用约束

- 不支持的列类型统一抛 `rsqlx.InterfaceError`，消息形如
  `unsupported PostgreSQL type 'INET' for column 'x'; cast(col as text) to retrieve as string`
- 绑定侧仅接受白名单类型；不支持的 Python 类型抛 `TypeError`
- 超大 Python int（> 64 位）自动降级为 `Decimal`
- `NUMERIC` / `DECIMAL` 使用 `rust_decimal`（约 96 位精度），超出范围的更高精度值不支持
