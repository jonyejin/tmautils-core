"""
Tests for SQL schema generation from Pydantic models with PyArrow schemas.
"""

import pytest
import pyarrow as pa
from typing import ClassVar
from pydantic import BaseModel
import os

from tmautils.db.sql_schema import (
    arrow_type_to_sql,
    generate_create_table_sql,
)


class TestArrowTypeToSql:
    """Tests for arrow_type_to_sql() function"""

    def test_timestamp_with_tz(self):
        assert arrow_type_to_sql(pa.timestamp(
            "us", tz="UTC"
        )) == "TIMESTAMP WITH TIME ZONE"

    def test_timestamp_without_tz(self):
        assert arrow_type_to_sql(pa.timestamp("us")) == "TIMESTAMP"

    def test_int64(self):
        assert arrow_type_to_sql(pa.int64()) == "BIGINT"

    def test_int32(self):
        assert arrow_type_to_sql(pa.int32()) == "INTEGER"

    def test_int16(self):
        assert arrow_type_to_sql(pa.int16()) == "SMALLINT"

    def test_int8(self):
        assert arrow_type_to_sql(pa.int8()) == "TINYINT"

    def test_uint64(self):
        assert arrow_type_to_sql(pa.uint64()) == "UBIGINT"

    def test_uint32(self):
        assert arrow_type_to_sql(pa.uint32()) == "UINTEGER"

    def test_uint16(self):
        assert arrow_type_to_sql(pa.uint16()) == "USMALLINT"

    def test_uint8(self):
        assert arrow_type_to_sql(pa.uint8()) == "UTINYINT"

    def test_float64(self):
        assert arrow_type_to_sql(pa.float64()) == "DOUBLE"

    def test_float32(self):
        assert arrow_type_to_sql(pa.float32()) == "REAL"

    def test_string(self):
        assert arrow_type_to_sql(pa.string()) == "VARCHAR"

    def test_large_string(self):
        assert arrow_type_to_sql(pa.large_string()) == "VARCHAR"

    def test_boolean(self):
        assert arrow_type_to_sql(pa.bool_()) == "BOOLEAN"

    def test_binary(self):
        assert arrow_type_to_sql(pa.binary()) == "BLOB"

    def test_large_binary(self):
        assert arrow_type_to_sql(pa.large_binary()) == "BLOB"

    def test_date(self):
        assert arrow_type_to_sql(pa.date32()) == "DATE"

    def test_time(self):
        assert arrow_type_to_sql(pa.time64("us")) == "TIME"

    def test_decimal(self):
        assert arrow_type_to_sql(pa.decimal128(10, 2)) == "DECIMAL(10,2)"

    def test_unsupported_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported Arrow type"):
            arrow_type_to_sql(pa.list_(pa.int32()))

    def test_unsupported_dialect_raises(self):
        with pytest.raises(ValueError, match="Unsupported SQL dialect"):
            arrow_type_to_sql(pa.int64(), dialect="postgresql")


class TestGenerateCreateTableSql:
    """Tests for generate_create_table_sql() function"""

    def test_basic_table_no_metadata(self):
        """Test basic table generation without SQL metadata"""

        class SimpleModel(BaseModel):
            id: int
            name: str

            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("id", pa.int64()),
                pa.field("name", pa.string()),
            ])

        sql = generate_create_table_sql("users", SimpleModel)
        sql_str = "\n".join(sql)

        # Should contain CREATE TABLE with columns
        assert "CREATE TABLE IF NOT EXISTS users" in sql_str
        assert "id BIGINT" in sql_str
        assert "name VARCHAR" in sql_str

        # Should not contain CREATE INDEX (no indices defined)
        assert "CREATE INDEX" not in sql_str

    def test_table_with_constraints(self):
        """Test table generation with constraints"""

        class ModelWithConstraints(BaseModel):
            id: int
            email: str

            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("id", pa.int64()),
                pa.field("email", pa.string()),
            ])

            SQL_TABLE_CONSTRAINTS: ClassVar[list[str]] = [
                "PRIMARY KEY (id)",
                "UNIQUE (email)"
            ]

        sql = generate_create_table_sql("users", ModelWithConstraints)
        sql_str = "\n".join(sql)

        assert "CREATE TABLE IF NOT EXISTS users" in sql_str
        assert "id BIGINT" in sql_str
        assert "email VARCHAR" in sql_str
        assert "PRIMARY KEY (id)" in sql_str
        assert "UNIQUE (email)" in sql_str

    def test_table_with_indices(self):
        """Test table generation with indices"""

        class ModelWithIndices(BaseModel):
            id: int
            name: str
            email: str

            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("id", pa.int64()),
                pa.field("name", pa.string()),
                pa.field("email", pa.string()),
            ])

            SQL_TABLE_INDICES: ClassVar[list[list[str]]] = [
                ["name"],
                ["email", "name"]
            ]

        sql = generate_create_table_sql("users", ModelWithIndices)
        sql_str = "\n".join(sql)

        # Should have CREATE TABLE
        assert "CREATE TABLE IF NOT EXISTS users" in sql_str

        # Should have CREATE INDEX statements
        assert "CREATE INDEX IF NOT EXISTS idx_users_name ON users (name)" in sql_str
        assert "CREATE INDEX IF NOT EXISTS idx_users_email_name ON users (email, name)" in sql_str

    def test_table_with_type_overrides(self):
        """Test table generation with type overrides"""

        class ModelWithOverrides(BaseModel):
            id: int
            name: str

            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("id", pa.int64()),
                pa.field("name", pa.string()),
            ])

            SQL_TYPE_OVERRIDES: ClassVar[dict[str, str]] = {
                "name": "VARCHAR(255)"
            }

        sql = generate_create_table_sql("users", ModelWithOverrides)
        sql_str = "\n".join(sql)

        assert "id BIGINT" in sql_str
        assert "name VARCHAR(255)" in sql_str  # Override applied
        assert "name VARCHAR," not in sql_str  # Not the default

    def test_table_with_all_metadata(self):
        """Test table generation with constraints, indices, and overrides"""

        class FullModel(BaseModel):
            id: int
            name: str
            email: str

            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("id", pa.int64()),
                pa.field("name", pa.string()),
                pa.field("email", pa.string()),
            ])

            SQL_TABLE_CONSTRAINTS: ClassVar[list[str]] = [
                "PRIMARY KEY (id)"
            ]

            SQL_TABLE_INDICES: ClassVar[list[list[str]]] = [
                ["email"]
            ]

            SQL_TYPE_OVERRIDES: ClassVar[dict[str, str]] = {
                "email": "VARCHAR(320)"  # Max email length
            }

        sql = generate_create_table_sql("users", FullModel)
        sql_str = "\n".join(sql)

        # Check CREATE TABLE
        assert "CREATE TABLE IF NOT EXISTS users" in sql_str
        assert "id BIGINT" in sql_str
        assert "name VARCHAR" in sql_str
        assert "email VARCHAR(320)" in sql_str
        assert "PRIMARY KEY (id)" in sql_str

        # Check CREATE INDEX
        assert "CREATE INDEX IF NOT EXISTS idx_users_email ON users (email)" in sql_str

    def test_if_not_exists_true(self):
        """Test that IF NOT EXISTS is included by default"""

        class SimpleModel(BaseModel):
            id: int
            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("id", pa.int64()),
            ])

        sql = generate_create_table_sql(
            "test", SimpleModel, if_not_exists=True)
        sql_str = "\n".join(sql)
        assert "CREATE TABLE IF NOT EXISTS test" in sql_str

    def test_if_not_exists_false(self):
        """Test that IF NOT EXISTS can be disabled"""

        class SimpleModel(BaseModel):
            id: int
            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("id", pa.int64()),
            ])

        sql = generate_create_table_sql(
            "test", SimpleModel, if_not_exists=False)
        sql_str = "\n".join(sql)
        assert "CREATE TABLE test" in sql_str
        assert "IF NOT EXISTS" not in sql_str

    def test_model_without_arrow_schema_raises(self):
        """Test that missing ARROW_SCHEMA raises ValueError"""

        class NoSchemaModel(BaseModel):
            id: int

        with pytest.raises(ValueError, match="must have ARROW_SCHEMA"):
            generate_create_table_sql("test", NoSchemaModel)

    def test_arrow_schema_wrong_type_raises(self):
        """Test that ARROW_SCHEMA of wrong type raises ValueError"""

        class WrongTypeModel(BaseModel):
            id: int
            ARROW_SCHEMA: ClassVar[str] = "not a schema"

        with pytest.raises(ValueError, match="must be a pyarrow.Schema instance"):
            generate_create_table_sql("test", WrongTypeModel)

    def test_timestamp_conversion(self):
        """Test that timestamp fields convert correctly"""

        class TimestampModel(BaseModel):
            ts_utc: float
            ts_naive: float

            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("ts_utc", pa.timestamp("us", tz="UTC")),
                pa.field("ts_naive", pa.timestamp("us")),
            ])

        sql = generate_create_table_sql("events", TimestampModel)
        sql_str = "\n".join(sql)

        assert "ts_utc TIMESTAMP WITH TIME ZONE" in sql_str
        assert "ts_naive TIMESTAMP" in sql_str

    def test_empty_indices_list(self):
        """Test that empty indices list doesn't create any indices"""

        class EmptyIndicesModel(BaseModel):
            id: int
            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("id", pa.int64()),
            ])
            SQL_TABLE_INDICES: ClassVar[list[list[str]]] = []

        sql = generate_create_table_sql("test", EmptyIndicesModel)
        sql_str = "\n".join(sql)
        assert "CREATE INDEX" not in sql_str

    def test_index_name_generation(self):
        """Test that index names are generated correctly"""

        class MultiColIndexModel(BaseModel):
            a: int
            b: int
            c: int

            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("a", pa.int64()),
                pa.field("b", pa.int64()),
                pa.field("c", pa.int64()),
            ])

            SQL_TABLE_INDICES: ClassVar[list[list[str]]] = [
                ["a"],
                ["a", "b"],
                ["a", "b", "c"]
            ]

        sql = generate_create_table_sql("test", MultiColIndexModel)
        sql_str = "\n".join(sql)

        assert "idx_test_a ON test (a)" in sql_str
        assert "idx_test_a_b ON test (a, b)" in sql_str
        assert "idx_test_a_b_c ON test (a, b, c)" in sql_str

    def test_real_world_example(self):
        """Test with a real-world example similar to openintel.py models"""

        class NewlyRegisteredFqdn(BaseModel):
            msg_timestamp: float
            fqdn: str
            cert_index: int
            ct_name: str
            timestamp: int

            ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
                pa.field("msg_timestamp", pa.timestamp("us", tz="UTC")),
                pa.field("fqdn", pa.string()),
                pa.field("cert_index", pa.int64()),
                pa.field("ct_name", pa.string()),
                pa.field("timestamp", pa.int64()),
            ])

            SQL_TABLE_CONSTRAINTS: ClassVar[list[str]] = [
                "PRIMARY KEY (msg_timestamp, fqdn)"
            ]

            SQL_TABLE_INDICES: ClassVar[list[list[str]]] = [
                ["fqdn"],
                ["fqdn", "msg_timestamp"]
            ]

        sql = generate_create_table_sql(
            "newly_registered_fqdn", NewlyRegisteredFqdn)
        sql_str = "\n".join(sql)

        # Verify structure
        assert "CREATE TABLE IF NOT EXISTS newly_registered_fqdn" in sql_str
        assert "msg_timestamp TIMESTAMP WITH TIME ZONE" in sql_str
        assert "fqdn VARCHAR" in sql_str
        assert "cert_index BIGINT" in sql_str
        assert "ct_name VARCHAR" in sql_str
        assert "timestamp BIGINT" in sql_str
        assert "PRIMARY KEY (msg_timestamp, fqdn)" in sql_str
        assert "idx_newly_registered_fqdn_fqdn ON newly_registered_fqdn (fqdn)" in sql_str
        assert "idx_newly_registered_fqdn_fqdn_msg_timestamp ON newly_registered_fqdn (fqdn, msg_timestamp)" in sql_str


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
