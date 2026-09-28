"""Plan-driven execution engine."""
from engine.operators import OPERATORS


class ExecutionContext:
    """物理算子的运行上下文：提供表/索引扫描、快照选择、指标和行锁。"""
    def __init__(self, database):
        self.database = database

    def base_table(self, plan):
        # 沿计划左支找到基础扫描表，供 JOIN 构造限定列名。
        if plan.op in ("SeqScan", "IndexScan"):
            return plan.args["table"]
        return self.base_table(plan.children[0]) if plan.children else ""

    def _lock_row(self, table, rid):
        # 事务读取实际记录时获取 RID 级共享锁，并登记语句结束时的释放列表。
        transaction = self.database.transaction
        if rid is None:
            return
        txn_id = transaction.txn_id if transaction else getattr(self.database, '_statement_lock_txn', -abs(id(self.database)))
        resource = f"row:{table.lower()}:{rid.page_id}:{rid.slot_id}"
        self.database.lock_manager.acquire(txn_id, resource, "SHARED", timeout=self.database.lock_timeout)
        if transaction:
            transaction.locks.add(resource)
            self.database._statement_shared_locks.append(resource)

    def scan_table(self, table_name, columns=None):
        """顺序扫描表或事务快照，过滤删除行并记录扫描指标。"""
        table = self.database.catalog.get(table_name)
        snapshot = self.database._uses_snapshot(table)
        heap = self.database._heap(table)
        # Keep full source rows until projection so ORDER BY and join predicates
        # may reference columns that are not part of the final SELECT list.
        source = self.database.txn_visible.get(table.name.lower(), []) if snapshot else heap.scan()
        rows = []
        for rid, row in source:
            if self.database._active_metrics: self.database._active_metrics.examine()
            if row is not None and not row.get("_deleted"):
                self._lock_row(table.name, rid)
                if not snapshot: row = heap.get(rid)
                if row is None or row.get("_deleted"): continue
                rows.append(dict(row))
        return rows

    def scan_index(self, args):
        """用 B+ 树等值/范围查出 RID，再回表读取完整记录。"""
        table = self.database.catalog.get(args["table"])
        if self.database._uses_snapshot(table):
            return self.scan_table(table.name, args.get("columns"))
        meta = table.indexes[args["index"]]
        tree = self.database._index_tree(table, args["index"], meta)
        if "key" in args:
            identifiers = tree.search(args["key"])
        elif "high" in args:
            identifiers = tree.range(high=args["high"], high_inclusive=True)
        else:
            identifiers = tree.range(low=args["low"], low_inclusive=True)
        heap = self.database._heap(table)
        rows = []
        if self.database._active_metrics: self.database._active_metrics.examine_index(len(identifiers))
        for rid in identifiers:
            if self.database._active_metrics: self.database._active_metrics.examine()
            row = heap.get(rid)
            if row is not None and not row.get("_deleted"):
                self._lock_row(table.name, rid)
                row = heap.get(rid)
                if row is None or row.get("_deleted"): continue
                rows.append(dict(row))
        return rows


class PlanExecutor:
    """计划执行器：按 Plan.op 找到物理算子，并递归执行整棵计划树。"""
    def __init__(self, database):
        self.context = ExecutionContext(database)

    def rows(self, plan):
        # 通用算子分派入口；execute_child 回调让每个算子按需执行子计划。
        operator = OPERATORS.get(plan.op)
        if operator is None:
            raise ValueError(f"no physical operator for plan node {plan.op!r}")
        return operator.execute(plan, self.context, self.rows)

    def select(self, plan, table):
        """执行 SELECT 计划并清理内部字段；空结果时仍根据投影/表结构返回列名。"""
        rows = self.rows(plan)
        cleaned = [{key: value for key, value in row.items() if key != "_source"} for row in rows]
        if cleaned:
            columns = list(cleaned[0])
        else:
            project = self._find(plan, "Project")
            expressions = project.args.get("columns", []) if project else []
            if len(expressions) == 1 and expressions[0].get("node") == "Star":
                columns = [column["name"] for column in table.columns]
            else:
                columns = [
                    item.get("name") if item.get("node") == "Identifier"
                    else item.get("op", f"expr{i + 1}").lower()
                    for i, item in enumerate(expressions)
                ]
        return {"columns": columns, "rows": cleaned, "count": len(cleaned)}

    def _find(self, plan, operation):
        # 在计划树中查找指定算子，主要用于推导空结果集的输出列。
        if plan.op == operation:
            return plan
        for child in plan.children:
            found = self._find(child, operation)
            if found:
                return found
        return None


__all__ = ["ExecutionContext", "PlanExecutor"]
