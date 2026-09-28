"""Local HTTP API and browser UI for MiniDBMS."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import statistics
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from engine.database import Database
from sql_compiler import Lexer, Parser
from common.errors import DBError

HTML = Path(__file__).with_name("index.html").read_text(encoding="utf8")
WEB_DIR = Path(__file__).resolve().parent


class SessionRegistry:
    """浏览器标签页会话表；底层存储组件由所有会话共享。"""
    def __init__(self, database):
        self.database=database; self._lock=threading.RLock(); self._sessions={}
    def create(self, client_id=None):
        client_id=str(client_id or uuid.uuid4().hex)
        with self._lock:
            item=self._sessions.get(client_id)
            if item is None:
                item={'db':self.database.create_session(client_id),'created':time.time(),'last_seen':time.time()}
                self._sessions[client_id]=item
            item['last_seen']=time.time()
            return client_id,item['db']
    def get(self, client_id):
        with self._lock:
            item=self._sessions.get(str(client_id))
            if item: item['last_seen']=time.time(); return item['db']
            return None
    def close(self, client_id):
        with self._lock: item=self._sessions.pop(str(client_id),None)
        if item: item['db'].close(); return True
        return False
    def close_all(self):
        with self._lock:
            sessions=list(self._sessions.values()); self._sessions.clear()
        for item in sessions: item['db'].close()
    def snapshot(self):
        with self._lock:
            sessions=[]
            for client_id,item in self._sessions.items():
                txn=item['db'].transaction
                sessions.append({'session_id':client_id,'transaction':None if txn is None else {
                    'id':txn.txn_id,'state':txn.state.value,'isolation':txn.isolation.value,
                    'locks':len(txn.locks)},'lock_timeout':item['db'].lock_timeout})
            return sessions


def validate_cache_behavior(db, sql):
    """冷热缓存验证入口：限制为 SELECT，并汇总单条或多条语句的验证结果。"""
    statements = Parser(Lexer(sql).tokens()).parse()
    if not statements or any(statement.__class__.__name__ != "Select" for statement in statements):
        raise DBError("Semantic", "Cache validation only accepts SELECT statements")
    if len(statements) == 1:
        return _validate_single_cache_behavior(db, sql)
    statement_sql = [part.strip() + ";" for part in sql.split(";") if part.strip()]
    if len(statement_sql) != len(statements):
        raise DBError("Syntax", "Unable to split SELECT statements safely")
    results = []
    for index, statement in enumerate(statement_sql, 1):
        result = _validate_single_cache_behavior(db, statement)
        result["statement_index"] = index
        result["sql"] = statement
        results.append(result)
    checks = {
        "all_verified": all(item.get("verified", False) for item in results),
        "same_result": all(item.get("checks", {}).get("same_result", False) for item in results),
        "cold_read_from_disk": any(item.get("checks", {}).get("cold_read_from_disk", False) for item in results),
        "warm_hit_cache": all(item.get("checks", {}).get("warm_hit_cache", False) for item in results),
        "warm_avoided_disk": all(item.get("checks", {}).get("warm_avoided_disk", False) for item in results),
    }
    return {
        "success": all(item.get("success", False) for item in results),
        "type": "cache_validation_batch",
        "results": results,
        "checks": checks,
        "verified": checks["all_verified"],
        "statement_count": len(results),
        "verified_count": sum(1 for item in results if item.get("verified", False)),
        "stats": db.buffer.stats(),
    }


def _validate_single_cache_behavior(db, sql):
    """同一 SELECT 先冷后热各执行一次，核对磁盘读取、缓存命中和结果一致性。"""
    with db.lock:
        db.buffer.clear()
        db.buffer.reset_stats()
        cold = db.execute_structured(sql)
        if not cold.get("success", False):
            return cold
        warm = db.execute_structured(sql)
        if not warm.get("success", False):
            return warm
        cold_metrics = cold.get("metrics", {})
        warm_metrics = warm.get("metrics", {})
        checks = {
            "cold_read_from_disk": cold_metrics.get("pages_read", 0) > 0,
            "warm_hit_cache": warm_metrics.get("buffer_hits", 0) > 0,
            "warm_avoided_disk": warm_metrics.get("pages_read", 0) == 0,
            "same_result": cold.get("rows") == warm.get("rows"),
        }
        return {
            "success": cold.get("success", False) and warm.get("success", False),
            "type": "cache_validation", "cold": cold, "warm": warm,
            "checks": checks, "verified": all(checks.values()),
            "stats": db.buffer.stats(),
        }


class Handler(BaseHTTPRequestHandler):
    """后端 HTTP API 控制器：提供静态页面、SQL 执行、计划和性能观测接口。"""
    db = None

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        # 统一编码响应体并添加内容类型、长度和跨域响应头。
        if isinstance(payload, bytes):
            data = payload
        elif isinstance(payload, str):
            data = payload.encode("utf8")
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        # CORS 预检接口：声明前端允许使用的请求头与 HTTP 方法。
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-MiniDB-Session")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            # GET /：返回 MiniDBMS 浏览器控制台首页。
            return self._send(200, HTML, "text/html; charset=utf-8")
        if path == "/api/session":
            client_id=self.headers.get('X-MiniDB-Session')
            if client_id and self.registry.get(client_id):
                db=self.registry.get(client_id)
                return self._send(200, {'success':True,'session_id':client_id,'transaction':self._transaction_payload(db)})
            return self._send(400, {'success':False,'error':'Session is required'})
        db=self._session_db()
        if path == "/api/concurrency":
            snapshot=self.registry.database.lock_manager.snapshot()
            return self._send(200, {'success':True,'session_id':db.session_id,'sessions':self.registry.snapshot(),**snapshot})
        if path == "/api/tables":
            # GET /api/tables：返回表、索引以及当前事务状态。
            tables = sorted(db.catalog.tables)
            indexes = []
            for table in db.catalog.tables.values():
                for name, meta in table.indexes.items():
                    indexes.append({"name": name, "table": table.name, "column": meta.get("column", "")})
            return self._send(200, {"success": True, "tables": tables, "indexes": indexes, "transaction": self._transaction_payload(db), 'session_id':db.session_id})
        if path == "/api/stats":
            # GET /api/stats：返回 Buffer Pool 容量、命中率和磁盘 I/O 指标。
            return self._send(200, {"success": True, "type": "stats", "stats": db.buffer.stats()})
        # 其余 GET 请求作为 CSS/JS/HTML 等静态资源读取，并阻止目录越界访问。
        file_path = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR not in file_path.parents or not file_path.is_file():
            return self._send(404, {"success": False, "error": "Not found"})
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        return self._send(200, file_path.read_bytes(), content_type)

    def do_POST(self):
        """POST API 总路由：读取 JSON 请求体，并将各接口分派到数据库能力。"""
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == '/api/session':
                client_id,db=self.registry.create(body.get('client_id'))
                return self._send(200, {'success':True,'session_id':client_id,'transaction':self._transaction_payload(db),'lock_timeout':db.lock_timeout})
            if path == '/api/session/close':
                self.registry.close(body.get('session_id') or self.headers.get('X-MiniDB-Session'))
                return self._send(200, {'success':True})
            db=self._session_db()
            if path == '/api/session/settings':
                timeout=max(1.0,min(120.0,float(body.get('lock_timeout',30))))
                db.lock_timeout=timeout
                return self._send(200, {'success':True,'session_id':db.session_id,'lock_timeout':timeout})
            if path == "/api/flush":
                # POST /api/flush：将 Buffer Pool 脏页刷盘并返回刷新后的统计。
                self.registry.database.buffer.flush_all()
                return self._send(200, {"success": True, "type": "stats", "stats": self.registry.database.buffer.stats()})
            sql = str(body.get("sql", "")).strip()
            if not sql:
                return self._send(400, {"success": False, "error": "SQL is required"})
            if path == "/api/execute":
                # POST /api/execute：执行 SQL，返回统一的查询/命令/错误结构。
                return self._send(200, db.execute_structured(sql))
            if path == "/api/explain":
                # POST /api/explain：只编译 SQL，查看 Token、AST 和优化前后计划。
                compiled = db.compile(sql)
                return self._send(200, {"success": True, "type": "explain", "pipeline": compiled})
            if path == "/api/cache-test":
                # POST /api/cache-test：验证同一查询的冷读盘、热命中和结果一致性。
                return self._send(200, validate_cache_behavior(db, sql))
            if path == "/api/analyze":
                # POST /api/analyze：冷缓存下各跑 5 次，比较优化前后中位耗时与结果。
                statements = Parser(Lexer(sql).tokens()).parse()
                if len(statements) != 1 or statements[0].__class__.__name__ != "Select":
                    return self._send(400, {"success": False, "error": "Analyze only accepts one SELECT"})
                compiled = db.compile(sql)
                baseline_runs=[]; optimized_runs=[]
                for _ in range(5):
                    db.buffer.clear()
                    baseline_runs.append(db.execute_structured(sql, optimize_enabled=False))
                    db.buffer.clear()
                    optimized_runs.append(db.execute_structured(sql, optimize_enabled=True))
                baseline=baseline_runs[-1]; optimized=optimized_runs[-1]
                for result,runs in ((baseline,baseline_runs),(optimized,optimized_runs)):
                    result['metrics']['total_time_ms']=round(statistics.median(x['metrics']['total_time_ms'] for x in runs),3)
                    result['metrics']['execution_time_ms']=round(statistics.median(x['metrics']['execution_time_ms'] for x in runs),3)
                    result['metrics']['runs']=len(runs)
                return self._send(200, {
                    "success": True, "type": "comparison", "pipeline": compiled,
                    "optimized": optimized, "baseline": baseline,
                    "same_result": optimized.get("rows") == baseline.get("rows"), "cache_mode":"cold", "runs":5,
                })
            return self._send(404, {"success": False, "error": "Not found"})
        except DBError as exc:
            # 可预期的数据库错误以 400 返回，并保留阶段与 SQL 行列位置。
            return self._send(400, {"success": False, "type": "error", "error": {
                "stage": exc.stage, "message": exc.message, "line": exc.line, "column": exc.column
            }})
        except Exception as exc:
            # 未分类异常以 500 返回，标记为 Server 阶段错误。
            return self._send(500, {"success": False, "type": "error", "error": {"stage": "Server", "message": str(exc)}})

    def log_message(self, fmt, *args):
        # 关闭 BaseHTTPRequestHandler 默认访问日志，保持演示终端简洁。
        return

    def _session_db(self):
        client_id=self.headers.get('X-MiniDB-Session')
        if not client_id: return self.registry.database
        db=self.registry.get(client_id)
        if db is None: _client_id,db=self.registry.create(client_id)
        return db

    @staticmethod
    def _transaction_payload(db):
        transaction=db.transaction
        return None if transaction is None else {'id':transaction.txn_id,'state':transaction.state.value,'isolation':transaction.isolation.value,'locks':len(transaction.locks)}


def run(data_dir="data", port=8080):
    """Web 服务入口：创建共享 Database，启动本机线程服务器并负责优雅关闭。"""
    Handler.db = Database(data_dir)
    Handler.registry = SessionRegistry(Handler.db)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"MiniDBMS Web UI: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.registry.close_all(); Handler.db.close()
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MiniDBMS local Web UI")
    parser.add_argument("--data", default="data")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    run(args.data, args.port)
