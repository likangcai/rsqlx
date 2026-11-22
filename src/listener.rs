// Copyright (C) rsqlx Contributors
// SPDX-License-Identifier: MIT OR Apache-2.0

//! PostgreSQL LISTEN/NOTIFY listener (async).

use std::sync::Arc;

use pyo3::prelude::*;
use sqlx::postgres::{PgListener, PgNotification};
use tokio::sync::Mutex;

type SharedListener = Arc<Mutex<Option<PgListener>>>;

/// A PostgreSQL NOTIFY listener.  Uses a dedicated connection for the
/// lifetime of the object (sqlx `PgListener::connect_with` acquires and holds
/// one pooled connection).
#[pyclass]
pub struct NotificationListener {
    inner: SharedListener,
}

#[pymethods]
impl NotificationListener {
    /// Subscribe to a channel.
    async fn listen(&self, channel: String) -> PyResult<()> {
        let inner = self.inner.clone();
        crate::runtime::run_db_task(async move {
            let mut guard = inner.lock().await;
            let listener = guard
                .as_mut()
                .ok_or_else(|| sqlx::Error::Protocol("listener is closed".into()))?;
            listener.listen(&channel).await
        })
        .await
    }

    /// Unsubscribe from a channel.
    async fn unlisten(&self, channel: String) -> PyResult<()> {
        let inner = self.inner.clone();
        crate::runtime::run_db_task(async move {
            let mut guard = inner.lock().await;
            let listener = guard
                .as_mut()
                .ok_or_else(|| sqlx::Error::Protocol("listener is closed".into()))?;
            listener.unlisten(&channel).await
        })
        .await
    }

    /// Wait for the next notification.  Returns a tuple
    /// ``(channel, payload, process_id)``.
    async fn recv(&self) -> PyResult<(String, String, u32)> {
        let inner = self.inner.clone();
        let notification: PgNotification = crate::runtime::run_db_task(async move {
            let mut guard = inner.lock().await;
            let listener = guard
                .as_mut()
                .ok_or_else(|| sqlx::Error::Protocol("listener is closed".into()))?;
            listener.recv().await
        })
        .await?;
        Ok((
            notification.channel().to_string(),
            notification.payload().to_string(),
            notification.process_id(),
        ))
    }

    /// Close the listener (release the dedicated connection).
    async fn close(&self) -> PyResult<()> {
        let inner = self.inner.clone();
        crate::runtime::run_db_task(async move {
            *inner.lock().await = None;
            Ok::<(), sqlx::Error>(())
        })
        .await
    }
}

impl NotificationListener {
    pub(crate) fn new(inner: PgListener) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Some(inner))),
        }
    }
}
