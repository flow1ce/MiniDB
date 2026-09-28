import copy
from .plan import Plan

def optimize(p):
    """规则优化入口：深拷贝原计划后做常量折叠、布尔化简和列需求标注。"""
    p=copy.deepcopy(p)
    def fold(x):
        # 递归折叠计划参数中的常量表达式，并化简 AND/OR 恒真恒假项。
        if isinstance(x,dict):
            for k,v in list(x.items()): x[k]=fold(v)
            if x.get('node')=='Binary' and isinstance(x.get('left'),dict) and isinstance(x.get('right'),dict):
                a,b=x['left'],x['right']
                op=x.get('op')
                if a.get('node')=='Literal' and b.get('node')=='Literal':
                    try:
                        av,bv=a.get('value'),b.get('value')
                        if op in ('AND','OR'):
                            value=(bool(av) and bool(bv)) if op=='AND' else (bool(av) or bool(bv))
                        else: value={'=':av==bv,'==':av==bv,'!=':av!=bv,'<>':av!=bv,'>':av>bv,'>=':av>=bv,'<':av<bv,'<=':av<=bv,'+':av+bv,'-':av-bv,'*':av*bv,'/':av/bv,'%':av%bv}[op]
                        return {'node':'Literal','value':value,'dtype':'BOOL' if op in ('AND','OR','=','==','!=','<>','>','>=','<','<=') else None}
                    except Exception: pass
                if op=='AND':
                    if a.get('node')=='Literal' and a.get('value') is True: return b
                    if b.get('node')=='Literal' and b.get('value') is True: return a
                    if a.get('node')=='Literal' and a.get('value') is False: return a
                    if b.get('node')=='Literal' and b.get('value') is False: return b
                if op=='OR':
                    if a.get('node')=='Literal' and a.get('value') is False: return b
                    if b.get('node')=='Literal' and b.get('value') is False: return a
                    if a.get('node')=='Literal' and a.get('value') is True: return a
                    if b.get('node')=='Literal' and b.get('value') is True: return b
        elif isinstance(x,list): return [fold(v) for v in x]
        return x
    def rec(n):
        # 自底向上优化计划树：删除恒真 Filter，并把所需列标注到扫描节点。
        n.children=[rec(c) for c in n.children]; n.args=fold(n.args)
        if n.op=='Filter' and n.args.get('expr',{}).get('node')=='Literal' and n.args['expr'].get('value') is True: return n.children[0]
        if n.op=='Project' and n.children:
            needed=set()
            def collect(x):
                if isinstance(x,dict):
                    if x.get('node')=='Identifier': needed.add(x.get('name'))
                    for v in x.values(): collect(v)
                elif isinstance(x,list):
                    for v in x: collect(v)
            collect(n.args.get('columns',[]))
            child=n.children[0]
            if child.op=='Filter': collect(child.args.get('expr',{})); child=child.children[0] if child.children else child
            if child.op in ('SeqScan','IndexScan') and needed: child.args['columns']=sorted(needed)
        return n
    return rec(p)
