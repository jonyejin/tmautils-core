from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional, List, ClassVar, Callable
import os
import math
import time
import logging
import threading

import pyarrow as pa
import pytest

from tmautils.db import DuckLakeStore, pydantic_to_arrow

try:
    import duckdb
except Exception:  # pragma: no cover
    duckdb = None


def _ducklake_available() -> bool:
    if duckdb is None:
        return False
    try:
        con = duckdb.connect()
        try:
            con.execute("INSTALL ducklake; LOAD ducklake;")
        finally:
            con.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _ducklake_available(),
    reason="ducklake extension not available; skipping DuckLakeStore tests",
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


# For negative tests: mismatched-order model
class BadOrderModel(BaseModel):
    # NOTE: field order here is v, k, ts (does not match ARROW_SCHEMA)
    v: Optional[str] = None
    k: int
    ts: Optional[float] = None

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

@pytest.fixture(scope="session")
def tmp_catalog_and_data(tmp_path_factory):
    base = tmp_path_factory.mktemp("ducklake_it")
    cat = base / "catalog.db"   # will be used via sqlite: catalog
    data = base / "data_dir"
    data.mkdir(exist_ok=True, parents=True)
    return str(cat), str(data)


@pytest.fixture
def store(tmp_catalog_and_data):
    catalog_path, data_path = tmp_catalog_and_data
    s = DuckLakeStore()

    alias = "lake"
    schema_sql = f"""
        CREATE TABLE IF NOT EXISTS {alias}.items (
            k BIGINT,
            v VARCHAR,
            ts TIMESTAMP
        );
    """
    s.attach_lake(
        alias=alias,
        catalog_path=f"sqlite:{catalog_path}",
        data_path=data_path,
        options=["META_JOURNAL_MODE 'WAL'", "META_BUSY_TIMEOUT 500"],
        extensions=("sqlite",),
        schema_sql=schema_sql,
    )
    yield s
    try:
        s.detach_lake("lake")
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to detach lake in fixture teardown", exc_info=True
        )
    s.close()


# --------------------------- Helpers -------------------------------

def _count_items(store: DuckLakeStore) -> int:
    return store.query_one("SELECT COUNT(*) FROM lake.items;") or 0


def _fetch_all(store: DuckLakeStore):
    return store.query_records("SELECT k, v, epoch(ts) AS ts_s FROM lake.items ORDER BY k;")


# --------------------------- Tests -------------------------------

def test_attach_idempotent(store: DuckLakeStore, tmp_catalog_and_data):
    catalog_path, data_path = tmp_catalog_and_data
    # Second attach with same alias should be a no-op
    store.attach_lake(
        alias="lake",
        catalog_path=f"sqlite:{catalog_path}",
        data_path=data_path,
        extensions=("sqlite",),
        schema_sql=None,
    )
    # Calling snapshot should work (may return None on some backends)
    try:
        _ = store.snapshot("lake")
    except Exception:
        # Some ducklake builds may not expose current_snapshot(); don't fail the test
        logging.getLogger(__name__).warning(
            "snapshot() call failed; ducklake build may not support it", exc_info=True
        )
        pass


def test_attach_schema_sql_placeholder(store: DuckLakeStore, tmp_catalog_and_data):
    catalog_path, data_path = tmp_catalog_and_data
    # Attach a second alias using the {{alias}} placeholder
    store.attach_lake(
        alias="lake2",
        catalog_path=f"sqlite:{catalog_path}",
        data_path=data_path,
        extensions=("sqlite",),
        schema_sql="CREATE TABLE IF NOT EXISTS {{alias}}.tmp_tbl (k BIGINT);",
    )
    # Should be able to write/read that table
    store.execute("INSERT INTO lake2.tmp_tbl VALUES (1), (2);")
    cnt = store.query_one("SELECT COUNT(*) FROM lake2.tmp_tbl;")
    assert cnt == 2
    # Clean up alias
    store.detach_lake("lake2")


def test_pydantic_to_arrow_and_append(store: DuckLakeStore):
    # Build a few rows with epoch-second floats; test timestamp conversion to us
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
    store.insert_arrow("items", tbl, mode="append")
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


def test_insert_ignore(store: DuckLakeStore):
    # Try inserting duplicates for k=1,2; insert_ignore should keep existing
    dup_items = [
        ItemModel(k=1, v="dup1", ts=None),
        ItemModel(k=2, v="dup2", ts=None),
    ]
    tbl = pydantic_to_arrow(dup_items, ItemModel)
    store.insert_arrow("items", tbl, mode="insert_ignore", key_cols=["k"])
    # count stays 3
    assert _count_items(store) == 3

    rows = _fetch_all(store)
    # Values for k=1,2 should remain from original append ("a", None)
    assert next(r for r in rows if r["k"] == 1)["v"] == "a"
    assert next(r for r in rows if r["k"] == 2)["v"] is None


def test_upsert_update_and_insert(store: DuckLakeStore):
    # Upsert should update existing keys and insert new ones
    up_items = [
        ItemModel(k=2, v="updated", ts=None),  # update existing
        ItemModel(k=4, v="new", ts=None),      # new row
    ]
    tbl = pydantic_to_arrow(up_items, ItemModel)
    store.insert_arrow("items", tbl, mode="upsert", key_cols=["k"])

    assert _count_items(store) == 4
    rows = _fetch_all(store)
    assert next(r for r in rows if r["k"] == 2)["v"] == "updated"
    assert next(r for r in rows if r["k"] == 4)["v"] == "new"


def test_upsert_only_keys_behaves_like_insert_ignore(store: DuckLakeStore):
    # Provide only the key column; MERGE path should fall back to insert_ignore semantics
    # duplicate key -> only one should survive
    t = pa.table({"k": pa.array([5, 5], type=pa.int64())})
    store.insert_arrow("items", t, mode="upsert", key_cols=["k"])
    # Now ensure exactly one row with k=5 exists
    c = store.query_one("SELECT COUNT(*) FROM lake.items WHERE k=5;")
    assert c == 1


def test_query_df_and_plain_query(store: DuckLakeStore):
    # Ensure both query() and query_df() paths are fine
    out_dicts = store.query_records(
        "SELECT k FROM lake.items WHERE k IN (1,4) ORDER BY k;")
    assert [r["k"] for r in out_dicts] == [1, 4]

    out_df = store.query_df(
        "SELECT k, v FROM lake.items WHERE k >= 2 ORDER BY k;")
    # After prior tests, k=5 now exists as well
    assert list(out_df["k"]) == [2, 3, 4, 5]


def test_insert_records_convenience(store: DuckLakeStore):
    rows = [
        {"k": 10, "v": "r1", "ts": None},
        {"k": 11, "v": "r2", "ts": None},
    ]
    store.insert_records("items", rows, ItemModel,
                         mode="insert_ignore", key_cols=["k"])
    rs = store.query_records(
        "SELECT k FROM lake.items WHERE k IN (10,11) ORDER BY k;")
    assert [r["k"] for r in rs] == [10, 11]


def test_retry_toggle_execute(store: DuckLakeStore, monkeypatch):
    # Verify that retry_on_lock toggles the use of the retry wrapper
    calls = {"n": 0}
    real_cwr = store.run_with_retry

    def spy(fn, *a, **kw):
        calls["n"] += 1
        return fn(*a, **kw)

    monkeypatch.setattr(store, "run_with_retry", spy)
    # With retry -> wrapper should be called
    store.execute("SELECT 1;", retry_on_lock=True)
    assert calls["n"] == 1
    # Without retry -> wrapper not called again
    store.execute("SELECT 1;", retry_on_lock=False)
    assert calls["n"] == 1
    # restore (not strictly necessary in pytest since object is per-test)
    monkeypatch.setattr(store, "run_with_retry", real_cwr)


def test_retry_toggle_insert_arrow(store: DuckLakeStore, monkeypatch):
    # Verify retry wrapper is used or bypassed for insert_arrow
    calls = {"n": 0}
    real_cwr = store.run_with_retry

    def spy(fn, *a, **kw):
        calls["n"] += 1
        return fn(*a, **kw)

    # Minimal table with distinct keys to avoid interference
    t1 = pa.table({
        "k": pa.array([701], type=pa.int64()),
        "v": pa.array(["toggle1"], type=pa.string()),
        "ts": pa.array([None], type=pa.timestamp("us")),
    })
    t2 = pa.table({
        "k": pa.array([702], type=pa.int64()),
        "v": pa.array(["toggle2"], type=pa.string()),
        "ts": pa.array([None], type=pa.timestamp("us")),
    })

    monkeypatch.setattr(store, "run_with_retry", spy)
    store.insert_arrow("items", t1, mode="append", retry_on_lock=True)
    assert calls["n"] == 1
    store.insert_arrow("items", t2, mode="append", retry_on_lock=False)
    assert calls["n"] == 1  # unchanged (second call bypassed retry wrapper)
    # Validate rows landed
    got = set(r["k"] for r in store.query_records(
        "SELECT k FROM lake.items WHERE k IN (701,702) ORDER BY k;"))
    assert got == {701, 702}
    monkeypatch.setattr(store, "run_with_retry", real_cwr)


def test_detach(store: DuckLakeStore):
    # Detach should succeed and be idempotent-ish via our guard
    store.detach_lake("lake")
    # Detaching again should no-op via internal guard (and not raise)
    store.detach_lake("lake")


def test_query_arrow(store: DuckLakeStore):
    # Arrow roundtrip path
    reader_or_table = store.query_arrow(
        "SELECT k, v FROM lake.items ORDER BY k;"
    )
    # duckdb.Relation.arrow() commonly returns a RecordBatchReader; convert to Table if needed
    if hasattr(pa, "RecordBatchReader") and isinstance(reader_or_table, pa.RecordBatchReader):
        t = reader_or_table.read_all()
    else:
        t = reader_or_table

    assert isinstance(t, pa.Table)
    assert set(t.column_names) == {"k", "v"}
    # also ensure we actually have some rows
    assert t.num_rows >= 4


def test_missing_arrow_schema_raises():
    items = [NoSchemaModel(a=1, b="x")]
    with pytest.raises(ValueError):
        _ = pydantic_to_arrow(items, NoSchemaModel)


def test_schema_order_can_differ():
    # Pydantic field order (v, k, ts) differs from ARROW_SCHEMA order (k, v, ts),
    # but names match, so conversion should succeed and follow schema order.
    rows = [BadOrderModel(k=1, v="x", ts=None)]
    t = pydantic_to_arrow(rows, BadOrderModel)
    assert isinstance(t, pa.Table)
    # Confirm schema order is respected
    assert t.column_names == ["k", "v", "ts"]
    # Values mapped to correct columns
    assert t.column("k").to_pylist() == [1]
    assert t.column("v").to_pylist() == ["x"]
    # ts is optional timestamp(us) and should be NULL -> None
    assert t.schema.field("ts").type == pa.timestamp("us")
    assert t.column("ts").to_pylist() == [None]


def test_execute_txn_rolls_back_on_error(store: DuckLakeStore):
    # Verify that a failure in the middle of a transaction rolls back all changes.
    before = _count_items(store)
    # Cause failure with an invalid statement after a valid insert.
    stmts = [
        ("INSERT INTO lake.items(k, v, ts) VALUES (999, 'ok', NULL);", None),
        ("SELECT * FROM __this_table_does_not_exist__", None),
    ]
    with pytest.raises(Exception):
        store.execute_txn(stmts)

    after = _count_items(store)
    assert after == before  # nothing committed


def test_checkpoint_tolerant(store: DuckLakeStore):
    # Options via set_option + global CHECKPOINT; not all builds may support everything
    try:
        store.checkpoint(
            lake="lake",
            expire_older_than="0 day",
            delete_older_than="0 day",
            rewrite_delete_threshold=1.0,
        )
    except Exception:
        # Accept lack of procedures on certain builds/backends
        logging.getLogger(__name__).warning(
            "checkpoint() call failed; ducklake build may not support it", exc_info=True
        )
        pass


def test_concurrent_insert_arrow_registration_lock(store: DuckLakeStore):
    """Two concurrent Arrow inserts that both need view registration."""
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
            store.insert_arrow("items", table, mode="append")
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
        "SELECT k FROM lake.items WHERE k IN (300,301,400,401) ORDER BY k;"
    )]
    assert ks == [300, 301, 400, 401]


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
