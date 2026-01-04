from typing import Type, Optional
import pyarrow as pa
from pydantic import BaseModel


def arrow_type_to_sql(arrow_type: pa.DataType, dialect: str = "duckdb") -> str:
    """
    Convert a PyArrow data type to a SQL type string.

    Args:
        arrow_type:
            The PyArrow data type to convert

        dialect:
            The SQL dialect (currently only "duckdb" is supported)

    Returns:
        SQL type string (e.g., "BIGINT", "VARCHAR", "TIMESTAMP WITH TIME ZONE")

    Raises:
        ValueError: If the Arrow type is not supported for SQL conversion
    """

    if dialect != "duckdb":
        raise ValueError(f"Unsupported SQL dialect: {dialect}")

    # Timestamp types
    if pa.types.is_timestamp(arrow_type):
        if arrow_type.tz:
            return "TIMESTAMP WITH TIME ZONE"
        return "TIMESTAMP"

    # Integer types
    elif pa.types.is_int64(arrow_type):
        return "BIGINT"
    elif pa.types.is_int32(arrow_type):
        return "INTEGER"
    elif pa.types.is_int16(arrow_type):
        return "SMALLINT"
    elif pa.types.is_int8(arrow_type):
        return "TINYINT"

    # Unsigned integer types
    elif pa.types.is_uint64(arrow_type):
        return "UBIGINT"
    elif pa.types.is_uint32(arrow_type):
        return "UINTEGER"
    elif pa.types.is_uint16(arrow_type):
        return "USMALLINT"
    elif pa.types.is_uint8(arrow_type):
        return "UTINYINT"

    # Floating point types
    elif pa.types.is_float64(arrow_type):
        return "DOUBLE"
    elif pa.types.is_float32(arrow_type):
        return "REAL"

    # String types
    elif pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return "VARCHAR"

    # Boolean type
    elif pa.types.is_boolean(arrow_type):
        return "BOOLEAN"

    # Binary types
    elif pa.types.is_binary(arrow_type) or pa.types.is_large_binary(arrow_type):
        return "BLOB"

    # Date and time types
    elif pa.types.is_date(arrow_type):
        return "DATE"
    elif pa.types.is_time(arrow_type):
        return "TIME"

    # Decimal type
    elif pa.types.is_decimal(arrow_type):
        precision = arrow_type.precision
        scale = arrow_type.scale
        return f"DECIMAL({precision},{scale})"

    else:
        raise ValueError(
            f"Unsupported Arrow type for SQL conversion: {arrow_type}. "
            f"Use SQL_TYPE_OVERRIDES to specify a custom type for this column."
        )


def _extract_sql_metadata(model_cls: Type[BaseModel]):
    constraints: list[str] = getattr(
        model_cls, 'SQL_TABLE_CONSTRAINTS', []
    )
    indices: list[list[str]] = getattr(
        model_cls, 'SQL_TABLE_INDICES', []
    )
    type_overrides: dict[str, str] = getattr(
        model_cls, 'SQL_TYPE_OVERRIDES', {}
    )
    return constraints, indices, type_overrides


def generate_create_table_sql(
    table_name: str,
    model_cls: Type[BaseModel],
    *,
    if_not_exists: bool = True,
    dialect: str = "duckdb"
) -> list[str]:
    """
    Generate SQL DDL (CREATE TABLE + CREATE INDEX statements) from a Pydantic model.

    The model must have an ARROW_SCHEMA ClassVar defining the PyArrow schema.
    SQL types are auto-derived from Arrow types. The model can optionally define:

    - `SQL_TABLE_CONSTRAINTS` (`list[str]`) - Constraints to add to CREATE TABLE
    - `SQL_TABLE_INDICES` (`list[list[str]]`) - Indices to create (list of column lists)
    - `SQL_TYPE_OVERRIDES` (`dict[str, str]`) - Override auto-derived types for specific columns

    Args:
        table_name:
            Name of the SQL table to create

        model_cls:
            Pydantic model class with ARROW_SCHEMA ClassVar

        if_not_exists:
            Whether to use CREATE TABLE IF NOT EXISTS (default: True)

        dialect:
            SQL dialect (default: "duckdb")

    Returns:
        List of SQL statements (CREATE TABLE followed by CREATE INDEX statements)

    Raises:
        ValueError: If model doesn't have ARROW_SCHEMA or if conversion fails

    Examples:
        >>> class MyModel(BaseModel):
        ...     id: int
        ...     name: str
        ...     ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        ...         pa.field("id", pa.int64()),
        ...         pa.field("name", pa.string()),
        ...     ])
        ...     SQL_TABLE_CONSTRAINTS: ClassVar[list[str]] = ["PRIMARY KEY (id)"]
        ...     SQL_TABLE_INDICES: ClassVar[list[list[str]]] = [["name"]]
        >>> sql = generate_create_table_sql("users", MyModel)
        >>> sql
        ['CREATE TABLE IF NOT EXISTS users (\\n    id BIGINT,\\n    name VARCHAR,\\n    PRIMARY KEY (id)\\n);', 'CREATE INDEX IF NOT EXISTS idx_users_name ON users (name);']
        >>> for stmt in sql:
        ...     print(stmt)
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT,
            name VARCHAR,
            PRIMARY KEY (id)
        );
        CREATE INDEX IF NOT EXISTS idx_users_name ON users (name);
    """

    # Extract ARROW_SCHEMA
    if not hasattr(model_cls, "ARROW_SCHEMA"):
        raise ValueError(
            f"Model {model_cls.__name__} must have ARROW_SCHEMA ClassVar attribute"
        )
    schema: pa.Schema = model_cls.ARROW_SCHEMA
    if not isinstance(schema, pa.Schema):
        raise ValueError(
            f"Model.ARROW_SCHEMA must be a pyarrow.Schema instance, "
            f"got {type(schema)}"
        )

    # Extract SQL metadata
    constraints, indices, type_overrides = _extract_sql_metadata(model_cls)

    # Generate column definitions
    col_defs = []
    for field in schema:
        col_name = field.name

        # Check for type override
        if col_name in type_overrides:
            sql_type = type_overrides[col_name]
        else:
            sql_type = arrow_type_to_sql(field.type, dialect=dialect)

        col_defs.append(f"    {col_name} {sql_type}")

    # Add constraints
    if constraints:
        for constraint in constraints:
            col_defs.append(f"    {constraint}")

    # Build CREATE TABLE statement
    create_table_clause = "CREATE TABLE IF NOT EXISTS" if if_not_exists else "CREATE TABLE"
    create_table = f"{create_table_clause} {table_name} (\n"
    create_table += ",\n".join(col_defs)
    create_table += "\n);"

    # Build CREATE INDEX statements
    index_statements = []
    if indices:
        for idx_cols in indices:
            if not idx_cols:
                continue

            # Generate index name: idx_{table}_{col1}_{col2}_...
            idx_name = f"idx_{table_name}_{'_'.join(idx_cols)}"
            cols_csv = ", ".join(idx_cols)

            create_index_clause = "CREATE INDEX IF NOT EXISTS" if if_not_exists else "CREATE INDEX"
            index_sql = f"{create_index_clause} {idx_name} ON {table_name} ({cols_csv});"
            index_statements.append(index_sql)

    # Combine all statements
    all_statements = [create_table] + index_statements
    return all_statements
