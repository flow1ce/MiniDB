from .ast import *
from .plan import Plan
from common.errors import SemanticError

def plan_for(s):
    """把 AST 转为基础逻辑计划；SELECT 计划按数据流方向由下向上搭建。"""
    if isinstance(s,ShowTables): return Plan('ShowTables')
    if isinstance(s,Describe): return Plan('Describe',{'table':s.name})
    if isinstance(s,CreateUser): return Plan('CreateUser',{'user':s.name})
    if isinstance(s,Grant): return Plan('Grant',{'privilege':s.privilege,'table':s.table,'user':s.user})
    if isinstance(s,Revoke): return Plan('Revoke',{'privilege':s.privilege,'table':s.table,'user':s.user})
    if isinstance(s,DropTable): return Plan('DropTable',{'table':s.name})
    if isinstance(s,DropIndex): return Plan('DropIndex',{'index':s.name})
    if isinstance(s,CreateIndex): return Plan('CreateIndex',{'name':s.name,'table':s.table,'column':s.index_column,'unique':s.unique})
    if isinstance(s,CreateTable): return Plan('CreateTable',{'table':s.name,'columns':s.columns})
    if isinstance(s,Insert): return Plan('Insert',{'table':s.table,'columns':s.columns,'values':[x.to_dict() for x in s.values]})
    if isinstance(s,Delete): return Plan('Delete',{'table':s.table,'where':s.where.to_dict() if s.where else None},[Plan('SeqScan',{'table':s.table})])
    if isinstance(s,Update): return Plan('Update',{'table':s.table,'assignments':{k:v.to_dict() for k,v in s.assignments.items()},'where':s.where.to_dict() if s.where else None},[Plan('SeqScan',{'table':s.table})])
    if isinstance(s,Explain): return Plan('Explain',{'analyze':s.analyze},[plan_for(s.inner)])
    if isinstance(s,Txn): return Plan(s.action,{'isolation':s.isolation})
    if isinstance(s,Select):
        p=Plan('SeqScan',{'table':s.table})
        for jt,cond in s.joins: p=Plan('NestedLoopJoin',{'table':jt,'on':cond.to_dict()},[p,Plan('SeqScan',{'table':jt})])
        if s.where: p=Plan('Filter',{'expr':s.where.to_dict()},[p])
        if s.group: p=Plan('GroupBy',{'keys':[x.to_dict() for x in s.group]},[p])
        p=Plan('Project',{'columns':[x.to_dict() for x in s.columns],'distinct':s.distinct},[p])
        if s.order: p=Plan('OrderBy',{'keys':[(x.to_dict(),d) for x,d in s.order]},[p])
        if s.offset: p=Plan('Offset',{'offset':s.offset},[p])
        if s.limit is not None: p=Plan('Limit',{'limit':s.limit},[p])
        return p
    raise SemanticError('unsupported statement')

def build_plan(statement):
    """AST → 逻辑计划的稳定公开接口。"""
    return plan_for(statement)

def _index_candidate(statement, catalog):
    """识别“索引列 与 常量简单比较”的条件，返回可用索引候选。"""
    if not isinstance(statement, (Select,Update,Delete)) or getattr(statement,'joins',[]) or not statement.where: return None
    e=statement.where
    if not isinstance(e,Binary) or e.op not in ('=','==','<','<=','>','>='): return None
    if isinstance(e.left,Identifier) and isinstance(e.right,Literal): col,val=e.left.name,e.right.value
    elif isinstance(e.right,Identifier) and isinstance(e.left,Literal): col,val=e.right.name,e.left.value
    else: return None
    t=catalog.get(statement.table)
    for name,meta in t.indexes.items():
        if meta.get('column','').lower()==col.lower(): return name,meta,e.op,val
    return None

def choose_plan(statement,catalog):
    """选择访问路径：可用时把顺序扫描替换成等值或范围索引扫描。"""
    p=plan_for(statement); candidate=_index_candidate(statement,catalog)
    if candidate and isinstance(p,Plan):
        child=p.children[0] if p.children else None
        if p.op in ('Update','Delete') and child and child.op=='SeqScan' and candidate[2] in ('=','=='):
            name,meta,op,val=candidate; args={'table':statement.table,'index':name,'key':val}
            p.children[0]=Plan('IndexScan',args); return p
        if p.op!='Project': return p
        if child.op=='Filter':
            name,meta,op,val=candidate
            args={'table':statement.table,'index':name}
            if op in ('=','=='): args['key']=val
            elif op in ('<','<='): args['high']=val
            else: args['low']=val
            p.children[0]=Plan('Filter',child.args,[Plan('IndexScan',args)])
    return p
