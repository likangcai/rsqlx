// Copyright (C) rsqlx Contributors
// SPDX-License-Identifier: MIT OR Apache-2.0

//! Backend dispatch: parameter binding, row decoding and generic query execution
//! for PostgreSQL / MySQL / SQLite.

use std::sync::Arc;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use pyo3::conversion::IntoPyObjectExt;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rust_decimal::Decimal;
use serde_json::Value;
use sqlx::mysql::{MySql, MySqlPool};
use sqlx::postgres::{PgPool, PgRow, Postgres};
use sqlx::query::Query;
use sqlx::sqlite::{Sqlite, SqlitePool, SqliteRow};
use sqlx::{Column, Database, Pool, Row, Transaction, TypeInfo, ValueRef as _};

use crate::params::PyParam;

pub(crate) type TxMutex<DB> = tokio::sync::Mutex<Option<Transaction<'static, DB>>>;

/// Which database a `Pool` talks to.
pub enum Backend {
    Pg(PgPool),
    MySql(MySqlPool),
    Sqlite(SqlitePool),
}

/// Which database a `Transaction` runs on.
pub enum TxB {
    Pg(Arc<TxMutex<Postgres>>),
    MySql(Arc<TxMutex<MySql>>),
    Sqlite(Arc<TxMutex<Sqlite>>),
}

// ---------------------------------------------------------------------------
// small Python-object helpers
// ---------------------------------------------------------------------------

fn py_none(py: Python<'_>) -> Bound<'_, PyAny> {
    py.None().into_bound(py)
}

macro_rules! pyv {
    ($py:expr, $v:expr) => {
        ($v).into_bound_py_any($py)?
    };
}

macro_rules! opt {
    ($row:expr, $i:expr, $t:ty) => {
        $row.try_get::<Option<$t>, usize>($i)
            .map_err(crate::error::sqlx_to_py)?
    };
}

macro_rules! arr {
    ($row:expr, $i:expr, $py:expr, $t:ty) => {{
        match opt!($row, $i, Vec<Option<$t>>) {
            None => Ok(py_none($py)),
            Some(vs) => {
                let list = PyList::empty($py);
                for x in vs {
                    list.append(x)?;
                }
                Ok(list.into_any())
            }
        }
    }};
}

pub fn decimal_to_py<'py>(py: Python<'py>, d: &Decimal) -> PyResult<Bound<'py, PyAny>> {
    let cls = py.import("decimal")?.getattr("Decimal")?;
    cls.call1((d.to_string(),))
}

pub fn uuid_to_py<'py>(py: Python<'py>, u: &uuid::Uuid) -> PyResult<Bound<'py, PyAny>> {
    let cls = py.import("uuid")?.getattr("UUID")?;
    cls.call1((u.to_string(),))
}

pub fn json_to_py<'py>(py: Python<'py>, v: &Value) -> PyResult<Bound<'py, PyAny>> {
    Ok(match v {
        Value::Null => py_none(py),
        Value::Bool(b) => pyv!(py, *b),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                pyv!(py, i)
            } else if let Some(u) = n.as_u64() {
                pyv!(py, u)
            } else {
                pyv!(py, n.as_f64().unwrap_or_default())
            }
        }
        Value::String(s) => pyv!(py, s.clone()),
        Value::Array(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(json_to_py(py, item)?)?;
            }
            list.into_any()
        }
        Value::Object(map) => {
            let dict = PyDict::new(py);
            for (k, item) in map {
                dict.set_item(k, json_to_py(py, item)?)?;
            }
            dict.into_any()
        }
    })
}

fn unsupported_type(db: &str, type_name: &str, col: &str) -> PyErr {
    crate::InterfaceError::new_err(format!(
        "unsupported {db} type '{type_name}' for column '{col}'; \
         consider casting the column in SQL (e.g. `expr::text`)"
    ))
}

// ---------------------------------------------------------------------------
// PostgreSQL decoding
// ---------------------------------------------------------------------------

fn pg_value_to_py<'py>(
    py: Python<'py>,
    row: &PgRow,
    i: usize,
    type_name: &str,
    col_name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    match type_name {
        "BOOL" => Ok(pyv!(py, opt!(row, i, bool))),
        "INT2" => Ok(pyv!(py, opt!(row, i, i16))),
        "INT4" => Ok(pyv!(py, opt!(row, i, i32))),
        "INT8" => Ok(pyv!(py, opt!(row, i, i64))),
        "FLOAT4" => Ok(pyv!(py, opt!(row, i, f32))),
        "FLOAT8" => Ok(pyv!(py, opt!(row, i, f64))),
        "NUMERIC" => match opt!(row, i, Decimal) {
            Some(d) => decimal_to_py(py, &d),
            None => Ok(py_none(py)),
        },
        "TEXT" | "NAME" | "VARCHAR" | "CHAR" | "\"CHAR\"" | "BPCHAR" | "UNKNOWN" => {
            match opt!(row, i, String) {
                Some(s) => Ok(pyv!(py, s)),
                None => Ok(py_none(py)),
            }
        }
        "BYTEA" => Ok(pyv!(py, opt!(row, i, Vec<u8>))),
        "JSON" | "JSONB" => match opt!(row, i, sqlx::types::Json<Value>) {
            Some(j) => json_to_py(py, &j.0),
            None => Ok(py_none(py)),
        },
        "UUID" => match opt!(row, i, uuid::Uuid) {
            Some(u) => uuid_to_py(py, &u),
            None => Ok(py_none(py)),
        },
        "DATE" => Ok(pyv!(py, opt!(row, i, NaiveDate))),
        "TIME" => Ok(pyv!(py, opt!(row, i, NaiveTime))),
        "TIMESTAMP" => Ok(pyv!(py, opt!(row, i, NaiveDateTime))),
        "TIMESTAMPTZ" => Ok(pyv!(py, opt!(row, i, DateTime<FixedOffset>))),
        "VOID" => Ok(py_none(py)),

        // arrays
        "BOOL[]" => arr!(row, i, py, bool),
        "INT2[]" => arr!(row, i, py, i16),
        "INT4[]" => arr!(row, i, py, i32),
        "INT8[]" => arr!(row, i, py, i64),
        "FLOAT4[]" => arr!(row, i, py, f32),
        "FLOAT8[]" => arr!(row, i, py, f64),
        "TEXT[]" | "VARCHAR[]" | "CHAR[]" | "\"CHAR\"[]" | "NAME[]" => arr!(row, i, py, String),
        "BYTEA[]" => arr!(row, i, py, Vec<u8>),
        "DATE[]" => arr!(row, i, py, NaiveDate),
        "TIME[]" => arr!(row, i, py, NaiveTime),
        "TIMESTAMP[]" => arr!(row, i, py, NaiveDateTime),
        "TIMESTAMPTZ[]" => arr!(row, i, py, DateTime<FixedOffset>),
        "NUMERIC[]" => match opt!(row, i, Vec<Option<Decimal>>) {
            None => Ok(py_none(py)),
            Some(vs) => {
                let list = PyList::empty(py);
                for x in vs {
                    match x {
                        Some(d) => list.append(decimal_to_py(py, &d)?)?,
                        None => list.append(py_none(py))?,
                    }
                }
                Ok(list.into_any())
            }
        },
        "UUID[]" => match opt!(row, i, Vec<Option<uuid::Uuid>>) {
            None => Ok(py_none(py)),
            Some(vs) => {
                let list = PyList::empty(py);
                for x in vs {
                    match x {
                        Some(u) => list.append(uuid_to_py(py, &u)?)?,
                        None => list.append(py_none(py))?,
                    }
                }
                Ok(list.into_any())
            }
        },
        "JSON[]" | "JSONB[]" => match opt!(row, i, Vec<Option<sqlx::types::Json<Value>>>) {
            None => Ok(py_none(py)),
            Some(vs) => {
                let list = PyList::empty(py);
                for x in vs {
                    match x {
                        Some(j) => list.append(json_to_py(py, &j.0)?)?,
                        None => list.append(py_none(py))?,
                    }
                }
                Ok(list.into_any())
            }
        },

        _ => Err(unsupported_type("PostgreSQL", type_name, col_name)),
    }
}

// ---------------------------------------------------------------------------
// MySQL decoding
// ---------------------------------------------------------------------------

fn mysql_value_to_py<'py>(
    py: Python<'py>,
    row: &sqlx::mysql::MySqlRow,
    i: usize,
    type_name: &str,
    col_name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    match type_name {
        "BOOLEAN" => Ok(pyv!(py, opt!(row, i, bool))),
        "TINYINT" => Ok(pyv!(py, opt!(row, i, i8))),
        "TINYINT UNSIGNED" => Ok(pyv!(py, opt!(row, i, u8))),
        "SMALLINT" => Ok(pyv!(py, opt!(row, i, i16))),
        "SMALLINT UNSIGNED" => Ok(pyv!(py, opt!(row, i, u16))),
        "INT" | "MEDIUMINT" => Ok(pyv!(py, opt!(row, i, i32))),
        "INT UNSIGNED" | "MEDIUMINT UNSIGNED" => Ok(pyv!(py, opt!(row, i, u32))),
        "BIGINT" => Ok(pyv!(py, opt!(row, i, i64))),
        "BIGINT UNSIGNED" => Ok(pyv!(py, opt!(row, i, u64))),
        "YEAR" => {
            let v: Option<u16> = opt!(row, i, u16);
            match v {
                Some(n) => Ok(pyv!(py, n as i64)),
                None => Ok(py_none(py)),
            }
        }
        "FLOAT" => Ok(pyv!(py, opt!(row, i, f32))),
        "DOUBLE" => Ok(pyv!(py, opt!(row, i, f64))),
        "DECIMAL" => match opt!(row, i, Decimal) {
            Some(d) => decimal_to_py(py, &d),
            None => Ok(py_none(py)),
        },
        "CHAR" | "VARCHAR" | "TINYTEXT" | "TEXT" | "MEDIUMTEXT" | "LONGTEXT" | "ENUM" | "SET" => {
            match opt!(row, i, String) {
                Some(s) => Ok(pyv!(py, s)),
                None => Ok(py_none(py)),
            }
        }
        "BINARY" | "VARBINARY" | "TINYBLOB" | "BLOB" | "MEDIUMBLOB" | "LONGBLOB" | "GEOMETRY"
        | "BIT" => Ok(pyv!(py, opt!(row, i, Vec<u8>))),
        "JSON" => match opt!(row, i, sqlx::types::Json<Value>) {
            Some(j) => json_to_py(py, &j.0),
            None => Ok(py_none(py)),
        },
        "DATE" => Ok(pyv!(py, opt!(row, i, NaiveDate))),
        "TIME" => Ok(pyv!(py, opt!(row, i, NaiveTime))),
        "DATETIME" => Ok(pyv!(py, opt!(row, i, NaiveDateTime))),
        "TIMESTAMP" => match opt!(row, i, DateTime<chrono::Utc>) {
            Some(dt) => Ok(pyv!(py, dt.naive_utc())),
            None => Ok(py_none(py)),
        },
        "NULL" => Ok(py_none(py)),
        _ => Err(unsupported_type("MySQL", type_name, col_name)),
    }
}

// ---------------------------------------------------------------------------
// SQLite decoding
// ---------------------------------------------------------------------------

fn sqlite_value_to_py<'py>(
    py: Python<'py>,
    row: &SqliteRow,
    i: usize,
    declared: &str,
    col_name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    // SQLite is dynamically typed: dispatch on the runtime type of the value,
    // using the declared column type only to sniff datetime-ish columns.
    let runtime = row
        .try_get_raw(i)
        .map_err(crate::error::sqlx_to_py)?
        .type_info()
        .name()
        .to_string();

    match runtime.as_str() {
        "NULL" => Ok(py_none(py)),
        "INTEGER" => Ok(pyv!(py, opt!(row, i, i64))),
        "REAL" => Ok(pyv!(py, opt!(row, i, f64))),
        "BLOB" => Ok(pyv!(py, opt!(row, i, Vec<u8>))),
        "TEXT" => match declared {
            "DATETIME" | "TIMESTAMP" => match row.try_get::<Option<NaiveDateTime>, usize>(i) {
                Ok(Some(dt)) => Ok(pyv!(py, dt)),
                _ => Ok(pyv!(py, opt!(row, i, String))),
            },
            "DATE" => match row.try_get::<Option<NaiveDate>, usize>(i) {
                Ok(Some(d)) => Ok(pyv!(py, d)),
                _ => Ok(pyv!(py, opt!(row, i, String))),
            },
            "TIME" => match row.try_get::<Option<NaiveTime>, usize>(i) {
                Ok(Some(t)) => Ok(pyv!(py, t)),
                _ => Ok(pyv!(py, opt!(row, i, String))),
            },
            _ => Ok(pyv!(py, opt!(row, i, String))),
        },
        _ => Err(unsupported_type("SQLite", &runtime, col_name)),
    }
}

// ---------------------------------------------------------------------------
// DbExt: per-database glue
// ---------------------------------------------------------------------------

pub trait DbExt: Database + Sized
where
    Self::Row: Send,
    Self::QueryResult: Send,
    for<'q> <Self as Database>::Arguments<'q>: sqlx::IntoArguments<'q, Self>,
{
    fn bind_param<'q>(
        q: Query<'q, Self, <Self as Database>::Arguments<'q>>,
        p: &PyParam,
    ) -> Query<'q, Self, <Self as Database>::Arguments<'q>>;

    /// Push a single parameter as a bind value onto a `Separated` builder
    /// (used by `QueryBuilder::push_values` for batch INSERTs).
    fn push_bind(
        sep: &mut sqlx::query_builder::Separated<'_, '_, Self, &'static str>,
        p: &PyParam,
    );

    fn row_to_dict(py: Python<'_>, row: &Self::Row) -> PyResult<Py<PyDict>>;
    fn result_summary(res: &Self::QueryResult) -> (u64, Option<i64>);
}

impl DbExt for Postgres {
    fn bind_param<'q>(
        q: Query<'q, Postgres, <Postgres as Database>::Arguments<'q>>,
        p: &PyParam,
    ) -> Query<'q, Postgres, <Postgres as Database>::Arguments<'q>> {
        match p {
            PyParam::Null => q.bind(PgUntypedNull),
            PyParam::Bool(v) => q.bind(*v),
            PyParam::Int(v) => q.bind(*v),
            PyParam::Float(v) => q.bind(*v),
            PyParam::Str(v) => q.bind(v.clone()),
            PyParam::Bytes(v) => q.bind(v.clone()),
            PyParam::Date(v) => q.bind(*v),
            PyParam::Time(v) => q.bind(*v),
            PyParam::DateTime(v) => q.bind(*v),
            PyParam::DateTimeTz(v) => q.bind(*v),
            PyParam::Decimal(v) => q.bind(*v),
            PyParam::Uuid(v) => q.bind(*v),
            PyParam::Json(v) => q.bind(sqlx::types::Json(v.clone())),
            PyParam::Array(items) => pg_bind_array(q, items),
        }
    }

    fn row_to_dict(py: Python<'_>, row: &PgRow) -> PyResult<Py<PyDict>> {
        let dict = PyDict::new(py);
        for (i, col) in row.columns().iter().enumerate() {
            let type_name = col.type_info().name();
            let value = pg_value_to_py(py, row, i, type_name, col.name())?;
            dict.set_item(col.name(), value)?;
        }
        Ok(dict.unbind())
    }

    fn push_bind(
        sep: &mut sqlx::query_builder::Separated<'_, '_, Postgres, &'static str>,
        p: &PyParam,
    ) {
        match p {
            PyParam::Null => { sep.push_bind(Option::<String>::None); }
            PyParam::Bool(v) => { sep.push_bind(*v); }
            PyParam::Int(v) => { sep.push_bind(*v); }
            PyParam::Float(v) => { sep.push_bind(*v); }
            PyParam::Str(v) => { sep.push_bind(v.clone()); }
            PyParam::Bytes(v) => { sep.push_bind(v.clone()); }
            PyParam::Date(v) => { sep.push_bind(*v); }
            PyParam::Time(v) => { sep.push_bind(*v); }
            PyParam::DateTime(v) => { sep.push_bind(*v); }
            PyParam::DateTimeTz(v) => { sep.push_bind(*v); }
            PyParam::Decimal(v) => { sep.push_bind(*v); }
            PyParam::Uuid(v) => { sep.push_bind(*v); }
            PyParam::Json(v) => { sep.push_bind(sqlx::types::Json(v.clone())); }
            PyParam::Array(items) => {
                let json = serde_json::Value::Array(items.iter().map(param_to_json_value).collect());
                sep.push_bind(sqlx::types::Json(json));
            }
        }
    }

    fn result_summary(res: &sqlx::postgres::PgQueryResult) -> (u64, Option<i64>) {
        (res.rows_affected(), None)
    }
}

/// Bind a Python list as a native PostgreSQL array.
///
/// The element type is inferred from the first non-null element; all
/// elements must share that type (or be `Null`). Heterogeneous arrays raise
/// a `TypeError` and should instead be passed as JSON.
fn pg_bind_array<'q>(
    q: Query<'q, Postgres, <Postgres as Database>::Arguments<'q>>,
    items: &[PyParam],
) -> Query<'q, Postgres, <Postgres as Database>::Arguments<'q>> {
    // Find the first non-null element to decide the array element type.
    let kind = items.iter().find(|p| !matches!(p, PyParam::Null)).cloned();
    match kind {
        None => q.bind(Vec::<Option<bool>>::new()),
        Some(PyParam::Bool(_)) => bind_array_t(q, items, |p| if let PyParam::Bool(v) = p { Some(*v) } else { None }),
        Some(PyParam::Int(_)) => bind_array_t(q, items, |p| if let PyParam::Int(v) = p { Some(*v) } else { None }),
        Some(PyParam::Float(_)) => bind_array_t(q, items, |p| if let PyParam::Float(v) = p { Some(*v) } else { None }),
        Some(PyParam::Str(_)) => bind_array_t(q, items, |p| if let PyParam::Str(v) = p { Some(v.clone()) } else { None }),
        Some(PyParam::Bytes(_)) => bind_array_t(q, items, |p| if let PyParam::Bytes(v) = p { Some(v.clone()) } else { None }),
        Some(PyParam::Date(_)) => bind_array_t(q, items, |p| if let PyParam::Date(v) = p { Some(*v) } else { None }),
        Some(PyParam::Time(_)) => bind_array_t(q, items, |p| if let PyParam::Time(v) = p { Some(*v) } else { None }),
        Some(PyParam::DateTime(_)) => bind_array_t(q, items, |p| if let PyParam::DateTime(v) = p { Some(*v) } else { None }),
        Some(PyParam::DateTimeTz(_)) => bind_array_t(q, items, |p| if let PyParam::DateTimeTz(v) = p { Some(*v) } else { None }),
        Some(PyParam::Decimal(_)) => bind_array_t(q, items, |p| if let PyParam::Decimal(v) = p { Some(*v) } else { None }),
        Some(PyParam::Uuid(_)) => bind_array_t(q, items, |p| if let PyParam::Uuid(v) = p { Some(*v) } else { None }),
        // Mixed/nested arrays should have been routed to JSON in params.rs;
        // this is a defensive fallback.
        _ => {
            let json = serde_json::Value::Array(
                items.iter().map(|p| param_to_json_value(p)).collect(),
            );
            q.bind(sqlx::types::Json(json))
        }
    }
}

fn bind_array_t<'q, T>(
    q: Query<'q, Postgres, <Postgres as Database>::Arguments<'q>>,
    items: &[PyParam],
    extract: impl Fn(&PyParam) -> Option<T>,
) -> Query<'q, Postgres, <Postgres as Database>::Arguments<'q>>
where
    T: 'q + sqlx::Encode<'q, Postgres> + sqlx::Type<Postgres> + sqlx::postgres::PgHasArrayType,
{
    let mut v: Vec<Option<T>> = Vec::with_capacity(items.len());
    for p in items {
        match p {
            PyParam::Null => v.push(None),
            other => match extract(other) {
                Some(t) => v.push(Some(t)),
                None => {
                    // heterogeneous -> fall back to JSON
                    let json = serde_json::Value::Array(
                        items.iter().map(param_to_json_value).collect(),
                    );
                    return q.bind(sqlx::types::Json(json));
                }
            },
        }
    }
    q.bind(v)
}

fn param_to_json_value(p: &PyParam) -> serde_json::Value {
    match p {
        PyParam::Null => serde_json::Value::Null,
        PyParam::Bool(b) => serde_json::Value::Bool(*b),
        PyParam::Int(i) => serde_json::Value::Number((*i).into()),
        PyParam::Float(f) => serde_json::Number::from_f64(*f).map(serde_json::Value::Number).unwrap_or(serde_json::Value::Null),
        PyParam::Str(s) => serde_json::Value::String(s.clone()),
        PyParam::Bytes(b) => serde_json::Value::Array(
            b.iter().map(|&x| serde_json::Value::Number((x as i64).into())).collect(),
        ),
        PyParam::Decimal(d) => serde_json::Value::String(d.to_string()),
        PyParam::Uuid(u) => serde_json::Value::String(u.to_string()),
        PyParam::Date(d) => serde_json::Value::String(d.to_string()),
        PyParam::Time(t) => serde_json::Value::String(t.to_string()),
        PyParam::DateTime(dt) => serde_json::Value::String(dt.to_string()),
        PyParam::DateTimeTz(dt) => serde_json::Value::String(dt.to_string()),
        PyParam::Json(v) => v.clone(),
        PyParam::Array(items) => serde_json::Value::Array(items.iter().map(param_to_json_value).collect()),
    }
}

/// A NULL parameter declared with PostgreSQL's `unknown` (OID 705) type.
///
/// This lets PG infer the concrete type from the query context (e.g. an
/// `int` column), so `None` can be bound to columns of any type — matching
/// the behaviour of `psycopg2` / `asyncpg`.
struct PgUntypedNull;

impl sqlx::Type<Postgres> for PgUntypedNull {
    fn type_info() -> sqlx::postgres::PgTypeInfo {
        // OID 705 is the built-in `unknown` (pseudo)type.
        sqlx::postgres::PgTypeInfo::with_oid(sqlx::postgres::types::Oid(705))
    }
}

impl<'q> sqlx::Encode<'q, Postgres> for PgUntypedNull {
    fn produces(&self) -> Option<sqlx::postgres::PgTypeInfo> {
        Some(sqlx::postgres::PgTypeInfo::with_oid(sqlx::postgres::types::Oid(705)))
    }
    fn encode_by_ref(&self, _buf: &mut sqlx::postgres::PgArgumentBuffer) -> std::result::Result<sqlx::encode::IsNull, sqlx::error::BoxDynError> {
        Ok(sqlx::encode::IsNull::Yes)
    }
}

impl DbExt for MySql {
    fn bind_param<'q>(
        q: Query<'q, MySql, <MySql as Database>::Arguments<'q>>,
        p: &PyParam,
    ) -> Query<'q, MySql, <MySql as Database>::Arguments<'q>> {
        match p {
            PyParam::Null => q.bind(Option::<String>::None),
            PyParam::Bool(v) => q.bind(*v),
            PyParam::Int(v) => q.bind(*v),
            PyParam::Float(v) => q.bind(*v),
            PyParam::Str(v) => q.bind(v.clone()),
            PyParam::Bytes(v) => q.bind(v.clone()),
            PyParam::Date(v) => q.bind(*v),
            PyParam::Time(v) => q.bind(*v),
            PyParam::DateTime(v) => q.bind(*v),
            // MySQL DATETIME has no timezone: bind the wall-clock portion.
            PyParam::DateTimeTz(v) => q.bind(v.naive_local()),
            PyParam::Decimal(v) => q.bind(*v),
            PyParam::Uuid(v) => q.bind(*v),
            PyParam::Json(v) => q.bind(sqlx::types::Json(v.clone())),
            PyParam::Array(items) => {
                let json = serde_json::Value::Array(items.iter().map(param_to_json_value).collect());
                q.bind(sqlx::types::Json(json))
            }
        }
    }

    fn row_to_dict(py: Python<'_>, row: &sqlx::mysql::MySqlRow) -> PyResult<Py<PyDict>> {
        let dict = PyDict::new(py);
        for (i, col) in row.columns().iter().enumerate() {
            let type_name = col.type_info().name();
            let value = mysql_value_to_py(py, row, i, type_name, col.name())?;
            dict.set_item(col.name(), value)?;
        }
        Ok(dict.unbind())
    }

    fn push_bind(
        sep: &mut sqlx::query_builder::Separated<'_, '_, MySql, &'static str>,
        p: &PyParam,
    ) {
        match p {
            PyParam::Null => { sep.push_bind(Option::<String>::None); }
            PyParam::Bool(v) => { sep.push_bind(*v); }
            PyParam::Int(v) => { sep.push_bind(*v); }
            PyParam::Float(v) => { sep.push_bind(*v); }
            PyParam::Str(v) => { sep.push_bind(v.clone()); }
            PyParam::Bytes(v) => { sep.push_bind(v.clone()); }
            PyParam::Date(v) => { sep.push_bind(*v); }
            PyParam::Time(v) => { sep.push_bind(*v); }
            PyParam::DateTime(v) => { sep.push_bind(*v); }
            PyParam::DateTimeTz(v) => { sep.push_bind(v.naive_local()); }
            PyParam::Decimal(v) => { sep.push_bind(*v); }
            PyParam::Uuid(v) => { sep.push_bind(*v); }
            PyParam::Json(v) => { sep.push_bind(sqlx::types::Json(v.clone())); }
            PyParam::Array(items) => {
                let json = serde_json::Value::Array(items.iter().map(param_to_json_value).collect());
                sep.push_bind(sqlx::types::Json(json));
            }
        }
    }

    fn result_summary(res: &sqlx::mysql::MySqlQueryResult) -> (u64, Option<i64>) {
        (res.rows_affected(), Some(res.last_insert_id() as i64))
    }
}

impl DbExt for Sqlite {
    fn bind_param<'q>(
        q: Query<'q, Sqlite, <Sqlite as Database>::Arguments<'q>>,
        p: &PyParam,
    ) -> Query<'q, Sqlite, <Sqlite as Database>::Arguments<'q>> {
        match p {
            PyParam::Null => q.bind(Option::<String>::None),
            PyParam::Bool(v) => q.bind(*v),
            PyParam::Int(v) => q.bind(*v),
            PyParam::Float(v) => q.bind(*v),
            PyParam::Str(v) => q.bind(v.clone()),
            PyParam::Bytes(v) => q.bind(v.clone()),
            PyParam::Date(v) => q.bind(*v),
            PyParam::Time(v) => q.bind(*v),
            PyParam::DateTime(v) => q.bind(*v),
            // SQLite has no timezone-aware storage: keep the wall-clock time.
            PyParam::DateTimeTz(v) => q.bind(v.naive_local()),
            // SQLite has no native DECIMAL: store as TEXT.
            PyParam::Decimal(v) => q.bind(v.to_string()),
            PyParam::Uuid(v) => q.bind(v.to_string()),
            PyParam::Json(v) => q.bind(sqlx::types::Json(v.clone())),
            PyParam::Array(items) => {
                let json = serde_json::Value::Array(items.iter().map(param_to_json_value).collect());
                q.bind(sqlx::types::Json(json))
            }
        }
    }

    fn row_to_dict(py: Python<'_>, row: &SqliteRow) -> PyResult<Py<PyDict>> {
        let dict = PyDict::new(py);
        for (i, col) in row.columns().iter().enumerate() {
            let declared = col.type_info().name();
            let value = sqlite_value_to_py(py, row, i, declared, col.name())?;
            dict.set_item(col.name(), value)?;
        }
        Ok(dict.unbind())
    }

    fn push_bind(
        sep: &mut sqlx::query_builder::Separated<'_, '_, Sqlite, &'static str>,
        p: &PyParam,
    ) {
        match p {
            PyParam::Null => { sep.push_bind(Option::<String>::None); }
            PyParam::Bool(v) => { sep.push_bind(*v); }
            PyParam::Int(v) => { sep.push_bind(*v); }
            PyParam::Float(v) => { sep.push_bind(*v); }
            PyParam::Str(v) => { sep.push_bind(v.clone()); }
            PyParam::Bytes(v) => { sep.push_bind(v.clone()); }
            PyParam::Date(v) => { sep.push_bind(*v); }
            PyParam::Time(v) => { sep.push_bind(*v); }
            PyParam::DateTime(v) => { sep.push_bind(*v); }
            PyParam::DateTimeTz(v) => { sep.push_bind(v.naive_local()); }
            PyParam::Decimal(v) => { sep.push_bind(v.to_string()); }
            PyParam::Uuid(v) => { sep.push_bind(v.to_string()); }
            PyParam::Json(v) => { sep.push_bind(sqlx::types::Json(v.clone())); }
            PyParam::Array(items) => {
                let json = serde_json::Value::Array(items.iter().map(param_to_json_value).collect());
                sep.push_bind(sqlx::types::Json(json));
            }
        }
    }

    fn result_summary(res: &sqlx::sqlite::SqliteQueryResult) -> (u64, Option<i64>) {
        (res.rows_affected(), Some(res.last_insert_rowid()))
    }
}

pub fn rows_to_list<DB>(py: Python<'_>, rows: Vec<DB::Row>) -> PyResult<Py<PyList>>
where
    DB: DbExt,
    DB::Row: Send,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
{
    let list = PyList::empty(py);
    for row in &rows {
        list.append(DB::row_to_dict(py, row)?)?;
    }
    Ok(list.unbind())
}

// ---------------------------------------------------------------------------
// generic query execution (pool)
// ---------------------------------------------------------------------------

pub async fn pool_fetch_rows<DB>(
    pool: Pool<DB>,
    sql: String,
    params: Vec<PyParam>,
) -> PyResult<Py<PyList>>
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    let rows = crate::runtime::run_db_task(async move {
        let mut q = sqlx::query::<DB>(&sql);
        for p in &params {
            q = DB::bind_param(q, p);
        }
        q.fetch_all(&pool).await
    })
    .await?;
    Python::with_gil(|py| rows_to_list::<DB>(py, rows))
}

pub async fn pool_fetch_optional<DB>(
    pool: Pool<DB>,
    sql: String,
    params: Vec<PyParam>,
) -> PyResult<Option<Py<PyDict>>>
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    let row = crate::runtime::run_db_task(async move {
        let mut q = sqlx::query::<DB>(&sql);
        for p in &params {
            q = DB::bind_param(q, p);
        }
        q.fetch_optional(&pool).await
    })
    .await?;
    match row {
        Some(row) => Python::with_gil(|py| DB::row_to_dict(py, &row).map(Some)),
        None => Ok(None),
    }
}

pub async fn pool_execute<DB>(
    pool: Pool<DB>,
    sql: String,
    params: Vec<PyParam>,
) -> PyResult<(u64, Option<i64>)>
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    let res = crate::runtime::run_db_task(async move {
        let mut q = sqlx::query::<DB>(&sql);
        for p in &params {
            q = DB::bind_param(q, p);
        }
        q.execute(&pool).await
    })
    .await?;
    Ok(DB::result_summary(&res))
}

pub async fn pool_execute_many<DB>(
    pool: Pool<DB>,
    sql: String,
    params: Vec<Vec<PyParam>>,
) -> PyResult<(u64, Option<i64>)>
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    // Optimisation: detect INSERT ... VALUES (...) and batch into a single
    // multi-row INSERT via QueryBuilder. Falls back to a loop for other
    // statement types.
    if let Some((prefix, placeholder_count, suffix)) = parse_insert_for_batch(&sql) {
        if params.iter().all(|p| p.len() == placeholder_count) && !params.is_empty() {
            return batch_insert(pool, prefix, suffix, placeholder_count, params).await;
        }
    }

    // Fallback: execute once per parameter set.
    crate::runtime::run_db_task(async move {
        let mut total: u64 = 0;
        let mut last_id = None;
        for set in &params {
            let mut q = sqlx::query::<DB>(&sql);
            for p in set {
                q = DB::bind_param(q, p);
            }
            let res = q.execute(&pool).await?;
            let (ra, id) = DB::result_summary(&res);
            total += ra;
            last_id = id;
        }
        Ok((total, last_id))
    })
    .await
}

/// Parse an INSERT statement into (prefix, placeholder_count, suffix) for
/// batch rewriting. Returns None if the SQL isn't a simple single-row INSERT.
///
/// Example input:  `INSERT INTO t (a, b) VALUES (?, ?)`
/// Returns:        ("INSERT INTO t (a, b) VALUES", 2, "")
fn parse_insert_for_batch(sql: &str) -> Option<(String, usize, String)> {
    let upper = sql.to_ascii_uppercase();
    let values_pos = upper.find("VALUES")?;
    // Check it's an INSERT
    if !upper.starts_with("INSERT") {
        return None;
    }
    let prefix = sql[..values_pos + "VALUES".len()].to_string();
    let rest = sql[values_pos + "VALUES".len()..].trim_start();

    // Expect ( ?, ?, ... ) possibly with trailing content (ON DUPLICATE KEY etc.)
    let open = rest.find('(')?;
    // Find matching close paren
    let mut depth = 0;
    let mut close = 0;
    for (i, c) in rest.char_indices() {
        match c {
            '(' => depth += 1,
            ')' => {
                depth -= 1;
                if depth == 0 {
                    close = i;
                    break;
                }
            }
            _ => {}
        }
    }
    if close == 0 {
        return None;
    }
    let tuple_str = &rest[1..close];
    let placeholder_count = tuple_str.matches('?').count();
    if placeholder_count == 0 {
        return None;
    }
    let suffix = rest[close + 1..].trim();
    Some((prefix, placeholder_count, suffix.to_string()))
}

/// Build and execute a single multi-row INSERT via `QueryBuilder::push_values`.
async fn batch_insert<DB>(
    pool: Pool<DB>,
    prefix: String,
    suffix: String,
    _placeholder_count: usize,
    params: Vec<Vec<PyParam>>,
) -> PyResult<(u64, Option<i64>)>
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    let res = crate::runtime::run_db_task(async move {
        // Manually build the multi-row INSERT SQL and bind all params.
        // This avoids QueryBuilder's lifetime issue across await points.
        let placeholder_count = params.first().map(|p| p.len()).unwrap_or(0);
        let placeholder_tuple = format!(
            "({})",
            (0..placeholder_count).map(|_| "?").collect::<Vec<_>>().join(", ")
        );
        let values: String = params
            .iter()
            .map(|_| placeholder_tuple.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        let full_sql = if suffix.is_empty() {
            format!("{} {}", prefix, values)
        } else {
            format!("{} {} {}", prefix, values, suffix)
        };

        // For MySQL/SQLite ? placeholders; for PostgreSQL we need $1, $2...
        let full_sql = rewrite_placeholders::<DB>(&full_sql, &params);

        let mut q = sqlx::query::<DB>(&full_sql);
        for set in &params {
            for p in set {
                q = DB::bind_param(q, p);
            }
        }
        q.execute(&pool).await
    })
    .await?;
    Ok(DB::result_summary(&res))
}

/// Rewrite `?` placeholders to the database's native format if needed
/// (PostgreSQL uses `$1, $2, ...`).
fn rewrite_placeholders<DB: Database>(sql: &str, params: &[Vec<PyParam>]) -> String {
    let is_pg = std::any::TypeId::of::<DB>() == std::any::TypeId::of::<Postgres>();
    if !is_pg {
        return sql.to_string();
    }
    let mut out = String::with_capacity(sql.len());
    let mut idx = 1;
    for ch in sql.chars() {
        if ch == '?' {
            out.push('$');
            out.push_str(&idx.to_string());
            idx += 1;
        } else {
            out.push(ch);
        }
    }
    out
}

pub async fn pool_close<DB: Database>(pool: Pool<DB>) -> PyResult<()> {
    crate::runtime::run_db_task(async move {
        pool.close().await;
        Ok::<(), sqlx::Error>(())
    })
    .await
}

pub async fn pool_migrate<DB>(pool: Pool<DB>, path: String) -> PyResult<()>
where
    DB: Database,
    DB::Connection: sqlx::migrate::Migrate,
{
    crate::runtime::run_db_task(async move {
        let migrator = sqlx::migrate::Migrator::new(std::path::Path::new(&path)).await?;
        migrator.run(&pool).await?;
        Ok::<(), sqlx::Error>(())
    })
    .await
}

/// Execute a raw SQL string (no parameter binding, uses the "simple query"
/// protocol / COM_QUERY instead of prepared statements).
///
/// This is needed for statements that MySQL doesn't support in the prepared
/// statement protocol: `DROP PROCEDURE`, `CREATE PROCEDURE`, `CALL`,
/// multi-statement scripts, etc.
pub async fn pool_execute_raw<DB>(pool: Pool<DB>, sql: String) -> PyResult<()>
where
    DB: Database,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    crate::runtime::run_db_task(async move {
        sqlx::raw_sql(&sql).execute(&pool).await?;
        Ok::<(), sqlx::Error>(())
    })
    .await
}

/// Fetch rows via a raw SQL string (simple query protocol, no parameters).
pub async fn pool_fetch_raw<DB>(pool: Pool<DB>, sql: String) -> PyResult<Py<PyList>>
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    let rows = crate::runtime::run_db_task(async move {
        sqlx::raw_sql(&sql).fetch_all(&pool).await
    })
    .await?;
    Python::with_gil(|py| rows_to_list::<DB>(py, rows))
}

pub fn pool_options<DB: Database>(
    min_connections: Option<u32>,
    max_connections: Option<u32>,
    acquire_timeout: Option<f64>,
    idle_timeout: Option<f64>,
    max_lifetime: Option<f64>,
) -> sqlx::pool::PoolOptions<DB> {
    let mut o = sqlx::pool::PoolOptions::<DB>::new();
    if let Some(v) = min_connections {
        o = o.min_connections(v);
    }
    if let Some(v) = max_connections {
        o = o.max_connections(v);
    }
    if let Some(v) = acquire_timeout {
        o = o.acquire_timeout(std::time::Duration::from_secs_f64(v));
    }
    if let Some(v) = idle_timeout {
        o = o.idle_timeout(Some(std::time::Duration::from_secs_f64(v)));
    }
    if let Some(v) = max_lifetime {
        o = o.max_lifetime(Some(std::time::Duration::from_secs_f64(v)));
    }
    o
}

// ---------------------------------------------------------------------------
// generic query execution (transaction)
// ---------------------------------------------------------------------------

pub async fn tx_fetch_rows<DB>(
    tx: Arc<TxMutex<DB>>,
    sql: String,
    params: Vec<PyParam>,
) -> PyResult<Py<PyList>>
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    let rows = crate::runtime::run_db_task(async move {
        let mut guard = tx.lock().await;
        let t = guard
            .as_mut()
            .ok_or_else(crate::error::tx_finished)?;
        let mut q = sqlx::query::<DB>(&sql);
        for p in &params {
            q = DB::bind_param(q, p);
        }
        q.fetch_all(&mut **t).await
    })
    .await?;
    Python::with_gil(|py| rows_to_list::<DB>(py, rows))
}

pub async fn tx_fetch_optional<DB>(
    tx: Arc<TxMutex<DB>>,
    sql: String,
    params: Vec<PyParam>,
) -> PyResult<Option<Py<PyDict>>>
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    let row = crate::runtime::run_db_task(async move {
        let mut guard = tx.lock().await;
        let t = guard
            .as_mut()
            .ok_or_else(crate::error::tx_finished)?;
        let mut q = sqlx::query::<DB>(&sql);
        for p in &params {
            q = DB::bind_param(q, p);
        }
        q.fetch_optional(&mut **t).await
    })
    .await?;
    match row {
        Some(row) => Python::with_gil(|py| DB::row_to_dict(py, &row).map(Some)),
        None => Ok(None),
    }
}

pub async fn tx_execute<DB>(
    tx: Arc<TxMutex<DB>>,
    sql: String,
    params: Vec<PyParam>,
) -> PyResult<(u64, Option<i64>)>
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    let res = crate::runtime::run_db_task(async move {
        let mut guard = tx.lock().await;
        let t = guard
            .as_mut()
            .ok_or_else(crate::error::tx_finished)?;
        let mut q = sqlx::query::<DB>(&sql);
        for p in &params {
            q = DB::bind_param(q, p);
        }
        q.execute(&mut **t).await
    })
    .await?;
    Ok(DB::result_summary(&res))
}

pub async fn tx_execute_many<DB>(
    tx: Arc<TxMutex<DB>>,
    sql: String,
    params: Vec<Vec<PyParam>>,
) -> PyResult<(u64, Option<i64>)>
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    crate::runtime::run_db_task(async move {
        let mut guard = tx.lock().await;
        let t = guard
            .as_mut()
            .ok_or_else(crate::error::tx_finished)?;
        let mut total: u64 = 0;
        let mut last_id = None;
        for set in &params {
            let mut q = sqlx::query::<DB>(&sql);
            for p in set {
                q = DB::bind_param(q, p);
            }
            let res = q.execute(&mut **t).await?;
            let (ra, id) = DB::result_summary(&res);
            total += ra;
            last_id = id;
        }
        Ok((total, last_id))
    })
    .await
}

pub async fn tx_commit<DB: Database>(tx: Arc<TxMutex<DB>>) -> PyResult<()> {
    crate::runtime::run_db_task(async move {
        let mut guard = tx.lock().await;
        match guard.take() {
            Some(t) => t.commit().await,
            None => Err(crate::error::tx_finished()),
        }
    })
    .await
}

pub async fn tx_rollback<DB: Database>(tx: Arc<TxMutex<DB>>) -> PyResult<()> {
    crate::runtime::run_db_task(async move {
        let mut guard = tx.lock().await;
        match guard.take() {
            Some(t) => t.rollback().await,
            None => Err(crate::error::tx_finished()),
        }
    })
    .await
}
