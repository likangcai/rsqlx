# Copyright (C) rsqlx Contributors
# SPDX-License-Identifier: MIT OR Apache-2.0
"""``mysql+rsqlx://`` async dialect."""

from __future__ import annotations

from sqlalchemy import pool
from sqlalchemy.dialects.mysql.base import MySQLDialect

from .base import RsqlxAsyncDialectMixin


class MySQLDialect_rsqlx(RsqlxAsyncDialectMixin, MySQLDialect):
    driver = "rsqlx"
    default_paramstyle = "qmark"
    paramstyle = "qmark"
    _rsqlx_paramstyle = "qmark"
    supports_statement_cache = True
    supports_native_decimal = True
    supports_native_boolean = True

    @classmethod
    def get_pool_class(cls, url):
        return pool.AsyncAdaptedQueuePool


dialect = MySQLDialect_rsqlx
