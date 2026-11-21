# Copyright (C) rsqlx Contributors
# SPDX-License-Identifier: MIT OR Apache-2.0
"""``sqlite+rsqlx://`` async dialect."""

from __future__ import annotations

from sqlalchemy import pool
from sqlalchemy.dialects.sqlite.base import SQLiteDialect

from .base import RsqlxAsyncDialectMixin


class SQLiteDialect_rsqlx(RsqlxAsyncDialectMixin, SQLiteDialect):
    driver = "rsqlx"
    default_paramstyle = "qmark"
    paramstyle = "qmark"
    _rsqlx_paramstyle = "qmark"
    supports_statement_cache = True

    @classmethod
    def get_pool_class(cls, url):
        if not url.database or url.database == ":memory:":
            return pool.StaticPool
        return pool.AsyncAdaptedQueuePool

    def set_isolation_level(self, dbapi_connection, level):
        dbapi_connection.autocommit = level == "AUTOCOMMIT"


dialect = SQLiteDialect_rsqlx
