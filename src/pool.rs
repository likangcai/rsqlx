//! `rsqlx.Pool`: connection pool + query entry points.

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use sqlx::mysql::MySql;
use sqlx::postgres::Postgres;
use sqlx::sqlite::Sqlite;

use crate::backend::{self, Backend};
use crate::params;
use crate::transaction::Transaction;

/// Result of an `execute()` / `execute_many()` call.
#[pyclass(frozen, get_all)]
pub struct ExecuteResult {
    pub rows_affected: u64,
    pub last_insert_id: Option<i64>,
}

#[pymethods]
impl ExecuteResult {
    fn __repr__(&self) -> String {
        format!(
            "ExecuteResult(rows_affected={}, last_insert_id={})",
            self.rows_affected,
            match self.last_insert_id {
                Some(v) => v.to_string(),
                None => "None".to_string(),
            }
        )
    }
}

/// An async connection pool to a PostgreSQL, MySQL or SQLite database.
///
/// Create with :func:`rsqlx.connect`.
#[pyclass]
pub struct Pool {
    pub(crate) inner: Backend,
}

impl Pool {
    async fn do_close(&self) -> PyResult<()> {
        match &self.inner {
            Backend::Pg(p) => backend::pool_close(p.clone()).await,
            Backend::MySql(p) => backend::pool_close(p.clone()).await,
            Backend::Sqlite(p) => backend::pool_close(p.clone()).await,
        }
    }
}

fn convert_params(params: Option<Py<PyAny>>) -> PyResult<Vec<params::PyParam>> {
    Python::with_gil(|py| params::py_to_params(py, params.as_ref().map(|p| p.bind(py))))
}

fn convert_params_many(params: Option<Py<PyAny>>) -> PyResult<Vec<Vec<params::PyParam>>> {
    Python::with_gil(|py| match params.as_ref() {
        None => Ok(Vec::new()),
        Some(p) => params::py_to_params_many(py, p.bind(py)),
    })
}

#[pymethods]
impl Pool {
    /// Execute a query and return all rows as a list of dicts.
    #[pyo3(signature = (query, params = None))]
    async fn fetch(&self, query: String, params: Option<Py<PyAny>>) -> PyResult<Py<PyList>> {
        let params = convert_params(params)?;
        match &self.inner {
            Backend::Pg(p) => backend::pool_fetch_rows::<Postgres>(p.clone(), query, params).await,
            Backend::MySql(p) => backend::pool_fetch_rows::<MySql>(p.clone(), query, params).await,
            Backend::Sqlite(p) => {
                backend::pool_fetch_rows::<Sqlite>(p.clone(), query, params).await
            }
        }
    }

    /// Execute a query and return exactly one row; raises `rsqlx.RowNotFound`
    /// if the query returned no rows.
    #[pyo3(signature = (query, params = None))]
    async fn fetch_one(&self, query: String, params: Option<Py<PyAny>>) -> PyResult<Py<PyDict>> {
        let row = self.fetch_optional(query, params).await?;
        row.ok_or_else(|| crate::RowNotFound::new_err("no rows were returned by the query"))
    }

    /// Execute a query and return one row or `None`.
    #[pyo3(signature = (query, params = None))]
    async fn fetch_optional(
        &self,
        query: String,
        params: Option<Py<PyAny>>,
    ) -> PyResult<Option<Py<PyDict>>> {
        let params = convert_params(params)?;
        match &self.inner {
            Backend::Pg(p) => {
                backend::pool_fetch_optional::<Postgres>(p.clone(), query, params).await
            }
            Backend::MySql(p) => {
                backend::pool_fetch_optional::<MySql>(p.clone(), query, params).await
            }
            Backend::Sqlite(p) => {
                backend::pool_fetch_optional::<Sqlite>(p.clone(), query, params).await
            }
        }
    }

    /// Execute a statement and return an `ExecuteResult` with the number of
    /// affected rows and (for MySQL / SQLite) the last insert id.
    #[pyo3(signature = (query, params = None))]
    async fn execute(&self, query: String, params: Option<Py<PyAny>>) -> PyResult<ExecuteResult> {
        let params = convert_params(params)?;
        let (rows_affected, last_insert_id) = match &self.inner {
            Backend::Pg(p) => backend::pool_execute::<Postgres>(p.clone(), query, params).await?,
            Backend::MySql(p) => backend::pool_execute::<MySql>(p.clone(), query, params).await?,
            Backend::Sqlite(p) => {
                backend::pool_execute::<Sqlite>(p.clone(), query, params).await?
            }
        };
        Ok(ExecuteResult {
            rows_affected,
            last_insert_id,
        })
    }

    /// Execute a statement once for each parameter set in `params`
    /// (an iterable of iterables) and return the accumulated `ExecuteResult`.
    #[pyo3(signature = (query, params))]
    async fn execute_many(&self, query: String, params: Py<PyAny>) -> PyResult<ExecuteResult> {
        let params = convert_params_many(Some(params))?;
        let (rows_affected, last_insert_id) = match &self.inner {
            Backend::Pg(p) => {
                backend::pool_execute_many::<Postgres>(p.clone(), query, params).await?
            }
            Backend::MySql(p) => {
                backend::pool_execute_many::<MySql>(p.clone(), query, params).await?
            }
            Backend::Sqlite(p) => {
                backend::pool_execute_many::<Sqlite>(p.clone(), query, params).await?
            }
        };
        Ok(ExecuteResult {
            rows_affected,
            last_insert_id,
        })
    }

    /// Start a transaction.
    ///
    /// Use as an async context manager to commit on success and roll back on
    /// exception:  `async with pool.begin() as tx: ...`
    async fn begin(&self) -> PyResult<Transaction> {
        let inner = match &self.inner {
            Backend::Pg(p) => {
                let pool = p.clone();
                let tx = crate::runtime::run_db_task(async move { pool.begin().await }).await?;
                backend::TxB::Pg(Arc::new(tokio::sync::Mutex::new(Some(tx))))
            }
            Backend::MySql(p) => {
                let pool = p.clone();
                let tx = crate::runtime::run_db_task(async move { pool.begin().await }).await?;
                backend::TxB::MySql(Arc::new(tokio::sync::Mutex::new(Some(tx))))
            }
            Backend::Sqlite(p) => {
                let pool = p.clone();
                let tx = crate::runtime::run_db_task(async move { pool.begin().await }).await?;
                backend::TxB::Sqlite(Arc::new(tokio::sync::Mutex::new(Some(tx))))
            }
        };
        Ok(Transaction { inner })
    }

    /// Execute a raw SQL string without parameter binding.
    ///
    /// Uses the "simple query" protocol (COM_QUERY) instead of prepared
    /// statements. Needed for statements that MySQL doesn't support in the
    /// prepared statement protocol: ``DROP PROCEDURE``, ``CREATE PROCEDURE``,
    /// ``CALL``, multi-statement scripts, etc.
    ///
    /// **Warning**: since there is no parameter binding, do NOT interpolate
    /// user input into the SQL string — use :meth:`execute` with ``?``
    /// placeholders for that.
    async fn execute_raw(&self, sql: String) -> PyResult<()> {
        match &self.inner {
            Backend::Pg(p) => backend::pool_execute_raw(p.clone(), sql).await,
            Backend::MySql(p) => backend::pool_execute_raw(p.clone(), sql).await,
            Backend::Sqlite(p) => backend::pool_execute_raw(p.clone(), sql).await,
        }
    }

    /// Execute a raw SQL string and return rows as a list of dicts.
    ///
    /// Like :meth:`execute_raw`, uses the simple query protocol (no
    /// parameter binding). Useful for stored procedure calls that return
    /// result sets.
    async fn fetch_raw(&self, sql: String) -> PyResult<Py<PyList>> {
        match &self.inner {
            Backend::Pg(p) => backend::pool_fetch_raw::<Postgres>(p.clone(), sql).await,
            Backend::MySql(p) => backend::pool_fetch_raw::<MySql>(p.clone(), sql).await,
            Backend::Sqlite(p) => backend::pool_fetch_raw::<Sqlite>(p.clone(), sql).await,
        }
    }

    /// Run sqlx migrations from a directory of `*.sql` files
    /// (`<N>_<description>.up.sql`, optionally with `.down.sql` files).
    async fn migrate(&self, path: String) -> PyResult<()> {
        match &self.inner {
            Backend::Pg(p) => backend::pool_migrate::<Postgres>(p.clone(), path).await,
            Backend::MySql(p) => backend::pool_migrate::<MySql>(p.clone(), path).await,
            Backend::Sqlite(p) => backend::pool_migrate::<Sqlite>(p.clone(), path).await,
        }
    }

    /// Close the pool; all subsequent operations raise `rsqlx.PoolClosed`.
    async fn close(&self) -> PyResult<()> {
        self.do_close().await
    }

    /// Number of connections currently held by the pool.
    #[getter]
    fn size(&self) -> u32 {
        match &self.inner {
            Backend::Pg(p) => p.size(),
            Backend::MySql(p) => p.size(),
            Backend::Sqlite(p) => p.size(),
        }
    }

    /// Number of idle connections in the pool.
    #[getter]
    fn num_idle(&self) -> usize {
        match &self.inner {
            Backend::Pg(p) => p.num_idle(),
            Backend::MySql(p) => p.num_idle(),
            Backend::Sqlite(p) => p.num_idle(),
        }
    }

    /// Whether the pool is closed.
    #[getter]
    fn is_closed(&self) -> bool {
        match &self.inner {
            Backend::Pg(p) => p.is_closed(),
            Backend::MySql(p) => p.is_closed(),
            Backend::Sqlite(p) => p.is_closed(),
        }
    }

    fn __repr__(&self) -> String {
        let (backend, size, idle) = match &self.inner {
            Backend::Pg(p) => ("postgresql", p.size(), p.num_idle()),
            Backend::MySql(p) => ("mysql", p.size(), p.num_idle()),
            Backend::Sqlite(p) => ("sqlite", p.size(), p.num_idle()),
        };
        format!("Pool(backend={backend}, size={size}, idle={idle})")
    }

    async fn __aenter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    async fn __aexit__(&self, _exc_type: PyObject, _exc_value: PyObject, _traceback: PyObject) -> PyResult<()> {
        self.do_close().await
    }
}

/// Create a connection pool.
///
/// :param url: database URL, e.g. ``postgres://user:pass@host/db``,
///     ``mysql://user:pass@host/db`` or ``sqlite:db.sqlite3`` /
///     ``sqlite::memory:``.
/// :param min_connections: minimum number of connections to keep open.
/// :param max_connections: maximum number of connections (default 10).
/// :param acquire_timeout: seconds to wait for a connection before raising
///     `rsqlx.PoolTimedOut` (default 30).
/// :param idle_timeout: seconds before idle connections are reaped.
/// :param max_lifetime: maximum lifetime of a connection, in seconds.
#[pyfunction]
#[pyo3(signature = (url, *, min_connections=None, max_connections=None, acquire_timeout=None, idle_timeout=None, max_lifetime=None))]
pub async fn connect(
    url: String,
    min_connections: Option<u32>,
    max_connections: Option<u32>,
    acquire_timeout: Option<f64>,
    idle_timeout: Option<f64>,
    max_lifetime: Option<f64>,
) -> PyResult<Pool> {
    for (name, value) in [
        ("acquire_timeout", acquire_timeout),
        ("idle_timeout", idle_timeout),
        ("max_lifetime", max_lifetime),
    ] {
        if let Some(v) = value {
            if !v.is_finite() || v < 0.0 {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "{name} must be a non-negative number of seconds"
                )));
            }
        }
    }

    let mut url = url;
    if url == "sqlite://:memory:" {
        url = "sqlite::memory:".to_string();
    }

    let lower = url.to_ascii_lowercase();
    let inner = if lower.starts_with("postgres://") || lower.starts_with("postgresql://") {
        let url = url;
        let pool = crate::runtime::run_db_task(async move {
            backend::pool_options::<Postgres>(
                min_connections,
                max_connections,
                acquire_timeout,
                idle_timeout,
                max_lifetime,
            )
            .connect(&url)
            .await
        })
        .await?;
        Backend::Pg(pool)
    } else if lower.starts_with("mysql://") {
        let url = url;
        let pool = crate::runtime::run_db_task(async move {
            backend::pool_options::<MySql>(
                min_connections,
                max_connections,
                acquire_timeout,
                idle_timeout,
                max_lifetime,
            )
            .connect(&url)
            .await
        })
        .await?;
        Backend::MySql(pool)
    } else if lower.starts_with("sqlite:") {
        let mut url = url;
        // Python-friendly default: create the database file if it doesn't exist
        // (like the stdlib sqlite3), unless the user pinned a mode explicitly.
        if !url.contains("mode=") {
            url.push(if url.contains('?') { '&' } else { '?' });
            url.push_str("mode=rwc");
        }
        let pool = crate::runtime::run_db_task(async move {
            backend::pool_options::<Sqlite>(
                min_connections,
                max_connections,
                acquire_timeout,
                idle_timeout,
                max_lifetime,
            )
            .connect(&url)
            .await
        })
        .await?;
        Backend::Sqlite(pool)
    } else {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "unsupported database URL: expected postgres://, postgresql://, mysql:// or sqlite: prefix",
        ));
    };

    Ok(Pool { inner })
}
