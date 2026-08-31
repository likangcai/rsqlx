// Copyright (C) rsqlx Contributors
// SPDX-License-Identifier: MIT OR Apache-2.0

//! rsqlx — async PostgreSQL / MySQL / SQLite driver for Python,
//! powered by Rust's [sqlx](https://github.com/launchbadge/sqlx) crate.

mod backend;
mod error;
mod params;
mod pool;
mod runtime;
mod transaction;

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;

create_exception!(rsqlx, Error, PyException, "Base class for all rsqlx errors.");
create_exception!(
    rsqlx,
    InterfaceError,
    Error,
    "Error related to the database interface: unsupported types, decode failures, API misuse."
);
create_exception!(
    rsqlx,
    DatabaseError,
    Error,
    "Error reported by the database server (syntax errors, constraint violations, ...)."
);
create_exception!(
    rsqlx,
    OperationalError,
    Error,
    "Error related to database operations: io failures, worker crashes, background task failures."
);
create_exception!(
    rsqlx,
    RowNotFound,
    Error,
    "Raised by fetch_one() when the query returned no rows."
);
create_exception!(
    rsqlx,
    PoolTimedOut,
    Error,
    "Timed out waiting for a connection from the pool."
);
create_exception!(
    rsqlx,
    PoolClosed,
    Error,
    "Operation attempted on a closed pool."
);
create_exception!(
    rsqlx,
    MigrateError,
    Error,
    "Error while running database migrations."
);

#[pymodule]
fn rsqlx(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("Error", py.get_type::<Error>())?;
    m.add("InterfaceError", py.get_type::<InterfaceError>())?;
    m.add("DatabaseError", py.get_type::<DatabaseError>())?;
    m.add("OperationalError", py.get_type::<OperationalError>())?;
    m.add("RowNotFound", py.get_type::<RowNotFound>())?;
    m.add("PoolTimedOut", py.get_type::<PoolTimedOut>())?;
    m.add("PoolClosed", py.get_type::<PoolClosed>())?;
    m.add("MigrateError", py.get_type::<MigrateError>())?;

    m.add_class::<pool::Pool>()?;
    m.add_class::<pool::ExecuteResult>()?;
    m.add_class::<transaction::Transaction>()?;

    m.add_function(wrap_pyfunction!(pool::connect, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
