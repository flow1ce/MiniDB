from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any
import json


def _json_value(value: Any) -> Any:
    """把 Plan 参数（包括 AST 节点）递归转换为 JSON 安全值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    to_dict = getattr(value, 'to_dict', None)
    if callable(to_dict):
        return _json_value(to_dict())
    if is_dataclass(value):
        return _json_value(asdict(value))
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')

@dataclass
class Plan:
    """执行计划树节点：op 是算子名，args 是参数，children 是输入子计划。"""
    op:str; args:dict=field(default_factory=dict); children:list=field(default_factory=list)
    def to_dict(self): return {'op':self.op,'args':_json_value(self.args),'children':[c.to_dict() for c in self.children]}
    def text(self,indent=0):
        # 用缩进文本展示计划树，供 EXPLAIN、CLI 和 Web 页面查看。
        args = _json_value(self.args)
        return ' '*indent+self.op+((' '+json.dumps(args,ensure_ascii=False)) if args else '')+''.join('\n'+c.text(indent+2) for c in self.children)
