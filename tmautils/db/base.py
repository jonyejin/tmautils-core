# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import (
    List, Optional, Type, Any, Iterable, Protocol,
)
import math
from enum import StrEnum
from dataclasses import dataclass

import pyarrow as pa
from pydantic import BaseModel


class WriteMode(StrEnum):
    """
    Write modes for database operations.
    """

    APPEND = "append"
    INSERT_IGNORE = "insert_ignore"
    UPSERT = "upsert"


@dataclass(frozen=True)
class TableConfig:
    """
    Configuration for a database table.

    Attributes:
        model (Type[BaseModel]):
            The Pydantic model class representing the table schema.

        mode (WriteMode):
            The write mode to use for database operations.

        key_cols (Optional[List[str]]):
            List of key column names for modes that require them (e.g., UPSERT).
    """

    model: Type[BaseModel]
    mode: WriteMode = WriteMode.APPEND
    key_cols: Optional[List[str]] = None
    partition_cols: Optional[List[str]] = None

    def __post_init__(self):
        _validate_write_mode(
            self.mode,
            model_cls=self.model,
            key_cols=self.key_cols,
            partition_cols=self.partition_cols,
        )


class ArrowBackend(Protocol):
    """
    Protocol for backends that can flush PyArrow Tables to a database.
    For use with BufferedWriter.
    For an example implementation, see ParquetBackend.
    """

    def flush_arrow(
        self,
        table_name: str,
        config: TableConfig,
        data: pa.Table,
    ) -> None:
        ...


def pydantic_to_arrow(
    items: List[BaseModel],
    model_cls: Type[BaseModel]
) -> Optional[pa.Table]:
    """
    Convert a list of Pydantic model instances to a PyArrow Table.
    The Pydantic model class must have an `ARROW_SCHEMA` attribute
    defining the desired PyArrow schema.

    Args:
        items (List[BaseModel]):
            List of Pydantic model instances to convert.

        model_cls (Type[BaseModel]):
            The Pydantic model class corresponding to the instances.

    Returns:
        A PyArrow Table representing the data, or None if the input list is empty.
    """
    if not items:
        return None

    # Extract schema and do sanity checks
    if not hasattr(model_cls, "ARROW_SCHEMA"):
        raise ValueError("Model must have ARROW_SCHEMA attribute")
    schema: pa.Schema = model_cls.ARROW_SCHEMA
    if not isinstance(schema, pa.Schema):
        raise ValueError(
            "Model.ARROW_SCHEMA must be a pyarrow.Schema instance"
        )
    _verify_names_match(model_cls, schema)

    cols = []
    for field in schema:
        name = field.name
        vals = [getattr(item, name) for item in items]
        cols.append(_to_arrow_array(vals, field.type))
    return pa.Table.from_arrays(cols, schema=schema)


def _quote_ident(name: str):
    if not isinstance(name, str):
        raise TypeError("identifier must be a string")
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(s: str) -> str:
    if not isinstance(s, str):
        raise TypeError("literal must be a string")
    return "'" + s.replace("'", "''") + "'"


def _to_arrow_array_ts(values: Iterable[Any], typ: pa.DataType) -> pa.Array:
    unit = typ.unit
    scale = {
        's': 1, 'ms': 1_000, 'us': 1_000_000, 'ns': 1_000_000_000
    }[unit]
    conv: List[Optional[int]] = []
    for v in values:
        if v is None:
            conv.append(None)
        elif isinstance(v, (int, float)):
            if not math.isfinite(v):
                conv.append(None)
            else:
                conv.append(int(round(float(v) * scale)))
        else:
            raise ValueError(
                f"Cannot convert value '{v}' of type '{type(v)}' to timestamp: "
                f"expected int/float epoch seconds or None."
            )
    return pa.array(conv, type=typ)


def _to_arrow_array(values, typ: pa.DataType) -> pa.Array:
    if pa.types.is_timestamp(typ):
        return _to_arrow_array_ts(values, typ)
    return pa.array(values, type=typ)


def _verify_names_match(model_cls: Type[BaseModel], schema: pa.Schema):
    model_fields = list(model_cls.model_fields.keys())
    schema_fields = [f.name for f in schema]
    if set(model_fields) != set(schema_fields):
        raise ValueError(
            "Pydantic model field names do not match ARROW_SCHEMA.\n"
            f"Model fields: {sorted(model_fields)}\n"
            f"Schema fields: {sorted(schema_fields)}"
        )


def _validate_write_mode(
    mode: WriteMode,
    *,
    table: Optional[pa.Table] = None,
    model_cls: Optional[Type[BaseModel]] = None,
    key_cols: Optional[List[str]] = None,
    partition_cols: Optional[List[str]] = None,
):
    if table is not None:
        fields = set(table.schema.names)
    elif model_cls is not None:
        fields = set(model_cls.model_fields.keys())
    else:
        fields = None

    if mode in {WriteMode.INSERT_IGNORE, WriteMode.UPSERT}:
        if not key_cols:
            raise ValueError(f"mode='{mode}' requires key_cols")
        if fields is None:
            raise ValueError(
                f"mode='{mode}' requires either table or model_cls to validate key_cols"
            )
        invalid = sorted([c for c in key_cols if c not in fields])
        if invalid:
            raise ValueError(
                f"key_cols {invalid} not present in table/model fields: {sorted(fields)}"
            )

    if partition_cols:
        if fields is None:
            raise ValueError(
                "partition_cols requires either table or model_cls to validate"
            )
        invalid = sorted([c for c in partition_cols if c not in fields])
        if invalid:
            raise ValueError(
                f"partition_cols {invalid} not present in table/model fields: {sorted(fields)}"
            )
