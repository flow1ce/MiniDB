from .ast import *
from common.errors import SemanticError

def _table(catalog, name, statement):
    """从 Catalog 查表，并把不存在错误定位到当前 SQL 语句。"""
    try:
        return catalog.get(name)
    except SemanticError as exc:
        raise SemanticError(exc.message, statement.line, statement.column) from exc

def _column_type(columns,name):
    """解析普通/限定列名，返回类型；多表同名列未限定时报告歧义。"""
    needle=name.lower()
    matches=[]
    for c in columns:
        cn=c['name'].lower()
        qualified=f"{c.get('table','')}.{cn}".lower() if c.get('table') else cn
        if needle in (cn,qualified) or needle.endswith('.'+cn): matches.append(c)
    if not matches: return None
    if len(matches)>1 and '.' not in needle: raise SemanticError(f"column '{name}' is ambiguous")
    return matches[0].get('type')

def _expr_type(expr,columns,allow_star=False):
    """递归推导表达式类型，并检查运算符两侧类型是否合法。"""
    if expr is None: return 'BOOL'
    if isinstance(expr,Literal): return expr.dtype
    if isinstance(expr,Star):
        if allow_star: return 'STAR'
        raise SemanticError('star is only valid in SELECT projection or COUNT(*)',expr.line,expr.column)
    if isinstance(expr,Identifier):
        typ=_column_type(columns,expr.name)
        if typ is None: raise SemanticError(f"column '{expr.name}' does not exist",expr.line,expr.column)
        return typ
    if isinstance(expr,Unary):
        typ=_expr_type(expr.expr,columns,expr.op in ('COUNT','SUM','AVG','MIN','MAX'))
        if expr.op=='NOT':
            if typ not in ('BOOL','NULL'): raise SemanticError('NOT requires a BOOL expression',expr.line,expr.column)
            return 'BOOL'
        if expr.op=='-':
            if typ not in ('INT','FLOAT','NULL'): raise SemanticError('unary - requires a numeric expression',expr.line,expr.column)
            return typ
        if expr.op=='COUNT': return 'INT'
        if expr.op in ('SUM','AVG','MIN','MAX'): return typ
    if isinstance(expr,Binary):
        lt,rt=_expr_type(expr.left,columns),_expr_type(expr.right,columns); op=expr.op
        if op in ('AND','OR'):
            if lt not in ('BOOL','NULL') or rt not in ('BOOL','NULL'): raise SemanticError(f'{op} requires BOOL operands',expr.line,expr.column)
            return 'BOOL'
        if op in ('IS NULL','IS NOT NULL'): return 'BOOL'
        if op in ('=','==','!=','<>','>','>=','<','<='):
            numeric={lt,rt}<={'INT','FLOAT','NULL'}
            if not numeric and lt!=rt and 'NULL' not in (lt,rt): raise SemanticError(f"cannot compare {lt} and {rt}",expr.line,expr.column)
            return 'BOOL'
        if op in ('+','-','*','/','%'):
            if lt not in ('INT','FLOAT','NULL') or rt not in ('INT','FLOAT','NULL'): raise SemanticError(f"operator '{op}' requires numeric operands",expr.line,expr.column)
            return 'FLOAT' if 'FLOAT' in (lt,rt) or op=='/' else 'INT'
    raise SemanticError('unsupported expression',getattr(expr,'line',1),getattr(expr,'column',1))

def validate_statement(statement,catalog):
    """计划生成前的语义总入口：检查表、列、类型、值数量和表达式。"""
    if isinstance(statement,Explain): return validate_statement(statement.inner,catalog)
    if isinstance(statement,CreateTable):
        if statement.name.lower() in catalog.tables: raise SemanticError(f"table '{statement.name}' already exists",statement.line,statement.column)
        return True
    if isinstance(statement,CreateIndex):
        t=_table(catalog,statement.table,statement); _column_type([dict(c,table=t.name) for c in t.columns],statement.index_column); return True
    if isinstance(statement,(DropTable,DropIndex,CreateUser,Grant,Revoke,ShowTables,Describe)): return True
    if isinstance(statement,Txn): return True
    if isinstance(statement,(Insert,Select,Delete,Update)):
        t=_table(catalog,statement.table,statement); cols=[dict(c,table=t.name) for c in t.columns]
        if isinstance(statement,Insert):
            names=statement.columns or [c['name'] for c in t.columns]
            if len(names)!=len(statement.values): raise SemanticError('INSERT column/value count mismatch',statement.line,statement.column)
            for n,v in zip(names,statement.values):
                typ=_column_type(cols,n)
                if typ is None: raise SemanticError(f"column '{n}' does not exist",statement.line,statement.column)
                actual=_expr_type(v,[],allow_star=False)
                if actual not in (typ,'NULL') and not ({actual,typ}<={'INT','FLOAT'}): raise SemanticError(f"column '{n}' expects {typ}, but {actual} found",v.line,v.column)
            return True
        allcols=list(cols)
        if isinstance(statement,Select):
            for jt,_ in statement.joins:
                ot=_table(catalog,jt,statement); allcols.extend(dict(c,table=ot.name) for c in ot.columns)
            for e in statement.columns: _expr_type(e,allcols,allow_star=True)
            for e,_ in statement.order: _expr_type(e,allcols)
            for e in statement.group: _expr_type(e,allcols)
            if statement.where: _expr_type(statement.where,allcols)
            return True
        if statement.where: _expr_type(statement.where,allcols)
        if isinstance(statement,Update):
            for n,e in statement.assignments.items():
                typ=_column_type(cols,n)
                if typ is None: raise SemanticError(f"column '{n}' does not exist",statement.line,statement.column)
                actual=_expr_type(e,cols)
                if actual not in (typ,'NULL') and not ({actual,typ}<={'INT','FLOAT'}): raise SemanticError(f"column '{n}' expects {typ}, but {actual} found",e.line,e.column)
        return True
    raise SemanticError('unsupported statement')
