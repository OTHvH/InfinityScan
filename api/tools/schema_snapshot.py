"""Canonical schema snapshots for ORM-to-PostgreSQL drift checks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql.sqltypes import DateTime, Enum, Float, Numeric, String, Text, Uuid


def _normalize_expression(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value).replace('"', "")
    rendered = re.sub(r"::[a-zA-Z_][a-zA-Z0-9_]*(?:\s+precision)?", "", rendered)
    rendered = re.sub(
        r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*ANY\s*\(\s*ARRAY\[(.*?)\]\s*\)",
        r"\1 IN (\2)",
        rendered,
    )
    rendered = re.sub(
        r"\b(btrim|jsonb_typeof)\(([^()]*)\)",
        r"\1@@\2##",
        rendered,
    )
    rendered = rendered.replace("(", "").replace(")", "")
    rendered = rendered.replace("@@", "(").replace("##", ")")
    return re.sub(r"\s+", " ", rendered).strip()


def _type_snapshot(type_: object, dialect: str) -> dict[str, object]:
    if dialect == "postgresql" and hasattr(type_, "dialect_impl"):
        type_ = type_.dialect_impl(postgresql.dialect())
    if isinstance(type_, Enum) or hasattr(type_, "enums"):
        return {
            "family": "enum",
            "name": getattr(type_, "name", None),
            "labels": list(getattr(type_, "enums", ())),
        }
    rendered = str(type_).lower()
    if isinstance(type_, (postgresql.UUID, Uuid)) or rendered in {"uuid", "char(32)"}:
        return {"family": "uuid"}
    if isinstance(type_, postgresql.JSONB) or rendered == "jsonb":
        return {"family": "jsonb"}
    if rendered in {"json", "jsonb"}:
        return {"family": rendered}
    if isinstance(type_, Float) or "float" in rendered or "double precision" in rendered:
        return {"family": "float"}
    if isinstance(type_, DateTime) or rendered.startswith("datetime") or rendered.startswith("timestamp"):
        return {"family": "datetime", "timezone": bool(getattr(type_, "timezone", "with time zone" in rendered))}
    if isinstance(type_, Numeric) or rendered.startswith("numeric") or rendered.startswith("decimal"):
        return {
            "family": "numeric",
            "precision": getattr(type_, "precision", None),
            "scale": getattr(type_, "scale", None),
        }
    if isinstance(type_, String) or "character varying" in rendered or rendered.startswith("varchar"):
        return {"family": "string", "length": getattr(type_, "length", None)}
    if isinstance(type_, Text) or rendered == "text":
        return {"family": "text"}
    if rendered in {"boolean", "bool"}:
        return {"family": "boolean"}
    if rendered in {"integer", "int", "int4"}:
        return {"family": "integer"}
    if rendered in {"bigint", "int8"}:
        return {"family": "bigint"}
    if rendered in {"smallint", "int2"}:
        return {"family": "smallint"}
    if rendered in {"double precision", "float", "float8"}:
        return {"family": "float"}
    return {"family": rendered, "dialect": dialect}


def _constraint_columns(constraint: object) -> list[str]:
    return [column.name for column in getattr(constraint, "columns", ())]


def _metadata_table(table: object, dialect: str) -> dict[str, object]:
    primary_key = list(table.primary_key.columns.keys())
    foreign_keys = []
    for constraint in table.foreign_key_constraints:
        foreign_keys.append(
            {
                "columns": [element.parent.name for element in constraint.elements],
                "referred_table": constraint.referred_table.name,
                "referred_columns": [element.column.name for element in constraint.elements],
                "ondelete": constraint.ondelete,
                "onupdate": constraint.onupdate,
            }
        )
    unique = [
        {"columns": _constraint_columns(constraint)}
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    ]
    checks = [
        {"expression": _normalize_expression(constraint.sqltext)}
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    ]
    indexes = []
    for index in table.indexes:
        columns = []
        expressions = []
        for expression in index.expressions:
            if hasattr(expression, "name") and expression.name in table.c:
                columns.append(expression.name)
            else:
                expressions.append(_normalize_expression(expression))
        predicate = index.dialect_options.get("postgresql", {}).get("where")
        indexes.append(
            {
                "columns": columns,
                "expressions": expressions,
                "unique": bool(index.unique),
                "predicate": _normalize_expression(predicate),
                "include": sorted(
                    str(value)
                    for value in (index.dialect_options.get("postgresql", {}).get("include") or ())
                ),
            }
        )
    unique_columns = {tuple(value["columns"]) for value in unique}
    indexes = [
        index
        for index in indexes
        if not (index["unique"] and tuple(index["columns"]) in unique_columns)
    ]
    return {
        "name": table.name,
        "columns": sorted([
            {
                "name": column.name,
                "type": _type_snapshot(column.type, dialect),
                "nullable": bool(column.nullable),
            }
            for column in table.columns
        ], key=lambda value: value["name"]),
        "primary_key": primary_key,
        "foreign_keys": sorted(foreign_keys, key=lambda value: (value["columns"], value["referred_table"])),
        "unique": sorted(unique, key=lambda value: value["columns"]),
        "checks": sorted(checks, key=lambda value: value["expression"] or ""),
        "indexes": sorted(indexes, key=lambda value: (value["columns"], value["expressions"], value["unique"])),
    }


def snapshot_metadata(metadata: MetaData, *, dialect: str = "postgresql") -> dict[str, object]:
    enums: dict[str, dict[str, object]] = {}
    for table in metadata.sorted_tables:
        for column in table.columns:
            type_ = column.type
            if isinstance(type_, Enum) or hasattr(type_, "enums"):
                name = getattr(type_, "name", None)
                if name:
                    enums[name] = {
                        "name": name,
                        "labels": list(getattr(type_, "enums", ())),
                        "schema": "public" if dialect == "postgresql" else None,
                    }
    return {
        "format": 1,
        "dialect": dialect,
        "tables": sorted(
            (_metadata_table(table, dialect) for table in metadata.sorted_tables),
            key=lambda value: value["name"],
        ),
        "enums": sorted(enums.values(), key=lambda value: value["name"]),
    }


def _inspected_table(inspector: object, table_name: str, schema: str | None, dialect: str) -> dict[str, object]:
    columns = sorted([
        {
            "name": column["name"],
            "type": _type_snapshot(column["type"], dialect),
            "nullable": bool(column["nullable"]),
        }
        for column in inspector.get_columns(table_name, schema=schema)
    ], key=lambda value: value["name"])
    pk = inspector.get_pk_constraint(table_name, schema=schema).get("constrained_columns") or []
    foreign_keys = []
    for constraint in inspector.get_foreign_keys(table_name, schema=schema):
        foreign_keys.append(
            {
                "columns": constraint.get("constrained_columns") or [],
                "referred_table": constraint.get("referred_table"),
                "referred_columns": constraint.get("referred_columns") or [],
                "ondelete": (constraint.get("options") or {}).get("ondelete"),
                "onupdate": (constraint.get("options") or {}).get("onupdate"),
            }
        )
    unique = [
        {"columns": constraint.get("column_names") or []}
        for constraint in inspector.get_unique_constraints(table_name, schema=schema)
    ]
    checks = [
        {"expression": _normalize_expression(constraint.get("sqltext"))}
        for constraint in inspector.get_check_constraints(table_name, schema=schema)
    ]
    indexes = []
    for index in inspector.get_indexes(table_name, schema=schema):
        options = index.get("dialect_options") or {}
        predicate = options.get("postgresql_where")
        indexes.append(
            {
                "columns": index.get("column_names") or [],
                "expressions": index.get("expressions") or [],
                "unique": bool(index.get("unique")),
                "predicate": _normalize_expression(predicate),
                "include": sorted(str(value) for value in (options.get("postgresql_include") or ())),
            }
        )
    unique_columns = {tuple(value["columns"]) for value in unique}
    indexes = [
        index
        for index in indexes
        if not (index["unique"] and tuple(index["columns"]) in unique_columns)
    ]
    return {
        "name": table_name,
        "columns": columns,
        "primary_key": list(pk),
        "foreign_keys": sorted(foreign_keys, key=lambda value: (value["columns"], value["referred_table"] or "")),
        "unique": sorted(unique, key=lambda value: value["columns"]),
        "checks": sorted(checks, key=lambda value: value["expression"] or ""),
        "indexes": sorted(indexes, key=lambda value: (value["columns"], value["expressions"], value["unique"])),
    }


def snapshot_connection(connection: Connection, *, schema: str | None = "public") -> dict[str, object]:
    inspector = inspect(connection)
    dialect = connection.dialect.name
    tables = sorted(
        table for table in inspector.get_table_names(schema=schema) if table != "alembic_version"
    )
    enums: list[dict[str, object]] = []
    if dialect == "postgresql":
        rows = connection.execute(
            text(
                "SELECT n.nspname, t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) "
                "FROM pg_type t JOIN pg_enum e ON t.oid = e.enumtypid "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = :schema GROUP BY n.nspname, t.typname"
            ),
            {"schema": schema or "public"},
        ).all()
        enums = [{"schema": row[0], "name": row[1], "labels": list(row[2])} for row in rows]
    return {
        "format": 1,
        "dialect": dialect,
        "tables": [_inspected_table(inspector, table, schema, dialect) for table in tables],
        "enums": sorted(enums, key=lambda value: (value["schema"], value["name"])),
    }


def snapshot_engine(engine: Engine, *, schema: str | None = "public") -> dict[str, object]:
    with engine.connect() as connection:
        return snapshot_connection(connection, schema=schema)


def canonicalize_schema(schema: Mapping[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(schema, sort_keys=True, separators=(",", ":")))


def schema_hash(schema: Mapping[str, object]) -> str:
    payload = json.dumps(canonicalize_schema(schema), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def compare_schema(expected: Mapping[str, object], actual: Mapping[str, object]) -> dict[str, object]:
    expected_canonical = canonicalize_schema(expected)
    actual_canonical = canonicalize_schema(actual)
    return {
        "equal": expected_canonical == actual_canonical,
        "expected_hash": schema_hash(expected_canonical),
        "actual_hash": schema_hash(actual_canonical),
        "expected": expected_canonical,
        "actual": actual_canonical,
    }
