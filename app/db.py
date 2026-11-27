"""Подключение к PostgreSQL и инициализация схемы."""
import pathlib
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pgvector.psycopg import register_vector

from . import config

_pool = None


def _configure(conn):
    register_vector(conn)


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # check: соединение проверяется перед выдачей — после перезапуска PostgreSQL или обрыва сервером пул не отдаёт
        # «мёртвое» соединение (слепой прогон 27.11, H01: AdminShutdown на первом же вопросе).
        _pool = ConnectionPool(config.DB_DSN, min_size=1, max_size=8, configure=_configure, check=ConnectionPool.check_connection,
                               kwargs={"row_factory": dict_row, "autocommit": True})
    return _pool


@contextmanager
def conn():
    with pool().connection() as c:
        yield c


def init_schema():
    sql = (pathlib.Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(config.DB_DSN, autocommit=True) as c:
        c.execute(sql)
