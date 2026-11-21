# Copyright (C) rsqlx Contributors
# SPDX-License-Identifier: MIT OR Apache-2.0
"""A synchronous, DBAPI-shaped adapter over rsqlx's async Pool/Transaction API.

This module never runs its own event loop.  Every coroutine is driven with
SQLAlchemy's ``await_only`` so execution stays on the greenlet that owns the
asyncio event loop.
"""

from __future__ import annotations

import datetime as _dt
import decimal as _decimal
import re
import sqlite3 as _sqlite3
from collections import deque
from typing import Any, Deque, Iterable, Optional, Sequence

import rsqlx
from sqlalchemy.util.concurrency import await_fallback, await_only

# ---------------------------------------------------------------------------
# DBAPI exception hierarchy
# ---------------------------------------------------------------------------


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


_CODE_RE = re.compile(r"\(code:\s*([^)]+)\)\s*$")


def _code_from_message(message: str) -> Optional[str]:
    match = _CODE_RE.search(message)
    return match.group(1) if match else None


def _classify(code: Optional[str]) -> type:
    if not code:
        return DatabaseError
    text = str(code)
    # PostgreSQL SQLSTATE classes.
    if text.startswith("23"):
        return IntegrityError
    if text.startswith("22"):
        return DataError
    if text.startswith("42"):
        return ProgrammingError
    # MySQL error numbers.
    if text in {"1062", "1216", "1217", "1451", "1452", "1586"}:
        return IntegrityError
    if text in {"1264", "1265", "1406", "1411"}:
        return DataError
    if text in {"1054", "1064", "1146"}:
        return ProgrammingError
    # SQLite extended result codes: the low byte is the primary result code.
    if text.isdigit():
        number = int(text)
        if number <= 0xFFFF:
            primary = number & 0xFF
            if primary == 19:
                return IntegrityError
            if primary == 1:
                return ProgrammingError
            if primary == 5:
                return OperationalError
    return DatabaseError


def translate_exception(error: BaseException) -> Optional[BaseException]:
    """Map an rsqlx exception to a DBAPI exception; ``None`` means "not an error"."""
    if isinstance(error, Error):
        return error
    if isinstance(error, rsqlx.RowNotFound):
        return None
    if isinstance(error, (rsqlx.PoolTimedOut, rsqlx.PoolClosed, rsqlx.OperationalError)):
        return OperationalError(str(error))
    if isinstance(error, rsqlx.MigrateError):
        return DatabaseError(str(error))
    if isinstance(error, rsqlx.DatabaseError):
        code = _code_from_message(str(error))
        translated = _classify(code)(str(error))
        setattr(translated, "code", code)
        return translated
    if isinstance(error, rsqlx.InterfaceError):
        return ProgrammingError(str(error))
    if isinstance(error, rsqlx.Error):
        return DatabaseError(str(error))
    return error


# ---------------------------------------------------------------------------
# cursor
# ---------------------------------------------------------------------------

_ROW_PREFIXES = (
    "SELECT",
    "WITH",
    "PRAGMA",
    "SHOW",
    "EXPLAIN",
    "VALUES",
    "DESCRIBE",
    "DESC",
)


def _is_row_statement(sql: str) -> bool:
    """Routing hint only — never rewrites the SQL string."""
    upper = sql.lstrip().upper()
    if " RETURNING" in upper:
        return True
    return any(upper.startswith(prefix) for prefix in _ROW_PREFIXES)


def _description_from_dicts(rows: Sequence[dict]) -> Optional[tuple]:
    """Build DBAPI description from rsqlx dict rows (original column names)."""
    if not rows:
        return None
    return tuple((name, None, None, None, None, None, None) for name in rows[0])


class AsyncAdapt_rsqlx_cursor:
    server_side = False

    def __init__(self, adapt_connection: "AsyncAdapt_rsqlx_connection"):
        self._adapt_connection = adapt_connection
        self._rows: Deque[Any] = deque()
        self.description: Optional[tuple] = None
        self.rowcount = -1
        self.lastrowid = None
        self.arraysize = 1

    async def _async_soft_close(self) -> None:
        return

    async def _async_execute(self, operation: str, parameters: Any) -> None:
        conn = self._adapt_connection
        target = await conn._get_target()

        if _is_row_statement(operation):
            if parameters is None:
                rows = await target.fetch(operation, row_factory="dict")
            else:
                rows = await target.fetch(operation, parameters, row_factory="dict")
            # SQLAlchemy needs the original column names for Row._mapping, so
            # keep dict rows (which preserve them) and convert to tuples here.
            self.description = _description_from_dicts(rows)
            if self.description is None:
                # Empty result sets still need column metadata for SQLAlchemy's
                # Row._mapping.  rsqlx.describe() prepares the statement without
                # fetching rows; no SQL rewriting is involved.
                try:
                    names = await target.describe(operation)
                    if names:
                        self.description = tuple(
                            (name, None, None, None, None, None, None) for name in names
                        )
                except Exception:
                    self.description = None
            self._rows = deque(tuple(row.values()) for row in rows)
            self.rowcount = len(rows)
            self.lastrowid = None
        else:
            if parameters is None:
                result = await target.execute(operation)
            else:
                result = await target.execute(operation, parameters)
            self.description = None
            self.rowcount = result.rows_affected
            self.lastrowid = result.last_insert_id
            self._rows.clear()

    async def _async_executemany(
        self, operation: str, seq_of_parameters: Iterable[Any]
    ) -> None:
        conn = self._adapt_connection
        target = await conn._get_target()
        params = [tuple(p) if isinstance(p, (list, tuple)) else (p,) for p in seq_of_parameters]
        result = await target.execute_many(operation, params)
        self.description = None
        self.rowcount = result.rows_affected
        self.lastrowid = result.last_insert_id
        self._rows.clear()

    def execute(self, operation: str, parameters: Any = None) -> None:
        try:
            self._adapt_connection.await_(self._async_execute(operation, parameters))
        except Exception as error:
            self._adapt_connection._handle_exception(error)

    def executemany(self, operation: str, seq_of_parameters: Iterable[Any]) -> None:
        try:
            self._adapt_connection.await_(
                self._async_executemany(operation, seq_of_parameters)
            )
        except Exception as error:
            self._adapt_connection._handle_exception(error)

    def setinputsizes(self, *sizes: Any) -> None:
        pass

    def setoutputsize(self, *args: Any) -> None:
        pass

    def close(self) -> None:
        self._rows.clear()
        self.description = None

    def __iter__(self):
        while self._rows:
            yield self._rows.popleft()

    def fetchone(self) -> Any:
        return self._rows.popleft() if self._rows else None

    def fetchmany(self, size: Optional[int] = None) -> Sequence[Any]:
        if size is None:
            size = self.arraysize
        return [self._rows.popleft() for _ in range(min(size, len(self._rows)))]

    def fetchall(self) -> Sequence[Any]:
        rows = list(self._rows)
        self._rows.clear()
        return rows


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------


class AsyncAdapt_rsqlx_connection:
    await_ = staticmethod(await_only)

    def __init__(self, dbapi: "AsyncAdapt_rsqlx_dbapi", pool: Any):
        self.dbapi = dbapi
        self._pool = pool
        self._tx: Any = None
        self.autocommit = False
        self._closed = False

    @property
    def isolation_level(self) -> str:
        return "AUTOCOMMIT" if self.autocommit else "DEFERRED"

    @isolation_level.setter
    def isolation_level(self, value: Optional[str]) -> None:
        self.autocommit = value == "AUTOCOMMIT" or value is None

    async def _get_target(self) -> Any:
        if self.autocommit:
            return self._pool
        if self._tx is None:
            self._tx = await self._pool.begin()
        return self._tx

    def cursor(self, server_side: bool = False) -> AsyncAdapt_rsqlx_cursor:
        return AsyncAdapt_rsqlx_cursor(self)

    def execute(self, operation: str, parameters: Any = None) -> AsyncAdapt_rsqlx_cursor:
        cursor = self.cursor()
        cursor.execute(operation, parameters)
        return cursor

    def commit(self) -> None:
        try:
            if self._tx is not None:
                self.await_(self._tx.commit())
                self._tx = None
        except Exception as error:
            self._handle_exception(error)

    def rollback(self) -> None:
        try:
            if self._tx is not None:
                self.await_(self._tx.rollback())
                self._tx = None
        except Exception as error:
            self._handle_exception(error)

    def close(self) -> None:
        try:
            if self._tx is not None:
                self.await_(self._tx.rollback())
                self._tx = None
            self.await_(self._pool.close())
        except Exception as error:
            self._handle_exception(error)
        finally:
            self._closed = True

    def _handle_exception(self, error: BaseException) -> None:
        translated = translate_exception(error)
        if translated is not None:
            raise translated

    def ping(self) -> bool:
        cursor = self.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchall()
        return True


class AsyncAdaptFallback_rsqlx_connection(AsyncAdapt_rsqlx_connection):
    await_ = staticmethod(await_fallback)


# ---------------------------------------------------------------------------
# module
# ---------------------------------------------------------------------------


class AsyncAdapt_rsqlx_dbapi:
    apilevel = "2.0"
    threadsafety = 2

    def __init__(self, paramstyle: str):
        self.paramstyle = paramstyle
        for cls in (
            Error,
            InterfaceError,
            DatabaseError,
            DataError,
            OperationalError,
            IntegrityError,
            InternalError,
            ProgrammingError,
            NotSupportedError,
        ):
            setattr(self, cls.__name__, cls)
        self.Binary = bytes
        self.Date = _dt.date
        self.Time = _dt.time
        self.Timestamp = _dt.datetime
        self.Decimal = _decimal.Decimal
        # Used only by SQLAlchemy's SQLite feature checks.  rsqlx statically
        # links its own SQLite; the stdlib version is a close approximation.
        self.sqlite_version = _sqlite3.sqlite_version
        self.sqlite_version_info = _sqlite3.sqlite_version_info

    def connect(self, *args: Any, **kwargs: Any) -> AsyncAdapt_rsqlx_connection:
        async_fallback = kwargs.pop("async_fallback", False)
        connection = rsqlx.connect(*args, **kwargs)
        if async_fallback:
            return AsyncAdaptFallback_rsqlx_connection(
                self, await_fallback(connection)
            )
        return AsyncAdapt_rsqlx_connection(self, await_only(connection))


def make_dbapi_module(paramstyle: str) -> AsyncAdapt_rsqlx_dbapi:
    return AsyncAdapt_rsqlx_dbapi(paramstyle)
