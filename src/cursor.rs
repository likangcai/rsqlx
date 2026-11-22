// Copyright (C) rsqlx Contributors
// SPDX-License-Identifier: MIT OR Apache-2.0

//! PostgreSQL server-side cursor (streaming fetch in batches).

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use pyo3::prelude::*;
use pyo3::types::PyList;
use sqlx::pool::PoolConnection;
use sqlx::postgres::Postgres;
use tokio::sync::Mutex;

use crate::backend::{self, RowFactory};

static NEXT_CURSOR_ID: AtomicU64 = AtomicU64::new(1);

type SharedConn = Arc<Mutex<Option<PoolConnection<Postgres>>>>;

/// A PostgreSQL server-side cursor. Created by `pool.cursor(sql, batch_size)`.
/// Uses a dedicated connection; call `close()` when finished.
#[pyclass]
pub struct Cursor {
    conn: SharedConn,
    name: String,
    batch: u32,
    closed: bool,
}

#[pymethods]
impl Cursor {
    /// Fetch the next batch of rows (default row_factory = dict).
    #[pyo3(signature = (row_factory = None))]
    async fn fetch_batch(
        &self,
        row_factory: Option<String>,
    ) -> PyResult<Py<PyList>> {
        if self.closed {
            return Err(pyo3::exceptions::PyRuntimeError::new_err("cursor is closed"));
        }
        let rows = backend::pg_cursor_fetch(
            self.conn.clone(),
            self.name.clone(),
            self.batch,
        )
        .await?;
        Python::with_gil(|py| {
            backend::rows_to_list::<Postgres>(py, rows, RowFactory::parse(row_factory)?)
        })
    }

    /// Close the cursor and release the dedicated connection.
    async fn close(&mut self) -> PyResult<()> {
        self.closed = true;
        let conn = self.conn.clone();
        let name = self.name.clone();
        crate::runtime::run_db_task(async move {
            let taken = conn.lock().await.take();
            if let Some(mut c) = taken {
                let sql = format!("CLOSE {}", name);
                sqlx::query(sqlx::AssertSqlSafe(sql.as_str()))
                    .execute(&mut *c)
                    .await?;
                sqlx::query(sqlx::AssertSqlSafe("COMMIT"))
                    .execute(&mut *c)
                    .await?;
            }
            Ok::<(), sqlx::Error>(())
        })
        .await
    }
}

impl Cursor {
    pub(crate) fn new(conn: PoolConnection<Postgres>, name: String, batch: u32) -> Self {
        Self {
            conn: Arc::new(Mutex::new(Some(conn))),
            name,
            batch,
            closed: false,
        }
    }

    pub(crate) fn next_name() -> String {
        format!("rsqlx_cursor_{}", NEXT_CURSOR_ID.fetch_add(1, Ordering::Relaxed))
    }
}
