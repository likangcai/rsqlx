//! Python -> Rust parameter conversion.
//!
//! Parameters are eagerly converted to a Rust-side tagged value (`PyParam`)
//! while the GIL is held, so the actual sqlx work never has to touch Python.

use std::str::FromStr;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyByteArray, PyBytes, PyDate, PyDateTime, PyDict, PyFloat, PyInt, PyList, PyString, PyTime, PyTuple};
use rust_decimal::Decimal;
use serde_json::Value;

/// A database-agnostic, GIL-free representation of a bound Python parameter.
#[derive(Debug, Clone)]
pub enum PyParam {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Bytes(Vec<u8>),
    Date(NaiveDate),
    Time(NaiveTime),
    DateTime(NaiveDateTime),
    DateTimeTz(DateTime<FixedOffset>),
    Decimal(Decimal),
    Uuid(uuid::Uuid),
    /// A JSON-encodable value (dict / nested / mixed list).
    Json(Value),
    /// A homogeneous list of scalar values. Bound as a native array on
    /// PostgreSQL; serialized to JSON on MySQL / SQLite.
    Array(Vec<PyParam>),
}

/// True if a `PyParam` is a scalar that can live inside a PG array.
fn is_array_scalar(p: &PyParam) -> bool {
    matches!(
        p,
        PyParam::Null
            | PyParam::Bool(_)
            | PyParam::Int(_)
            | PyParam::Float(_)
            | PyParam::Str(_)
            | PyParam::Bytes(_)
            | PyParam::Date(_)
            | PyParam::Time(_)
            | PyParam::DateTime(_)
            | PyParam::DateTimeTz(_)
            | PyParam::Decimal(_)
            | PyParam::Uuid(_)
    )
}

/// Convert an optional Python iterable of parameters into bound values.
/// `None` (or Python `None`) yields an empty parameter list.
pub fn py_to_params(py: Python<'_>, params: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<PyParam>> {
    match params {
        None => Ok(Vec::new()),
        Some(obj) if obj.is_none() => Ok(Vec::new()),
        Some(obj) => {
            let mut out = Vec::new();
            for item in obj.try_iter()? {
                out.push(py_to_param(py, &item?)?);
            }
            Ok(out)
        }
    }
}

/// Convert a Python iterable of parameter sequences (`execute_many` style).
pub fn py_to_params_many(py: Python<'_>, params: &Bound<'_, PyAny>) -> PyResult<Vec<Vec<PyParam>>> {
    if params.is_none() {
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    for seq in params.try_iter()? {
        let seq = seq?;
        let mut set = Vec::new();
        for item in seq.try_iter()? {
            set.push(py_to_param(py, &item?)?);
        }
        out.push(set);
    }
    Ok(out)
}

/// Convert a single Python value into a `PyParam`.
pub fn py_to_param(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<PyParam> {
    // bool must be checked before int (bool is a subclass of int)
    if obj.is_none() {
        return Ok(PyParam::Null);
    }
    if obj.is_instance_of::<PyBool>() {
        return Ok(PyParam::Bool(obj.extract::<bool>()?));
    }
    if obj.is_instance_of::<PyInt>() {
        if let Ok(i) = obj.extract::<i64>() {
            return Ok(PyParam::Int(i));
        }
        // arbitrarily large Python ints degrade to DECIMAL via their string form
        let s = obj.str()?.to_str()?.to_string();
        return Decimal::from_str(&s)
            .map(PyParam::Decimal)
            .map_err(|_| {
                pyo3::exceptions::PyOverflowError::new_err(format!(
                    "integer parameter {s} cannot be represented"
                ))
            });
    }
    if obj.is_instance_of::<PyString>() {
        return Ok(PyParam::Str(obj.extract::<String>()?));
    }
    if obj.is_instance_of::<PyFloat>() {
        return Ok(PyParam::Float(obj.extract::<f64>()?));
    }
    if obj.is_instance_of::<PyBytes>() {
        return Ok(PyParam::Bytes(obj.downcast::<PyBytes>().unwrap().as_bytes().to_vec()));
    }
    if obj.is_instance_of::<PyByteArray>() {
        return Ok(PyParam::Bytes(obj.downcast::<PyByteArray>().unwrap().to_vec()));
    }
    // datetime must be checked before date (datetime is a subclass of date)
    if obj.is_instance_of::<PyDateTime>() {
        let aware = obj
            .call_method0("utcoffset")
            .map(|v| !v.is_none())
            .unwrap_or(false);
        if aware {
            let dt = obj.extract::<DateTime<FixedOffset>>()?;
            return Ok(PyParam::DateTimeTz(dt));
        }
        let dt = obj.extract::<NaiveDateTime>()?;
        return Ok(PyParam::DateTime(dt));
    }
    if obj.is_instance_of::<PyDate>() {
        return Ok(PyParam::Date(obj.extract::<NaiveDate>()?));
    }
    if obj.is_instance_of::<PyTime>() {
        return Ok(PyParam::Time(obj.extract::<NaiveTime>()?));
    }

    let type_name = obj
        .get_type()
        .name()
        .map(|n| n.to_string())
        .unwrap_or_default();
    match type_name.as_str() {
        "Decimal" => {
            let s = obj.str()?.to_str()?.to_string();
            Decimal::from_str(&s)
                .map(PyParam::Decimal)
                .map_err(|_| {
                    pyo3::exceptions::PyValueError::new_err(format!("invalid Decimal: {s}"))
                })
        }
        "UUID" => {
            let s = obj.str()?.to_str()?.to_string();
            uuid::Uuid::parse_str(&s)
                .map(PyParam::Uuid)
                .map_err(|_| {
                    pyo3::exceptions::PyValueError::new_err(format!("invalid UUID: {s}"))
                })
        }
        _ => {
            if obj.is_instance_of::<PyDict>() || obj.is_instance_of::<PyList>() || obj.is_instance_of::<PyTuple>() {
                // Try to treat homogeneous scalar lists/tuples as native PG arrays;
                // fall back to JSON for dicts or mixed/nested structures.
                if obj.is_instance_of::<PyDict>() {
                    return Ok(PyParam::Json(py_to_json(py, obj)?));
                }
                let mut elems: Vec<PyParam> = Vec::new();
                for item in obj.try_iter()? {
                    elems.push(py_to_param(py, &item?)?);
                }
                if elems.iter().all(is_array_scalar) {
                    Ok(PyParam::Array(elems))
                } else {
                    Ok(PyParam::Json(py_to_json(py, obj)?))
                }
            } else {
                Err(pyo3::exceptions::PyTypeError::new_err(format!(
                    "unsupported parameter type {type_name}; expected None, bool, int, float, str, bytes, \
                     bytearray, datetime.datetime, datetime.date, datetime.time, decimal.Decimal, uuid.UUID, \
                     dict, list or tuple"
                )))
            }
            }
    }
}

/// Recursively convert a Python object to `serde_json::Value`.
/// datetime/date/time/Decimal/UUID are stringified; NaN floats become null.
pub fn py_to_json(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    if obj.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(b) = obj.extract::<bool>() {
        return Ok(Value::Bool(b));
    }
    if obj.is_instance_of::<PyInt>() {
        if let Ok(i) = obj.extract::<i64>() {
            return Ok(Value::Number(i.into()));
        }
        return Ok(Value::String(obj.str()?.to_str()?.to_string()));
    }
    if obj.is_instance_of::<PyFloat>() {
        let f = obj.extract::<f64>()?;
        return Ok(serde_json::Number::from_f64(f).map(Value::Number).unwrap_or(Value::Null));
    }
    if obj.is_instance_of::<PyString>() {
        return Ok(Value::String(obj.extract::<String>()?));
    }
    if obj.is_instance_of::<PyDateTime>()
        || obj.is_instance_of::<PyDate>()
        || obj.is_instance_of::<PyTime>()
    {
        return Ok(Value::String(obj.str()?.to_str()?.to_string()));
    }
    let type_name = obj.get_type().name().map(|n| n.to_string()).unwrap_or_default();
    if type_name == "Decimal" || type_name == "UUID" {
        return Ok(Value::String(obj.str()?.to_str()?.to_string()));
    }
    if obj.is_instance_of::<PyList>() || obj.is_instance_of::<PyTuple>() {
        let mut arr = Vec::new();
        for item in obj.try_iter()? {
            arr.push(py_to_json(py, &item?)?);
        }
        return Ok(Value::Array(arr));
    }
    if obj.is_instance_of::<PyDict>() {
        let mut map = serde_json::Map::new();
        let items = obj.call_method0("items")?;
        for item in items.try_iter()? {
            let (k, v) = item?.extract::<(String, Py<PyAny>)>()?;
            map.insert(k, py_to_json(py, v.bind(py))?);
        }
        return Ok(Value::Object(map));
    }
    Err(pyo3::exceptions::PyTypeError::new_err(format!(
        "object of type {type_name} is not JSON serializable"
    )))
}
