//! Global tokio runtime + GIL-releasing future wrapper.

use std::future::Future;
use std::pin::Pin;
use std::sync::OnceLock;
use std::task::{Context, Poll};

use pyo3::prelude::*;
use tokio::runtime::Runtime;
use tokio::task::JoinHandle;

static RUNTIME: OnceLock<Runtime> = OnceLock::new();

/// Lazily-initialized global multi-thread tokio runtime that drives all sqlx work.
pub fn runtime() -> &'static Runtime {
    RUNTIME.get_or_init(|| {
        Runtime::new().expect("failed to create tokio runtime for rsqlx")
    })
}

/// Spawn a Rust future onto the global runtime.
pub fn spawn<F>(fut: F) -> JoinHandle<F::Output>
where
    F: Future + Send + 'static,
    F::Output: Send + 'static,
{
    runtime().spawn(fut)
}

/// A future wrapper that releases the Python GIL while the inner future is pending.
///
/// The pyo3 `experimental-async` coroutine machinery holds the GIL across `.await`
/// points; wrapping the spawned tokio `JoinHandle` in `ReleaseGil` lets other
/// Python threads run while we wait for the database.
pub(crate) struct ReleaseGil<F>(F);

impl<F> Future for ReleaseGil<F>
where
    F: Future + Unpin + Send,
    F::Output: Send,
{
    type Output = F::Output;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.get_mut();
        let waker = cx.waker();
        Python::with_gil(|py| {
            py.allow_threads(|| Pin::new(&mut this.0).poll(&mut Context::from_waker(waker)))
        })
    }
}

/// Spawn `fut` on the runtime and await it with the GIL released.
/// Returns the future's output, mapping join/panics and sqlx errors to Python exceptions.
pub async fn run_db_task<T, F>(fut: F) -> PyResult<T>
where
    F: Future<Output = sqlx::Result<T>> + Send + 'static,
    T: Send + 'static,
{
    let handle = spawn(fut);
    let out = ReleaseGil(handle)
        .await
        .map_err(crate::error::join_to_py)?;
    out.map_err(crate::error::sqlx_to_py)
}
