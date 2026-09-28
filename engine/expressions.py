from collections import OrderedDict
from sql_compiler.ast import *
from common.errors import SemanticError

def eval_expr(e,row,env=None):
    """递归计算一棵表达式 AST，包含列查找、算术比较和 SQL 三值逻辑。"""
    env=env or row
    if e is None: return True
    if isinstance(e,Literal): return e.value
    if isinstance(e,Identifier):
        # 先精确匹配，再忽略大小写，最后尝试匹配 table.column 限定名。
        if e.name in env: return env[e.name]
        for k,v in env.items():
            if k.lower()==e.name.lower(): return v
        for k,v in env.items():
            if k.lower().endswith('.'+e.name.lower()): return v
        return None
    if isinstance(e,Star): return row
    if isinstance(e,Unary):
        v=eval_expr(e.expr,row,env)
        if e.op=='NOT': return None if v is None else not bool(v)
        if e.op=='-': return -v
        if e.op in ('COUNT','SUM','AVG','MIN','MAX'): return v
    if isinstance(e,Binary):
        # NULL 参与普通运算返回 UNKNOWN；AND/OR 按 SQL 三值真值表处理。
        a,b=eval_expr(e.left,row,env),eval_expr(e.right,row,env); op=e.op
        if op=='IS NULL': return a is None
        if op=='IS NOT NULL': return a is not None
        if op=='AND':
            if a is False or b is False: return False
            if a is None or b is None: return None
            return True
        if op=='OR':
            if a is True or b is True: return True
            if a is None or b is None: return None
            return False
        if a is None or b is None: return None
        try:
            return {'=':lambda:a==b,'==':lambda:a==b,'!=':lambda:a!=b,'<>':lambda:a!=b,'>':lambda:a>b,'>=':lambda:a>=b,'<':lambda:a<b,'<=':lambda:a<=b,'+':lambda:a+b,'-':lambda:a-b,'*':lambda:a*b,'/':lambda:a/b,'%':lambda:a%b}[op]()
        except Exception as ex: raise SemanticError(f"operator '{op}' failed for {a!r} and {b!r}",e.line,e.column)
    return None
