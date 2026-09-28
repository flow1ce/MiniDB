import argparse
import json

from common.errors import DBError
from engine.database import Database
from .render import render_result


def main():
    """命令行入口：解析启动参数，并支持一次性 SQL 与交互式 MiniDB 提示符。"""
    parser = argparse.ArgumentParser(description="MiniDBMS")
    parser.add_argument("--data", default="data")
    parser.add_argument("--buffer-size", type=int, default=16)
    parser.add_argument("--policy", choices=["LRU", "FIFO"], default="LRU")
    parser.add_argument("--sql")
    parser.add_argument("--debug", action="store_true", help="show Token -> AST -> Semantic -> Plan pipeline")
    parser.add_argument("--json", action="store_true", help="print the structured result as JSON")
    args = parser.parse_args()

    with Database(args.data, args.buffer_size, args.policy) as db:
        if args.sql:
            # 一次性模式：可先展示编译流水线，再执行并输出表格或 JSON。
            try:
                if args.debug:
                    print(json.dumps(db.compile(args.sql), ensure_ascii=False, indent=2))
                result=db.execute_structured(args.sql)
                print(json.dumps(result,ensure_ascii=False,indent=2) if args.json else render_result(result))
            except DBError as exc:
                print(str(exc))
            return

        print("MiniDB ready. Type SQL, .stats, .debug <SQL>, or quit.")
        # 交互模式：循环处理 SQL、缓存统计、编译调试和退出命令。
        while True:
            try:
                query = input("MiniDB > ")
                if query.strip().lower() in ("quit", "exit"):
                    break
                if query.strip() == ".stats":
                    print(render_result({'success': True, 'type': 'stats', 'stats': db.buffer.stats()}))
                    continue
                if query.strip().lower().startswith(".debug "):
                    print(json.dumps(db.compile(query.strip()[7:]), ensure_ascii=False, indent=2))
                    continue
                print(render_result(db.execute_structured(query)))
            except DBError as exc:
                print(str(exc))
            except (EOFError, KeyboardInterrupt):
                break

if __name__ == '__main__':
    main()
