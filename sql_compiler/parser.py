from .ast import *
from .token import Token
from common.errors import DBError, SyntaxError_

class Parser:
    """递归下降语法分析器：消费 Token，并构造对应的语句与表达式 AST。"""
    def __init__(self,tokens): self.t=tokens; self.i=0
    def cur(self): return self.t[self.i]
    def eat(self,k=None):
        # 消费当前 Token；指定 k 时同时检查它是否符合文法预期。
        x=self.cur()
        if k and x.type!=k: raise SyntaxError_(f"expected {k}, got {x.lexeme or x.type}",x.line,x.column)
        self.i+=1; return x
    def accept(self,k):
        if self.cur().type==k: return self.eat()
        return None
    def parse(self,recover=False):
        """解析一条或多条 SQL；恢复模式下遇错跳到下一个分号继续。"""
        stmts=[]
        while self.cur().type!='EOF':
            try: stmts.append(self.statement())
            except DBError:
                if not recover: raise
                while self.cur().type not in (';','EOF'): self.i+=1
            self.accept(';')
        return stmts
    def parse_expression(self):
        """Parse one expression; kept as an explicit答辩/集成接口."""
        e=self.expr()
        if self.cur().type!='EOF':
            t=self.cur(); raise SyntaxError_('unexpected token after expression',t.line,t.column)
        return e
    def statement(self):
        # 根据首个关键字分派到 CREATE、SELECT、事务等专用解析函数。
        c=self.cur()
        if c.type=='CREATE': return self.create()
        if c.type=='INSERT': return self.insert()
        if c.type=='SELECT': return self.select()
        if c.type=='DELETE': return self.delete()
        if c.type=='UPDATE': return self.update()
        if c.type=='DROP': return self.drop()
        if c.type=='CREATE': return self.create()
        if c.type=='GRANT': return self.grant()
        if c.type=='REVOKE': return self.revoke()
        if c.type=='SHOW':
            s=self.eat(); self.eat('TABLES'); return ShowTables(line=s.line,column=s.column)
        if c.type in ('DESCRIBE','DESC'):
            s=self.eat(); return Describe(name=self.eat('IDENT').lexeme,line=s.line,column=s.column)
        if c.type=='EXPLAIN':
            self.eat(); analyze=bool(self.accept('ANALYZE'))
            return Explain(inner=self.statement(),analyze=analyze,line=c.line,column=c.column)
        if c.type in ('BEGIN','COMMIT','ROLLBACK'):
            self.eat(); iso='READ_COMMITTED'
            if c.type=='BEGIN' and self.accept('ISOLATION'):
                self.accept('LEVEL'); tok=self.eat(); iso=tok.type
            return Txn(action=c.type,isolation=iso,line=c.line,column=c.column)
        raise SyntaxError_('expected SQL statement',c.line,c.column)
    def create(self):
        """解析建表、建索引或创建用户，并收集列约束/索引属性。"""
        s=self.eat('CREATE')
        unique=bool(self.accept('UNIQUE'))
        if self.accept('USER'): return CreateUser(name=self.eat('IDENT').lexeme,line=s.line,column=s.column)
        if self.accept('INDEX'):
            name=self.eat('IDENT').lexeme; self.eat('ON'); table=self.eat('IDENT').lexeme; self.eat('('); col=self.eat('IDENT').lexeme; self.eat(')')
            return CreateIndex(name=name,table=table,index_column=col,unique=unique,line=s.line,column=s.column)
        self.eat('TABLE'); name=self.eat('IDENT').lexeme; self.eat('('); cols=[]
        while True:
            if self.accept('PRIMARY'):
                self.eat('KEY'); self.eat('('); pk=self.eat('IDENT').lexeme; self.eat(')')
                matches=[c for c in cols if c['name'].lower()==pk.lower()]
                if not matches: raise SyntaxError_(f"primary key column '{pk}' is not defined",self.cur().line,self.cur().column)
                if any(c.get('primary_key') for c in cols): raise SyntaxError_('only one PRIMARY KEY is allowed',self.cur().line,self.cur().column)
                matches[0]['primary_key']=True; matches[0]['not_null']=True
                if not self.accept(','): break
                continue
            if self.accept('UNIQUE'):
                self.eat('('); uq=self.eat('IDENT').lexeme; self.eat(')')
                matches=[c for c in cols if c['name'].lower()==uq.lower()]
                if not matches: raise SyntaxError_(f"unique column '{uq}' is not defined",self.cur().line,self.cur().column)
                matches[0]['unique']=True
                if not self.accept(','): break
                continue
            n=self.eat('IDENT').lexeme; typ=self.eat().type
            if typ not in ('INT','FLOAT','VARCHAR','BOOL'): raise SyntaxError_(f'unsupported type {typ}',self.cur().line,self.cur().column)
            primary=bool(self.accept('PRIMARY'))
            if primary: self.eat('KEY')
            unique=bool(self.accept('UNIQUE'))
            not_null=False
            if self.accept('NOT'):
                self.eat('NULL'); not_null=True
            cols.append({'name':n,'type':typ,'primary_key':primary,'unique':unique,'not_null':not_null or primary})
            if not self.accept(','): break
        self.eat(')')
        if sum(1 for c in cols if c.get('primary_key'))>1: raise SyntaxError_('only one PRIMARY KEY is allowed',s.line,s.column)
        return CreateTable(name=name,columns=cols,line=s.line,column=s.column)
    def drop(self):
        s=self.eat('DROP')
        if self.accept('TABLE'): return DropTable(name=self.eat('IDENT').lexeme,line=s.line,column=s.column)
        self.eat('INDEX'); return DropIndex(name=self.eat('IDENT').lexeme,line=s.line,column=s.column)
    def grant(self):
        s=self.eat('GRANT'); privilege=self.eat().type; self.eat('ON'); table=self.eat('IDENT').lexeme; self.eat('TO'); user=self.eat('IDENT').lexeme
        return Grant(privilege=privilege,table=table,user=user,line=s.line,column=s.column)
    def revoke(self):
        s=self.eat('REVOKE'); privilege=self.eat().type; self.eat('ON'); table=self.eat('IDENT').lexeme; self.eat('FROM'); user=self.eat('IDENT').lexeme
        return Revoke(privilege=privilege,table=table,user=user,line=s.line,column=s.column)
    def insert(self):
        """解析 INSERT 的目标表、可选列清单和 VALUES 表达式。"""
        s=self.eat('INSERT'); self.eat('INTO'); table=self.eat('IDENT').lexeme; cols=[]
        if self.accept('('):
            while True:
                cols.append(self.eat('IDENT').lexeme)
                if not self.accept(','): break
            self.eat(')')
        self.eat('VALUES'); self.eat('('); vals=[]
        while True:
            vals.append(self.expr())
            if not self.accept(','): break
        self.eat(')'); return Insert(table=table,columns=cols,values=vals,line=s.line,column=s.column)
    def select(self):
        """按 SELECT→FROM→JOIN→WHERE→GROUP/ORDER→LIMIT/OFFSET 顺序构造 AST。"""
        s=self.eat('SELECT'); distinct=bool(self.accept('DISTINCT')); cols=[]
        if self.accept('*'): cols=[Star(line=s.line,column=s.column)]
        else:
            while True:
                if self.cur().type in ('IDENT','COUNT','SUM','AVG','MIN','MAX'):
                    kt=self.eat(); ident=Identifier(name=kt.lexeme,line=kt.line,column=kt.column)
                    if self.accept('('):
                        fn=ident.name.upper(); arg=Star() if self.accept('*') else self.expr(); self.eat(')'); ident=Unary(op=fn,expr=arg)
                    cols.append(ident)
                else: cols.append(self.expr())
                if not self.accept(','): break
        self.eat('FROM'); table=self.eat('IDENT').lexeme; joins=[]
        while self.accept('JOIN'):
            jt=self.eat('IDENT').lexeme; self.eat('ON'); cond=self.expr(); joins.append((jt,cond))
        where=self.expr() if self.accept('WHERE') else None; group=[]; order=[]
        if self.accept('GROUP'):
            self.eat('BY');
            while True:
                group.append(self.expr())
                if not self.accept(','): break
        if self.accept('ORDER'):
            self.eat('BY')
            while True:
                e=self.expr(); direction='ASC' if not self.accept('DESC') else 'DESC'; self.accept('ASC'); order.append((e,direction))
                if not self.accept(','): break
        limit=None
        if self.accept('LIMIT'):
            tok=self.eat('NUMBER')
            if '.' in tok.lexeme: raise SyntaxError_('LIMIT requires a non-negative integer',tok.line,tok.column)
            limit=int(tok.lexeme)
        offset=0
        if self.accept('OFFSET'):
            tok=self.eat('NUMBER')
            if '.' in tok.lexeme: raise SyntaxError_('OFFSET requires a non-negative integer',tok.line,tok.column)
            offset=int(tok.lexeme)
        return Select(columns=cols,table=table,where=where,joins=joins,order=order,group=group,limit=limit,offset=offset,distinct=distinct,line=s.line,column=s.column)
    def delete(self):
        # 解析 DELETE 目标表以及可选 WHERE 条件。
        s=self.eat('DELETE'); self.eat('FROM'); table=self.eat('IDENT').lexeme; return Delete(table=table,where=self.expr() if self.accept('WHERE') else None,line=s.line,column=s.column)
    def update(self):
        # 解析 UPDATE 的赋值列表以及可选 WHERE 条件。
        s=self.eat('UPDATE'); table=self.eat('IDENT').lexeme; self.eat('SET'); a={}
        while True:
            n=self.eat('IDENT').lexeme; self.eat('='); a[n]=self.expr()
            if not self.accept(','): break
        return Update(table=table,assignments=a,where=self.expr() if self.accept('WHERE') else None,line=s.line,column=s.column)
    def expr(self,minp=0):
        """按运算符优先级解析表达式，保证乘除、比较、AND、OR 的结合顺序。"""
        tok=self.cur()
        if tok.type in ('NOT','-'):
            self.eat(); left=Unary(op=tok.type,expr=self.expr(6),line=tok.line,column=tok.column)
        elif tok.type=='(':
            self.eat(); left=self.expr(); self.eat(')')
        elif tok.type=='IDENT': self.eat(); left=Identifier(name=tok.lexeme,line=tok.line,column=tok.column)
        elif tok.type=='NUMBER': self.eat(); left=Literal(value=float(tok.lexeme) if '.' in tok.lexeme else int(tok.lexeme),dtype='FLOAT' if '.' in tok.lexeme else 'INT',line=tok.line,column=tok.column)
        elif tok.type=='STRING': self.eat(); left=Literal(value=tok.lexeme,dtype='VARCHAR',line=tok.line,column=tok.column)
        elif tok.type in ('NULL','TRUE','FALSE'):
            self.eat(); left=Literal(value=None if tok.type=='NULL' else tok.type=='TRUE',dtype='NULL' if tok.type=='NULL' else 'BOOL',line=tok.line,column=tok.column)
        else: raise SyntaxError_('expected expression',tok.line,tok.column)
        if self.cur().type=='IS':
            op=self.eat(); neg=bool(self.accept('NOT')); self.eat('NULL'); left=Binary(left=left,op='IS NOT NULL' if neg else 'IS NULL',right=Literal(value=None,dtype='NULL'),line=op.line,column=op.column)
        prec={'OR':1,'AND':2,'=':3,'==':3,'!=':3,'<>':3,'>':3,'>=':3,'<':3,'<=':3,'+':4,'-':4,'*':5,'/':5,'%':5}
        while self.cur().type in prec and prec[self.cur().type]>=minp:
            op=self.eat(); p=prec[op.type]; right=self.expr(p+1); left=Binary(left=left,op=op.type,right=right,line=op.line,column=op.column)
        return left
__all__ = ['Parser']
