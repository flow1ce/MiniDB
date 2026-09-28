from dataclasses import dataclass, field, asdict
from typing import Any

# AST 是 Parser 的输出：表达“SQL 写了什么”，尚未决定具体访问路径。

def _ast_value(value):
    """递归把 AST 节点转换为可展示、可 JSON 序列化的字典。"""
    if isinstance(value, Node):
        return value.to_dict()
    if isinstance(value, dict):
        return {key: _ast_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_ast_value(item) for item in value]
    return value

@dataclass
class Node:
    """所有 AST 节点的基类；行列号用于把错误精确定位回 SQL 原文。"""
    line:int=1; column:int=1
    def to_dict(self):
        d={'node':self.__class__.__name__}
        for k,v in self.__dict__.items(): d[k]=_ast_value(v)
        return d

@dataclass
class Expr(Node):
    """表达式节点基类，例如常量、列名、一元运算和二元运算。"""
    pass

@dataclass
class Literal(Expr): value:Any=None; dtype:str='NULL'

@dataclass
class Identifier(Expr): name:str=''

@dataclass
class Unary(Expr): op:str=''; expr:Expr=None

@dataclass
class Binary(Expr): left:Expr=None; op:str=''; right:Expr=None

@dataclass
class Star(Expr): pass

@dataclass
class Statement(Node):
    """SQL 语句节点基类，下面各数据类分别承载不同语句的结构。"""
    pass

@dataclass
class CreateTable(Statement): name:str=''; columns:list=field(default_factory=list)

@dataclass
class Insert(Statement): table:str=''; columns:list=field(default_factory=list); values:list=field(default_factory=list)

@dataclass
class Select(Statement): columns:list=field(default_factory=list); table:str=''; where:Expr=None; joins:list=field(default_factory=list); order:list=field(default_factory=list); group:list=field(default_factory=list); limit:int=None; offset:int=0; distinct:bool=False

@dataclass
class Delete(Statement): table:str=''; where:Expr=None

@dataclass
class Update(Statement): table:str=''; assignments:dict=field(default_factory=dict); where:Expr=None

@dataclass
class Explain(Statement): inner:Statement=None; analyze:bool=False

@dataclass
class Txn(Statement): action:str=''; isolation:str='READ_COMMITTED'

@dataclass
class CreateIndex(Statement):
    name:str=''; table:str=''; index_column:str=''; unique:bool=False

@dataclass
class DropTable(Statement): name:str=''

@dataclass
class DropIndex(Statement): name:str=''

@dataclass
class CreateUser(Statement): name:str=''

@dataclass
class Grant(Statement): privilege:str=''; table:str=''; user:str=''

@dataclass
class Revoke(Statement): privilege:str=''; table:str=''; user:str=''

@dataclass
class ShowTables(Statement): pass

@dataclass
class Describe(Statement): name:str=''
