"""Stable row codec boundary. Rows are JSON-compatible dictionaries."""
import json
# 行编码边界：把字典行与 UTF-8 JSON 字节相互转换。
def encode_row(row): return json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf8')
def decode_row(data): return json.loads(data.decode('utf8'))
__all__ = ['encode_row','decode_row']
