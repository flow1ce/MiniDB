"""Human-friendly terminal rendering for structured database results."""
import json


def _value(value):
    # 将 Python 的 None/布尔值显示为对应 SQL 字面量。
    if value is None: return "NULL"
    if value is True: return "TRUE"
    if value is False: return "FALSE"
    return str(value)


def _table(columns, rows):
    """按内容计算列宽，将查询行渲染成终端 ASCII 表格。"""
    values = [[_value(row.get(column)) for column in columns] for row in rows]
    widths = [len(str(column)) for column in columns]
    for row in values:
        widths = [max(widths[i], len(value)) for i, value in enumerate(row)]
    line = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    header = "| " + " | ".join(str(column).ljust(widths[i]) for i, column in enumerate(columns)) + " |"
    body = ["| " + " | ".join(value.ljust(widths[i]) for i, value in enumerate(row)) + " |" for row in values]
    return "\n".join([line, header, line, *body, line])


def _metrics(metrics):
    # 把最重要的执行与 I/O 指标压缩成一行摘要。
    if not metrics: return ""
    return (
        f"Time {metrics.get('total_time_ms', 0):.3f} ms | "
        f"examined {metrics.get('rows_examined', 0)} | "
        f"returned {metrics.get('rows_returned', 0)} | "
        f"pages read {metrics.get('pages_read', 0)} | "
        f"buffer hits {metrics.get('buffer_hits', 0)}"
    )


def render_result(result):
    """根据 query/explain/metadata/stats 等结果类型选择对应终端展示格式。"""
    if isinstance(result, list):
        return "\n\n".join(render_result(item) for item in result)
    if not result.get("success", False):
        error = result.get("error", {})
        return f"{error.get('stage', 'Database')}Error at line {error.get('line', 1)}, column {error.get('column', 1)}: {error.get('message', '')}"
    kind = result.get("type")
    if kind == "query":
        table = _table(result.get("columns", []), result.get("rows", []))
        return f"{table}\n{result.get('row_count', 0)} row(s)\n{_metrics(result.get('metrics'))}"
    if kind == "explain":
        text = result.get("text", "")
        actual = result.get("actual")
        extra = ""
        if actual:
            extra = f"\n\nActual result: {actual.get('count', 0)} row(s)"
        return f"{text}{extra}\n{_metrics(result.get('metrics'))}"
    if kind == "metadata" and "tables" in result:
        return _table(["table"], [{"table": name} for name in result["tables"]])
    if kind == "metadata" and "columns" in result:
        return _table(["name", "type", "primary_key", "unique", "not_null"], result["columns"])
    if kind == "stats":
        return _table(["metric", "value"], [{"metric": key, "value": value} for key, value in result["stats"].items()])
    message = result.get("message", "OK")
    metrics = _metrics(result.get("metrics"))
    return message + (f"\n{metrics}" if metrics else "")


__all__ = ["render_result"]
