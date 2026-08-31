//! `rsqlx.Transaction`: a database transaction.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use sqlx::mysql::MySql;
use sqlx::postgres::Postgres;
use sqlx::sqlite::Sqlite;

use crate::backend::{self, TxB};
use crate::params;

/// A transaction started with `pool.begin()`.
///
/// Use as an async context manager for automatic commit / rollback:
///
///     async with pool.begin() as tx:
///         await tx.execute("...")
#[pyclass]
pub struct Transaction {
    pub(crate) inner: TxB,
}

impl Transaction {
    async fn do_commit(&self) -> PyResult<()> {
        match &self.inner {
            TxB::Pg(t) => backend::tx_commit(t.clone()).await,
            TxB::MySql(t) => backend::tx_commit(t.clone()).await,
            TxB::Sqlite(t) => backend::tx_commit(t.clone()).await,
        }
    }

    async fn do_rollback(&self) -> PyResult<()> {
        match &self.inner {
            TxB::Pg(t) => backend::tx_rollback(t.clone()).await,
            TxB::MySql(t) => backend::tx_rollback(t.clone()).await,
            TxB::Sqlite(t) => backend::tx_rollback(t.clone()).await,
        }
    }
}

fn convert_params(params: Option<Py<PyAny>>) -> PyResult<Vec<params::PyParam>> {
    Python::with_gil(|py| params::py_to_params(py, params.as_ref().map(|p| p.bind(py))))
}

#[pymethods]
impl Transaction {
    /// Execute a query inside the transaction, returning all rows.
    #[pyo3(signature = (query, params = None))]
    async fn fetch(&self, query: String, params: Option<Py<PyAny>>) -> PyResult<Py<PyList>> {
        let params = convert_params(params)?;
        match &self.inner {
            TxB::Pg(t) => backend::tx_fetch_rows::<Postgres>(t.clone(), query, params).await,
            TxB::MySql(t) => backend::tx_fetch_rows::<MySql>(t.clone(), query, params).await,
            TxB::Sqlite(t) => backend::tx_fetch_rows::<Sqlite>(t.clone(), query, params).await,
        }
    }

    /// Execute a query inside the transaction, returning exactly one row.
    #[pyo3(signature = (query, params = None))]
    async fn fetch_one(&self, query: String, params: Option<Py<PyAny>>) -> PyResult<Py<PyDict>> {
        let row = self.fetch_optional(query, params).await?;
        row.ok_or_else(|| crate::RowNotFound::new_err("no rows were returned by the query"))
    }

    /// Execute a query inside the transaction, returning one row or `None`.
    #[pyo3(signature = (query, params = None))]
    async fn fetch_optional(
        &self,
        query: String,
        params: Option<Py<PyAny>>,
    ) -> PyResult<Option<Py<PyDict>>> {
        let params = convert_params(params)?;
        match &self.inner {
            TxB::Pg(t) => backend::tx_fetch_optional::<Postgres>(t.clone(), query, params).await,
            TxB::MySql(t) => backend::tx_fetch_optional::<MySql>(t.clone(), query, params).await,
            TxB::Sqlite(t) => {
                backend::tx_fetch_optional::<Sqlite>(t.clone(), query, params).await
            }
        }
    }

    /// Execute a statement inside the transaction.
    #[pyo3(signature = (query, params = None))]
    async fn execute(&self, query: String, params: Option<Py<PyAny>>) -> PyResult<crate::pool::ExecuteResult> {
        let params = convert_params(params)?;
        let (rows_affected, last_insert_id) = match &self.inner {
            TxB::Pg(t) => backend::tx_execute::<Postgres>(t.clone(), query, params).await?,
            TxB::MySql(t) => backend::tx_execute::<MySql>(t.clone(), query, params).await?,
            TxB::Sqlite(t) => backend::tx_execute::<Sqlite>(t.clone(), query, params).await?,
        };
        Ok(crate::pool::ExecuteResult {
            rows_affected,
            last_insert_id,
        })
    }

    /// Execute a statement once for each parameter set inside the transaction.
    #[pyo3(signature = (query, params))]
    async fn execute_many(
        &self,
        query: String,
        params: Py<PyAny>,
    ) -> PyResult<crate::pool::ExecuteResult> {
        let params = Python::with_gil(|py| params::py_to_params_many(py, params.bind(py)))?;
        let (rows_affected, last_insert_id) = match &self.inner {
            TxB::Pg(t) => {
                backend::tx_execute_many::<Postgres>(t.clone(), query, params).await?
            }
            TxB::MySql(t) => backend::tx_execute_many::<MySql>(t.clone(), query, params).await?,
            TxB::Sqlite(t) => {
                backend::tx_execute_many::<Sqlite>(t.clone(), query, params).await?
            }
        };
        Ok(crate::pool::ExecuteResult {
            rows_affected,
            last_insert_id,
        })
    }

    /// Commit the transaction. It cannot be used afterwards.
    async fn commit(&self) -> PyResult<()> {
        self.do_commit().await
    }

    /// Roll back the transaction. It cannot be used afterwards.
    async fn rollback(&self) -> PyResult<()> {
        self.do_rollback().await
    }

    fn __repr__(&self) -> String {
        let db = match &self.inner {
            TxB::Pg(_) => "postgresql",
            TxB::MySql(_) => "mysql",
            TxB::Sqlite(_) => "sqlite",
        };
        format!("Transaction(backend={db})")
    }

    async fn __aenter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    async fn __aexit__(
        &self,
        exc_type: Option<PyObject>,
        _exc_value: PyObject,
        _traceback: PyObject,
    ) -> PyResult<bool> {
        if exc_type.is_none() {
            self.do_commit().await?;
        } else {
            self.do_rollback().await?;
        }
        Ok(false)
    }
}
