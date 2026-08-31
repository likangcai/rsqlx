//! Map `sqlx::Error` (and join errors) onto the `rsqlx` exception hierarchy.

use pyo3::prelude::*;

use crate::{DatabaseError, InterfaceError, MigrateError, OperationalError, PoolClosed, PoolTimedOut, RowNotFound};

pub fn sqlx_to_py(e: sqlx::Error) -> PyErr {
    match e {
        sqlx::Error::RowNotFound => RowNotFound::new_err("no rows were returned by the query"),
        sqlx::Error::PoolTimedOut => {
            PoolTimedOut::new_err("timed out while waiting for a connection from the pool")
        }
        sqlx::Error::PoolClosed => PoolClosed::new_err("connection pool is closed"),
        sqlx::Error::Io(err) => OperationalError::new_err(format!("io error: {err}")),
        sqlx::Error::WorkerCrashed => {
            OperationalError::new_err("database worker thread crashed")
        }
        sqlx::Error::Database(db) => {
            let mut msg = db.message().to_string();
            if let Some(code) = db.code() {
                msg.push_str(&format!(" (code: {code})"));
            }
            DatabaseError::new_err(msg)
        }
        sqlx::Error::Migrate(m) => MigrateError::new_err(m.to_string()),
        sqlx::Error::Protocol(p) => InterfaceError::new_err(p),
        other => InterfaceError::new_err(other.to_string()),
    }
}

pub fn join_to_py(e: tokio::task::JoinError) -> PyErr {
    if e.is_panic() {
        OperationalError::new_err(format!("background database task panicked: {e}"))
    } else {
        OperationalError::new_err(format!("background database task cancelled: {e}"))
    }
}

pub fn tx_finished() -> sqlx::Error {
    sqlx::Error::Protocol("transaction is no longer active (already committed or rolled back)".into())
}
