import os
import sqlite3
import pandas as pd
import numpy as np
import pytest
from ipaddress import IPv4Address, IPv6Address

from tmautils.common import SqliteDatabase


@pytest.fixture
def db_maker(tmp_path):
    created = []

    def make_db(filename: str, offload: bool) -> SqliteDatabase:
        db_file = tmp_path / filename
        db = SqliteDatabase(str(db_file), uri=False, offload_to_worker=offload)
        created.append(db)
        return db

    yield make_db

    # Close all DBs to shut down worker processes
    for db in created:
        try:
            db.close()
        except Exception:
            pass


@pytest.mark.parametrize("offload", [False, True])
def test_single_table_crud(db_maker, offload):
    manager = db_maker("test.db", offload)

    schema = {"id": int, "ip": IPv4Address, "name": str}
    qualifiers = {"id": "PRIMARY KEY AUTOINCREMENT"}
    constraints = ["UNIQUE(name)"]
    indices = [["ip"]]

    users = manager.register_table(
        "users", schema,
        qualifiers=qualifiers,
        table_constraints=constraints,
        indices=indices
    )

    df_insert = pd.DataFrame({
        "ip": ["192.168.0.1", "10.0.0.2"],
        "name": ["Alice", "Bob"]
    })
    users.insert_df(df_insert, block=True)

    df_all = users.query_all()
    assert set(df_all.columns) == {"id", "ip", "name"}
    assert list(df_all["id"]) == [1, 2]
    assert df_all.loc[0, "ip"] == IPv4Address("192.168.0.1")
    assert set(df_all["name"]) == {"Alice", "Bob"}

    df_dup = pd.DataFrame({"ip": ["127.0.0.1"], "name": ["Alice"]})
    with pytest.raises(sqlite3.IntegrityError):
        users.insert_df(df_dup, block=True)


@pytest.mark.parametrize("offload", [False, True])
def test_query_and_query_all(db_maker, offload):
    manager = db_maker("flows.db", offload)

    schema = {"src": IPv4Address, "dst": IPv4Address, "val": int}
    indices = [["val"]]
    flows = manager.register_table("flows", schema, indices=indices)

    df_flow = pd.DataFrame({
        "src": [f"1.1.1.{i}" for i in range(100)],
        "dst": [f"2.2.2.{i}" for i in range(100)],
        "val": list(range(100))
    })
    flows.insert_df(df_flow, block=True)

    sql = "SELECT * FROM flows WHERE val >= ? AND val < ?"
    df_filtered = flows.query(sql, params=(10, 20))
    assert df_filtered.shape[0] == 10
    assert set(df_filtered["val"]) == set(range(10, 20))

    df_all = flows.query_all()
    assert df_all.shape[0] == 100


@pytest.mark.parametrize("offload", [False, True])
def test_boolean_and_nullable(db_maker, offload):
    mgr = db_maker("bool.db", offload)

    schema = {"f": bool, "g": bool}
    qualifiers = {"g": "NOT NULL"}
    tbl = mgr.register_table("t_bool", schema, qualifiers=qualifiers)

    df = pd.DataFrame({
        "f": [True, False, pd.NA],
        "g": [True, False, True],
    }).astype({"f": "boolean", "g": "boolean"})
    tbl.insert_df(df, block=True)
    out = tbl.query_all()

    assert out["f"].dtype == "boolean"
    assert out["g"].dtype == "boolean"
    assert list(out["f"]) == [True, False, pd.NA]
    assert list(out["g"]) == [True, False, True]

    with pytest.raises(sqlite3.IntegrityError):
        tbl.insert_df(
            pd.DataFrame({"f": [True], "g": [pd.NA]}).astype("boolean"),
            block=True,
        )


@pytest.mark.parametrize("offload", [False, True])
def test_datetime_and_nat(db_maker, offload):
    mgr = db_maker("dt.db", offload)

    schema = {"ts1": np.datetime64, "ts2": pd.Timestamp}
    tbl = mgr.register_table("t_dt", schema)

    now = pd.Timestamp("2025-01-01T12:00")
    df = pd.DataFrame({
        "ts1": [now.to_numpy(), np.datetime64("NaT")],
        "ts2": [now, pd.NaT],
    })
    tbl.insert_df(df, block=True)
    out = tbl.query_all()

    assert out["ts1"].dtype == "datetime64[ns]"
    assert out["ts2"].dtype == "datetime64[ns]"
    assert pd.isna(out.loc[1, "ts1"])
    assert pd.isna(out.loc[1, "ts2"])


@pytest.mark.parametrize("offload", [False, True])
def test_ipaddr_and_null(db_maker, offload):
    mgr = db_maker("ip.db", offload)

    schema = {"a": IPv4Address, "b": IPv6Address}
    tbl = mgr.register_table("t_ip", schema)

    df = pd.DataFrame({
        "a": ["1.2.3.4", None],
        "b": ["2001:db8::1", pd.NA],
    })
    tbl.insert_df(df, block=True)
    out = tbl.query_all()

    assert isinstance(out.loc[0, "a"], IPv4Address)
    assert out.loc[1, "a"] is None
    assert isinstance(out.loc[0, "b"], IPv6Address)
    assert out.loc[1, "b"] in (None, pd.NA)


@pytest.mark.parametrize("offload", [False, True])
def test_numpy_scalars(db_maker, offload):
    mgr = db_maker("num.db", offload)

    schema = {"i": int, "f": float}
    tbl = mgr.register_table("t_num", schema)

    df = pd.DataFrame({
        "i": np.array([1, 2, 3], dtype=np.int64),
        "f": np.array([0.1, 0.2, 0.3], dtype=np.float64),
    })
    tbl.insert_df(df, block=True)
    out = tbl.query_all()

    assert out["i"].dtype == "Int64"
    assert out["f"].dtype == "Float64"
    assert out["i"].tolist() == [1, 2, 3]
    assert np.allclose(out["f"].astype(float), [0.1, 0.2, 0.3])


@pytest.mark.parametrize("offload", [False, True])
def test_partial_query(db_maker, offload):
    mgr = db_maker("part.db", offload)

    schema = {"x": int, "y": str, "z": IPv4Address}
    tbl = mgr.register_table("t_p", schema)

    df = pd.DataFrame({
        "x": [10, 20],
        "y": ["foo", "bar"],
        "z": ["8.8.8.8", "1.1.1.1"]
    })
    tbl.insert_df(df, block=True)

    out = tbl.query("SELECT z, x FROM t_p WHERE x > ?", params=(10,))
    assert list(out.columns) == ["z", "x"]
    assert out["x"].tolist() == [20]
    assert isinstance(out.loc[0, "z"], IPv4Address)


@pytest.mark.parametrize("offload", [False, True])
def test_index_creation(db_maker, offload):
    mgr = db_maker("idx.db", offload)

    schema = {"a": int, "b": int}
    idxs = [["a", "b"], ["b"]]
    tbl = mgr.register_table("t_idx", schema, indices=idxs)

    # Query PRAGMA via table API (works in both modes)
    pragma_df = tbl.query("PRAGMA index_list(t_idx);")
    got = set(pragma_df["name"])
    expected = {f"idx_t_idx_{'_'.join(i)}" for i in idxs}
    assert expected.issubset(got)


@pytest.mark.parametrize("offload", [False, True])
def test_invalid_schema_and_qualifier(db_maker, offload):
    mgr = db_maker("bad.db", offload)

    with pytest.raises(TypeError):
        mgr.register_table("t_bad", {"foo": set})

    with pytest.raises(ValueError):
        mgr.register_table("t_bad2", {"a": int}, qualifiers={"a": "NOTVALID"})


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
