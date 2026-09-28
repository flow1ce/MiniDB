"""Per-statement execution metrics used by CLI, Web UI and EXPLAIN ANALYZE."""
from dataclasses import dataclass, field
import time


@dataclass
class ExecutionMetrics:
    """单条语句指标快照：统计耗时、扫描行、索引项和 Buffer Pool 增量。"""
    started_ns: int = field(default_factory=time.perf_counter_ns)
    planning_ns: int = 0
    rows_examined: int = 0
    rows_returned: int = 0
    index_entries_examined: int = 0
    buffer_before: dict = field(default_factory=dict)
    buffer_after: dict = field(default_factory=dict)

    @classmethod
    def start(cls, buffer_pool):
        return cls(buffer_before=buffer_pool.stats())

    def finish(self, buffer_pool, rows_returned=0):
        self.rows_returned = rows_returned
        self.buffer_after = buffer_pool.stats()
        return self

    def add_planning_ns(self, elapsed):
        self.planning_ns += elapsed

    def examine(self, count=1):
        self.rows_examined += count

    def examine_index(self, count=1):
        self.index_entries_examined += count

    def to_dict(self):
        # 用执行前后缓存计数器的差值，得到当前语句实际产生的 I/O 指标。
        elapsed_ns = time.perf_counter_ns() - self.started_ns
        def delta(name):
            return self.buffer_after.get(name, 0) - self.buffer_before.get(name, 0)
        return {
            "planning_time_ms": round(self.planning_ns / 1_000_000, 3),
            "execution_time_ms": round(max(0, elapsed_ns - self.planning_ns) / 1_000_000, 3),
            "total_time_ms": round(elapsed_ns / 1_000_000, 3),
            "rows_examined": self.rows_examined,
            "rows_returned": self.rows_returned,
            "index_entries_examined": self.index_entries_examined,
            "buffer_hits": delta("hits"),
            "buffer_misses": delta("misses"),
            "pages_read": delta("disk_reads"),
            "pages_written": delta("disk_writes"),
            "evictions": delta("evictions"),
        }
