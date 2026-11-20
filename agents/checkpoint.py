import atexit

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from api.config import settings

conninfo = make_conninfo(
    host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
    user=settings.db_user, password=settings.db_password,
)

pool = ConnectionPool(
    conninfo=conninfo,
    max_size=5,
    kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    open=True,
)
atexit.register(pool.close)      # closes the connections when the program ends

checkpointer = PostgresSaver(pool)