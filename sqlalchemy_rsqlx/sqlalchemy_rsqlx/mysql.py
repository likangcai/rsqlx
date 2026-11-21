# Copyright (C) rsqlx Contributors
# SPDX-License-Identifier: MIT OR Apache-2.0
"""``mysql+rsqlx://`` async dialect."""

from __future__ import annotations

from typing import Any, Optional

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

    def _detect_charset(self, connection: Any) -> str:
        # rsqlx's MySQL connection uses utf8mb4 by default; avoid an extra
        # round trip during dialect initialization.
        return "utf8mb4"

    def _extract_error_code(self, exception: Any) -> Optional[int]:
        code = getattr(exception, "code", None)
        if code is None:
            return None
        try:
            return int(code)
        except (TypeError, ValueError):
            # rsqlx surfaces SQLSTATE strings (e.g. "42S02") for some MySQL
            # errors; SQLAlchemy only needs an int here, so skip such codes.
            return None


dialect = MySQLDialect_rsqlx
