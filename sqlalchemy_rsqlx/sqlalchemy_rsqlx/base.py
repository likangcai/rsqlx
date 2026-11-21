# Copyright (C) rsqlx Contributors
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Shared dialect logic: DSN translation and DBAPI adapter construction."""

from __future__ import annotations

from urllib.parse import urlencode

from .dbapi import OperationalError, make_dbapi_module

_HANDLED_QUERY_KEYS = {
    "pool_size",
    "max_overflow",
    "pool_timeout",
    "pool_recycle",
    "pool_pre_ping",
    "pool_use_lifo",
    "async_fallback",
    "min_connections",
    "max_connections",
    "acquire_timeout",
    "idle_timeout",
    "max_lifetime",
}

_INT_OPTIONS = {"min_connections", "max_connections"}
_FLOAT_OPTIONS = {"acquire_timeout", "idle_timeout", "max_lifetime"}


def build_connect_args(url):
    """Translate a SQLAlchemy URL into ``rsqlx.connect`` positional/keyword args.

    Each SQLAlchemy DBAPI connection owns one rsqlx pool with exactly one
    physical connection; therefore ``max_connections`` defaults to 1 here.
    """
    query = dict(url.query)
    opts = {"max_connections": 1}

    if "pool_timeout" in query:
        opts["acquire_timeout"] = float(query["pool_timeout"])
    if "pool_recycle" in query:
        opts["max_lifetime"] = float(query["pool_recycle"])
    for key in _INT_OPTIONS | _FLOAT_OPTIONS:
        if key in query:
            opts[key] = int(query[key]) if key in _INT_OPTIONS else float(query[key])

    drivername = url.drivername.split("+", 1)[0]
    rest = {k: v for k, v in query.items() if k not in _HANDLED_QUERY_KEYS}

    if drivername == "sqlite":
        database = url.database
        dsn = "sqlite::memory:" if not database or database == ":memory:" else "sqlite:" + database
        if rest:
            dsn += "?" + urlencode(rest)
    else:
        rendered = url.set(drivername=drivername, query=rest).render_as_string(
            hide_password=False
        )
        if drivername == "postgresql":
            rendered = "postgres" + rendered[len("postgresql") :]
        dsn = rendered

    return [dsn], opts


class RsqlxAsyncDialectMixin:
    is_async = True
    supports_statement_cache = True
    _rsqlx_paramstyle = "qmark"

    @classmethod
    def import_dbapi(cls):
        return make_dbapi_module(cls._rsqlx_paramstyle)

    def create_connect_args(self, url):
        return build_connect_args(url)

    def get_driver_connection(self, connection):
        return connection._pool

    def do_ping(self, dbapi_connection):
        dbapi_connection.ping()
        return True

    def is_disconnect(self, error, connection, cursor):
        if isinstance(error, OperationalError):
            message = str(error).lower()
            if "pool closed" in message or "no active connection" in message:
                return True
        return super().is_disconnect(error, connection, cursor)
