# Copyright (C) rsqlx Contributors
# SPDX-License-Identifier: MIT OR Apache-2.0
"""``postgresql+rsqlx://`` async dialect.

SQLAlchemy's ``numeric_dollar`` paramstyle is the one that emits ``$1, $2``
(the DBAPI-style name ``numeric`` would emit ``:1``).  rsqlx receives those
placeholders verbatim — the adapter never rewrites SQL.
"""

from __future__ import annotations

from sqlalchemy import pool
from sqlalchemy.dialects.postgresql.base import PGDialect

from .base import RsqlxAsyncDialectMixin


class PGDialect_rsqlx(RsqlxAsyncDialectMixin, PGDialect):
    driver = "rsqlx"
    default_paramstyle = "numeric_dollar"
    paramstyle = "numeric_dollar"
    _rsqlx_paramstyle = "numeric_dollar"
    supports_statement_cache = True
    supports_native_uuid = True
    supports_native_decimal = True
    supports_native_boolean = True
    supports_native_json = True

    @classmethod
    def get_pool_class(cls, url):
        return pool.AsyncAdaptedQueuePool


dialect = PGDialect_rsqlx
