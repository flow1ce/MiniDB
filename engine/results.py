"""Uniform public result envelopes."""
from sql_compiler.ast import (
    CreateIndex, CreateTable, CreateUser, Delete, Describe, DropIndex,
    DropTable, Explain, Grant, Insert, Revoke, Select, ShowTables, Txn, Update,
)


def normalize_result(statement, raw, metrics):
    """把各语句不同的原始返回值统一包装成 CLI/Web 可直接消费的结果信封。"""
    base = {"success": True, "metrics": metrics.to_dict()}
    if isinstance(statement, Select):
        return {
            **base, "type": "query", "columns": raw["columns"], "rows": raw["rows"],
            "row_count": raw["count"],
        }
    if isinstance(statement, Explain):
        result = {**base, "type": "explain", **raw}
        return result
    if isinstance(statement, Insert):
        return {**base, "type": "mutation", "message": "1 row inserted", "affected_rows": raw["inserted"], "details": raw}
    if isinstance(statement, (Update, Delete)):
        count = raw["affected"]
        return {**base, "type": "mutation", "message": f"{count} row(s) affected", "affected_rows": count}
    if isinstance(statement, ShowTables):
        return {**base, "type": "metadata", "tables": raw["tables"], "row_count": raw["count"]}
    if isinstance(statement, Describe):
        return {**base, "type": "metadata", **raw}
    if isinstance(statement, Txn):
        return {**base, "type": "transaction", "message": raw}
    if isinstance(statement, (CreateTable, CreateIndex, DropTable, DropIndex, CreateUser, Grant, Revoke)):
        return {**base, "type": "command", "message": raw}
    return {**base, "type": "command", "message": str(raw)}
