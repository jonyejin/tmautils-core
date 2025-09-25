import os
import sqlite3

import pandas as pd
import numpy as np
import pytest

from ipaddress import IPv4Address, IPv6Address

from tmautils.common import SqliteDatabase


def test_single_table_crud(tmp_path):
    db_file = tmp_path / "test.db"
    manager = SqliteDatabase(str(db_file), uri=False)

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
    users.insert_df(df_insert)

    df_all = users.query_all()
    assert set(df_all.columns) == {"id", "ip", "name"}
    assert list(df_all["id"]) == [1, 2]
    assert df_all.loc[0, "ip"] == IPv4Address("192.168.0.1")
    assert set(df_all["name"]) == {"Alice", "Bob"}

    df_dup = pd.DataFrame({"ip": ["127.0.0.1"], "name": ["Alice"]})
    with pytest.raises(sqlite3.IntegrityError):
        users.insert_df(df_dup)


def test_query_and_query_all(tmp_path):
    db_file = tmp_path / "flows.db"
    manager = SqliteDatabase(str(db_file), uri=False)

    schema = {"src": IPv4Address, "dst": IPv4Address, "val": int}
    indices = [["val"]]

    flows = manager.register_table("flows", schema, indices=indices)

    df_flow = pd.DataFrame({
        "src": [f"1.1.1.{i}" for i in range(100)],
        "dst": [f"2.2.2.{i}" for i in range(100)],
        "val": list(range(100))
    })
    flows.insert_df(df_flow)

    sql = "SELECT * FROM flows WHERE val >= ? AND val < ?"
    df_filtered = flows.query(sql, params=(10, 20))
    assert df_filtered.shape[0] == 10
    assert set(df_filtered["val"]) == set(range(10, 20))

    df_all = flows.query_all()
    assert df_all.shape[0] == 100


def test_boolean_and_nullable(tmp_path):
    db_file = tmp_path / "bool.db"
    mgr = SqliteDatabase(str(db_file), uri=False)

    schema = {"f": bool, "g": bool}
    qualifiers = {"g": "NOT NULL"}
    tbl = mgr.register_table("t_bool", schema, qualifiers=qualifiers)

    df = pd.DataFrame({
        "f": [True, False, pd.NA],
        "g": [True, False, True],
    }).astype({"f": "boolean", "g": "boolean"})
    tbl.insert_df(df)
    out = tbl.query_all()

    assert out["f"].dtype == "boolean"
    assert out["g"].dtype == "boolean"
    assert list(out["f"]) == [True, False, pd.NA]
    assert list(out["g"]) == [True, False, True]

    with pytest.raises(sqlite3.IntegrityError):
        tbl.insert_df(pd.DataFrame(
            {"f": [True], "g": [pd.NA]}).astype("boolean"))


def test_datetime_and_nat(tmp_path):
    db_file = tmp_path / "dt.db"
    mgr = SqliteDatabase(str(db_file), uri=False)

    schema = {"ts1": np.datetime64, "ts2": pd.Timestamp}
    tbl = mgr.register_table("t_dt", schema)

    now = pd.Timestamp("2025-01-01T12:00")
    df = pd.DataFrame({
        "ts1": [now.to_numpy(), np.datetime64("NaT")],
        "ts2": [now, pd.NaT],
    })
    tbl.insert_df(df)
    out = tbl.query_all()

    assert out["ts1"].dtype == "datetime64[ns]"
    assert out["ts2"].dtype == "datetime64[ns]"
    assert pd.isna(out.loc[1, "ts1"])
    assert pd.isna(out.loc[1, "ts2"])


def test_ipaddr_and_null(tmp_path):
    db_file = tmp_path / "ip.db"
    mgr = SqliteDatabase(str(db_file), uri=False)

    schema = {"a": IPv4Address, "b": IPv6Address}
    tbl = mgr.register_table("t_ip", schema)

    df = pd.DataFrame({
        "a": ["1.2.3.4", None],
        "b": ["2001:db8::1", pd.NA],
    })
    tbl.insert_df(df)
    out = tbl.query_all()

    assert isinstance(out.loc[0, "a"], IPv4Address)
    assert out.loc[1, "a"] is None

    assert isinstance(out.loc[0, "b"], IPv6Address)
    # pandas uses pd.NA => numpy NaT for missing datetime-like,
    # but for object dtype we expect None or pd.NA:
    assert out.loc[1, "b"] in (None, pd.NA)


def test_numpy_scalars(tmp_path):
    db_file = tmp_path / "num.db"
    mgr = SqliteDatabase(str(db_file), uri=False)

    schema = {"i": int, "f": float}
    tbl = mgr.register_table("t_num", schema)

    df = pd.DataFrame({
        "i": np.array([1, 2, 3], dtype=np.int64),
        "f": np.array([0.1, 0.2, 0.3], dtype=np.float64),
    })
    tbl.insert_df(df)
    out = tbl.query_all()

    assert out["i"].dtype == "Int64"
    assert out["f"].dtype == "Float64"
    assert out["i"].tolist() == [1, 2, 3]
    assert np.allclose(out["f"].astype(float), [0.1, 0.2, 0.3])


def test_partial_query(tmp_path):
    db_file = tmp_path / "part.db"
    mgr = SqliteDatabase(str(db_file), uri=False)

    schema = {"x": int, "y": str, "z": IPv4Address}
    tbl = mgr.register_table("t_p", schema)

    df = pd.DataFrame({
        "x": [10, 20],
        "y": ["foo", "bar"],
        "z": ["8.8.8.8", "1.1.1.1"]
    })
    tbl.insert_df(df)

    out = tbl.query("SELECT z, x FROM t_p WHERE x > ?", params=(10,))
    assert list(out.columns) == ["z", "x"]
    assert out["x"].tolist() == [20]
    assert isinstance(out.loc[0, "z"], IPv4Address)


def test_index_creation(tmp_path):
    db_file = tmp_path / "idx.db"
    mgr = SqliteDatabase(str(db_file), uri=False)

    schema = {"a": int, "b": int}
    idxs = [["a", "b"], ["b"]]
    tbl = mgr.register_table("t_idx", schema, indices=idxs)

    got = {row[1] for row in mgr.conn.execute("PRAGMA index_list(t_idx);")}
    expected = {f"idx_t_idx_{'_'.join(i)}" for i in idxs}
    assert expected.issubset(got)


def test_invalid_schema_and_qualifier(tmp_path):
    mgr = SqliteDatabase(str(tmp_path / "bad.db"), uri=False)
    with pytest.raises(TypeError):
        mgr.register_table("t_bad", {"foo": set})

    with pytest.raises(ValueError):
        mgr.register_table("t_bad2", {"a": int}, qualifiers={"a": "NOTVALID"})


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
