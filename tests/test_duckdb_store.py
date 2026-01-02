from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional, List, ClassVar
import os
import math
import time
import logging
import threading
import tempfile

import pyarrow as pa
import pytest

from tmautils.db import DuckDbStore, DuckDbBackend, pydantic_to_arrow, WriteMode

try:
    import duckdb
except Exception:  # pragma: no cover
    duckdb = None


def _duckdb_available() -> bool:
    return duckdb is not None


pytestmark = pytest.mark.skipif(
    not _duckdb_available(),
    reason="duckdb not available; skipping DuckDbStore tests",
)


# --------------------------- Test models ---------------------------

class ItemModel(BaseModel):
    k: int
    v: Optional[str] = None
    ts: Optional[float] = Field(
        default=None,
        description="epoch seconds (float); converted to TIMESTAMP(us)"
    )

    # Explicit Arrow schema (column order must match model field order)
    ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("k", pa.int64()),
        pa.field("v", pa.string()),
        pa.field("ts", pa.timestamp("us")),
    ])


# For negative tests: missing schema
class NoSchemaModel(BaseModel):
    a: int
    b: Optional[str] = None


# --------------------------- Fixtures ------------------------------

@pytest.fixture
def tmp_db_path(tmp_path):
    """Create a temporary DuckDB database file path"""
    return str(tmp_path / "test.duckdb")


@pytest.fixture
def store(tmp_db_path):
    """Create a DuckDbStore with items table"""
    s = DuckDbStore(db_path=tmp_db_path)

    # Create items table with PRIMARY KEY constraint for ON CONFLICT
    schema_sql = """
        CREATE TABLE IF NOT EXISTS items (
            k BIGINT PRIMARY KEY,
            v VARCHAR,
            ts TIMESTAMP
        );
    """
    s.execute(schema_sql)

    yield s
    s.close()


# --------------------------- Helpers -------------------------------

def _count_items(store: DuckDbStore) -> int:
    return store.query_one("SELECT COUNT(*) FROM items;") or 0


def _fetch_all(store: DuckDbStore):
    return store.query_records("SELECT k, v, epoch(ts) AS ts_s FROM items ORDER BY k;")


# --------------------------- Tests -------------------------------

def test_store_creation_and_close(tmp_db_path):
    """Test basic store creation and closing"""
    s = DuckDbStore(db_path=tmp_db_path)
    s.execute("CREATE TABLE test (id INTEGER);")
    cnt = s.query_one("SELECT COUNT(*) FROM test;")
    assert cnt == 0
    s.close()
    # Double close should be safe
    s.close()


def test_execute_and_query_records(store: DuckDbStore):
    """Test execute and query_records methods"""
    store.execute("INSERT INTO items (k, v, ts) VALUES (?, ?, ?);", [1, "test", None])
    store.execute("INSERT INTO items (k, v, ts) VALUES (?, ?, ?);", [2, "test2", None])

    rows = store.query_records("SELECT k, v FROM items ORDER BY k;")
    assert len(rows) == 2
    assert rows[0]["k"] == 1
    assert rows[0]["v"] == "test"
    assert rows[1]["k"] == 2


def test_execute_txn_commits_on_success(store: DuckDbStore):
    """Test execute_txn commits all statements on success"""
    statements = [
        ("INSERT INTO items (k, v, ts) VALUES (?, ?, ?);", [10, "a", None]),
        ("INSERT INTO items (k, v, ts) VALUES (?, ?, ?);", [11, "b", None]),
    ]
    store.execute_txn(statements)

    assert _count_items(store) == 2
    rows = _fetch_all(store)
    assert rows[0]["k"] == 10
    assert rows[1]["k"] == 11


def test_execute_txn_rolls_back_on_error(store: DuckDbStore):
    """Test execute_txn rolls back on error"""
    before = _count_items(store)

    statements = [
        ("INSERT INTO items (k, v, ts) VALUES (?, ?, ?);", [100, "ok", None]),
        ("SELECT * FROM __this_table_does_not_exist__;", None),
    ]

    with pytest.raises(Exception):
        store.execute_txn(statements)

    after = _count_items(store)
    assert after == before  # nothing committed


def test_query_one(store: DuckDbStore):
    """Test query_one returns single value"""
    store.execute("INSERT INTO items (k, v, ts) VALUES (1, 'test', NULL);")

    result = store.query_one("SELECT v FROM items WHERE k = 1;")
    assert result == "test"

    # Non-existent row
    result = store.query_one("SELECT v FROM items WHERE k = 999;")
    assert result is None


def test_query_arrow(store: DuckDbStore):
    """Test query_arrow returns PyArrow table"""
    store.execute("INSERT INTO items (k, v, ts) VALUES (1, 'a', NULL);")
    store.execute("INSERT INTO items (k, v, ts) VALUES (2, 'b', NULL);")

    reader_or_table = store.query_arrow("SELECT k, v FROM items ORDER BY k;")

    # Convert RecordBatchReader to Table if needed
    if hasattr(pa, "RecordBatchReader") and isinstance(reader_or_table, pa.RecordBatchReader):
        t = reader_or_table.read_all()
    else:
        t = reader_or_table

    assert isinstance(t, pa.Table)
    assert set(t.column_names) == {"k", "v"}
    assert t.num_rows == 2


def test_query_df(store: DuckDbStore):
    """Test query_df returns pandas DataFrame"""
    store.execute("INSERT INTO items (k, v, ts) VALUES (1, 'a', NULL);")
    store.execute("INSERT INTO items (k, v, ts) VALUES (2, 'b', NULL);")

    df = store.query_df("SELECT k, v FROM items ORDER BY k;")

    assert len(df) == 2
    assert list(df["k"]) == [1, 2]
    assert list(df["v"]) == ["a", "b"]


def test_pydantic_to_arrow_and_append(store: DuckDbStore):
    """Test insert_arrow with APPEND mode and pydantic models"""
    t0 = time.time()
    items: List[ItemModel] = [
        ItemModel(k=1, v="a", ts=t0),
        ItemModel(k=2, v=None, ts=t0 + 1.5),
        ItemModel(k=3, v="c", ts=None),
    ]
    tbl = pydantic_to_arrow(items, ItemModel)
    assert isinstance(tbl, pa.Table)
    assert tbl.schema.field("ts").type == pa.timestamp("us")

    # Append
    store.insert_arrow("items", tbl, mode=WriteMode.APPEND)
    assert _count_items(store) == 3

    rows = _fetch_all(store)
    # ts for k=1 and k=2 should be close to what we inserted (seconds)
    k1 = next(r for r in rows if r["k"] == 1)
    k2 = next(r for r in rows if r["k"] == 2)
    assert math.isclose(k1["ts_s"], t0, rel_tol=0, abs_tol=0.002)
    assert math.isclose(k2["ts_s"], t0 + 1.5, rel_tol=0, abs_tol=0.002)
    # k=3 had None -> should be NULL in DB (epoch(NULL) returns NULL)
    k3 = next(r for r in rows if r["k"] == 3)
    assert k3["ts_s"] is None


def test_insert_ignore(store: DuckDbStore):
    """Test INSERT_IGNORE mode ignores duplicates"""
    # Insert initial data
    initial_items = [
        ItemModel(k=1, v="a", ts=None),
        ItemModel(k=2, v="b", ts=None),
    ]
    tbl = pydantic_to_arrow(initial_items, ItemModel)
    store.insert_arrow("items", tbl, mode=WriteMode.APPEND)

    # Try inserting duplicates for k=1,2; insert_ignore should keep existing
    dup_items = [
        ItemModel(k=1, v="dup1", ts=None),
        ItemModel(k=2, v="dup2", ts=None),
        ItemModel(k=3, v="new", ts=None),  # new key
    ]
    tbl = pydantic_to_arrow(dup_items, ItemModel)
    store.insert_arrow("items", tbl, mode=WriteMode.INSERT_IGNORE, key_cols=["k"])

    # count should be 3 (1, 2 ignored, 3 inserted)
    assert _count_items(store) == 3

    rows = _fetch_all(store)
    # Values for k=1,2 should remain from original append
    assert next(r for r in rows if r["k"] == 1)["v"] == "a"
    assert next(r for r in rows if r["k"] == 2)["v"] == "b"
    # k=3 should be new
    assert next(r for r in rows if r["k"] == 3)["v"] == "new"


def test_upsert_update_and_insert(store: DuckDbStore):
    """Test UPSERT mode updates existing and inserts new rows"""
    # Insert initial data
    initial_items = [
        ItemModel(k=1, v="a", ts=None),
        ItemModel(k=2, v="b", ts=None),
    ]
    tbl = pydantic_to_arrow(initial_items, ItemModel)
    store.insert_arrow("items", tbl, mode=WriteMode.APPEND)

    # Upsert should update existing keys and insert new ones
    up_items = [
        ItemModel(k=2, v="updated", ts=None),  # update existing
        ItemModel(k=4, v="new", ts=None),      # new row
    ]
    tbl = pydantic_to_arrow(up_items, ItemModel)
    store.insert_arrow("items", tbl, mode=WriteMode.UPSERT, key_cols=["k"])

    assert _count_items(store) == 3
    rows = _fetch_all(store)
    assert next(r for r in rows if r["k"] == 1)["v"] == "a"  # unchanged
    assert next(r for r in rows if r["k"] == 2)["v"] == "updated"  # updated
    assert next(r for r in rows if r["k"] == 4)["v"] == "new"  # inserted


def test_upsert_only_keys_behaves_like_insert_ignore(store: DuckDbStore):
    """Test UPSERT with only key columns behaves like INSERT_IGNORE"""
    # Provide only the key column; UPSERT path should fall back to insert_ignore semantics
    # duplicate key -> only one should survive
    t = pa.table({"k": pa.array([5, 5], type=pa.int64())})
    store.insert_arrow("items", t, mode=WriteMode.UPSERT, key_cols=["k"])

    # Now ensure exactly one row with k=5 exists
    c = store.query_one("SELECT COUNT(*) FROM items WHERE k=5;")
    assert c == 1


def test_insert_arrow_empty_table(store: DuckDbStore):
    """Test insert_arrow with empty table is a no-op"""
    before = _count_items(store)

    # Empty table
    t = pa.table({"k": pa.array([], type=pa.int64())})
    store.insert_arrow("items", t, mode=WriteMode.APPEND)

    after = _count_items(store)
    assert after == before


def test_insert_arrow_none_table(store: DuckDbStore):
    """Test insert_arrow with None table is a no-op"""
    before = _count_items(store)

    store.insert_arrow("items", None, mode=WriteMode.APPEND)

    after = _count_items(store)
    assert after == before


def test_concurrent_insert_arrow(store: DuckDbStore):
    """Test concurrent Arrow inserts are thread-safe"""
    tA = pa.table({
        "k": pa.array([300, 301], type=pa.int64()),
        "v": pa.array(["A", "B"], type=pa.string()),
        "ts": pa.array([None, None], type=pa.timestamp("us")),
    })
    tB = pa.table({
        "k": pa.array([400, 401], type=pa.int64()),
        "v": pa.array(["C", "D"], type=pa.string()),
        "ts": pa.array([None, None], type=pa.timestamp("us")),
    })

    errors = []

    def worker(table):
        try:
            store.insert_arrow("items", table, mode=WriteMode.APPEND)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker, args=(tA,), daemon=True)
    t2 = threading.Thread(target=worker, args=(tB,), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Concurrent insert errors: {errors!r}"

    ks = [r["k"] for r in store.query_records(
        "SELECT k FROM items WHERE k IN (300,301,400,401) ORDER BY k;"
    )]
    assert ks == [300, 301, 400, 401]


def test_duckdb_backend_flush_arrow(tmp_db_path):
    """Test DuckDbBackend flush_arrow method"""
    from tmautils.db import TableConfig

    store = DuckDbStore(db_path=tmp_db_path)
    store.execute("""
        CREATE TABLE items (
            k BIGINT PRIMARY KEY,
            v VARCHAR,
            ts TIMESTAMP
        );
    """)

    backend = DuckDbBackend(store=store)
    config = TableConfig(model=ItemModel, mode=WriteMode.APPEND, key_cols=None)

    # Create test data
    data = pa.table({
        "k": pa.array([1, 2, 3], type=pa.int64()),
        "v": pa.array(["a", "b", "c"], type=pa.string()),
        "ts": pa.array([None, None, None], type=pa.timestamp("us")),
    })

    # Flush via backend
    backend.flush_arrow("items", config, data)

    # Verify data was written
    cnt = store.query_one("SELECT COUNT(*) FROM items;")
    assert cnt == 3

    store.close()


def test_context_manager(tmp_db_path):
    """Test DuckDbStore works as context manager"""
    with DuckDbStore(db_path=tmp_db_path) as store:
        store.execute("CREATE TABLE test (id INTEGER);")
        store.execute("INSERT INTO test VALUES (1);")
        cnt = store.query_one("SELECT COUNT(*) FROM test;")
        assert cnt == 1

    # Connection should be closed after context exit


def test_custom_connect_kwargs(tmp_path):
    """Test DuckDbStore with custom connection kwargs"""
    db_path = str(tmp_path / "custom.duckdb")

    store = DuckDbStore(
        db_path=db_path,
        connect_kwargs={
            "config": {
                "threads": 1,
                "memory_limit": "500MB",
            }
        }
    )

    # Verify connection works
    store.execute("CREATE TABLE test (id INTEGER);")
    cnt = store.query_one("SELECT COUNT(*) FROM test;")
    assert cnt == 0

    store.close()


def test_missing_arrow_schema_raises():
    """Test pydantic_to_arrow raises on missing ARROW_SCHEMA"""
    items = [NoSchemaModel(a=1, b="x")]
    with pytest.raises(ValueError):
        _ = pydantic_to_arrow(items, NoSchemaModel)


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
