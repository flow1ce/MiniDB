"""Physical query operators."""
from collections import OrderedDict

from engine.expressions import eval_expr
from sql_compiler.ast import Binary, Identifier, Literal, Star, Unary


def expression_from_dict(value):
    """把 Plan 中字典形式的表达式还原成可由 eval_expr 计算的 AST。"""
    if value is None or not isinstance(value, dict):
        return value
    node = value.get("node")
    common = {"line": value.get("line", 1), "column": value.get("column", 1)}
    if node == "Literal":
        return Literal(value=value.get("value"), dtype=value.get("dtype", "NULL"), **common)
    if node == "Identifier":
        return Identifier(name=value.get("name", ""), **common)
    if node == "Star":
        return Star(**common)
    if node == "Unary":
        return Unary(op=value.get("op", ""), expr=expression_from_dict(value.get("expr")), **common)
    if node == "Binary":
        return Binary(left=expression_from_dict(value.get("left")), op=value.get("op", ""),
                      right=expression_from_dict(value.get("right")), **common)
    raise ValueError(f"unsupported expression node {node!r}")


class SeqScanOperator:
    """顺序扫描算子：从 TableHeap 读取表中所有有效记录。"""
    def execute(self, plan, context, execute_child):
        return context.scan_table(plan.args["table"], plan.args.get("columns"))


class IndexScanOperator:
    """索引扫描算子：通过 B+ 树定位候选 RID 并回表取行。"""
    def execute(self, plan, context, execute_child):
        return context.scan_index(plan.args)


class FilterOperator:
    """过滤算子：只保留 WHERE 表达式计算结果严格为 TRUE 的记录。"""
    def execute(self, plan, context, execute_child):
        expression = expression_from_dict(plan.args.get("expr"))
        return [row for row in execute_child(plan.children[0]) if eval_expr(expression, row) is True]


class NestedLoopJoinOperator:
    """嵌套循环 JOIN：枚举左右表记录组合并计算 ON 条件。"""
    def execute(self, plan, context, execute_child):
        left_rows = execute_child(plan.children[0])
        right_rows = execute_child(plan.children[1])
        left_table = context.base_table(plan.children[0])
        right_table = plan.args["table"]
        condition = expression_from_dict(plan.args["on"])
        joined = []
        for left in left_rows:
            for right in right_rows:
                row = dict(left)
                row.update({f"{left_table}.{key}": value for key, value in left.items()})
                row.update({f"{right_table}.{key}": value for key, value in right.items()})
                if eval_expr(condition, row) is True:
                    joined.append(row)
        return joined


class GroupByOperator:
    """分组算子：按分组表达式的值聚合记录，供上层聚合函数使用。"""
    def execute(self, plan, context, execute_child):
        expressions = [expression_from_dict(item) for item in plan.args.get("keys", [])]
        groups = OrderedDict()
        for row in execute_child(plan.children[0]):
            groups.setdefault(tuple(eval_expr(expr, row) for expr in expressions), []).append(row)
        return [dict(rows[0], _group=rows) for rows in groups.values()]


class ProjectOperator:
    """投影算子：计算 SELECT 列、聚合函数以及 DISTINCT 去重。"""
    AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}

    @staticmethod
    def aggregate(expr, rows):
        values = [eval_expr(expr.expr, row) for row in rows]
        values = [value for value in values if value is not None]
        if expr.op == "COUNT": return len(values)
        if expr.op == "SUM": return sum(values) if values else None
        if expr.op == "AVG": return sum(values) / len(values) if values else None
        if expr.op == "MIN": return min(values) if values else None
        if expr.op == "MAX": return max(values) if values else None

    def execute(self, plan, context, execute_child):
        expressions = [expression_from_dict(item) for item in plan.args.get("columns", [])]
        rows = execute_child(plan.children[0])
        if any(isinstance(expr, Unary) and expr.op in self.AGGREGATES for expr in expressions) and not any("_group" in row for row in rows):
            rows = [{"_group": rows}]
        output = []
        for source in rows:
            if len(expressions) == 1 and isinstance(expressions[0], Star):
                projected = {key: value for key, value in source.items() if not key.startswith("_") and "." not in key}
            else:
                projected = {}
                for index, expr in enumerate(expressions):
                    key = expr.name if isinstance(expr, Identifier) else (expr.op.lower() if isinstance(expr, Unary) else f"expr{index + 1}")
                    value = self.aggregate(expr, source.get("_group", [source])) if isinstance(expr, Unary) and expr.op in self.AGGREGATES else eval_expr(expr, source)
                    projected[key] = value
            projected["_source"] = source
            output.append(projected)
        if plan.args.get("distinct"):
            seen = set(); unique = []
            for row in output:
                key = tuple((k, v) for k, v in row.items() if k != "_source")
                if key not in seen:
                    seen.add(key); unique.append(row)
            output = unique
        return output


class OrderByOperator:
    """排序算子：支持多关键字和 ASC/DESC，并允许按未投影源列排序。"""
    def execute(self, plan, context, execute_child):
        rows = execute_child(plan.children[0])
        keys = [(expression_from_dict(expr), direction) for expr, direction in plan.args.get("keys", [])]
        for expression, direction in reversed(keys):
            def sort_key(row):
                value = eval_expr(expression, row.get("_source", row))
                return value is None, value
            rows.sort(key=sort_key, reverse=direction == "DESC")
        return rows


class OffsetOperator:
    """偏移算子：跳过结果集前 offset 条记录。"""
    def execute(self, plan, context, execute_child):
        return execute_child(plan.children[0])[plan.args["offset"]:]


class LimitOperator:
    """限制算子：只返回结果集前 limit 条记录。"""
    def execute(self, plan, context, execute_child):
        return execute_child(plan.children[0])[:plan.args["limit"]]


OPERATORS = {
    # Plan 节点名到物理算子实例的注册表，是 PlanExecutor 的分派依据。
    "SeqScan": SeqScanOperator(), "IndexScan": IndexScanOperator(),
    "Filter": FilterOperator(), "NestedLoopJoin": NestedLoopJoinOperator(),
    "GroupBy": GroupByOperator(), "Project": ProjectOperator(),
    "OrderBy": OrderByOperator(), "Offset": OffsetOperator(), "Limit": LimitOperator(),
}

__all__ = ["OPERATORS", "expression_from_dict"]
