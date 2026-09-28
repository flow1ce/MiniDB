from __future__ import annotations
import copy
import os
import threading
import time

from common.errors import DBError, SemanticError
from sql_compiler.ast import *
from sql_compiler.lexer import Lexer
from sql_compiler.parser import Parser
from sql_compiler.semantic import validate_statement, _column_type
from sql_compiler.planner import choose_plan, plan_for, _index_candidate
from sql_compiler.optimizer import optimize
from engine.expressions import eval_expr
from engine.executor import PlanExecutor
from engine.metrics import ExecutionMetrics
from engine.results import normalize_result
from storage.page import PageManager
from storage.buffer_pool import BufferPool
from storage.table_heap import TableHeap
from index.bplus_tree import BPlusTree
from catalog.paged_catalog import PagedCatalog
from transaction import Transaction, LockManager, LogManager, IsolationLevel

_LOCK_REGISTRY = {}
_LOCK_REGISTRY_GUARD = threading.RLock()

class Database:
    """数据库总编排器：连接 SQL 编译、Catalog、存储、索引、事务和结果输出。"""
    def __init__(self,data_dir='data',buffer_capacity=16,policy='LRU'):
        # 初始化持久化目录、系统目录、缓存、共享锁表、WAL，并执行启动恢复。
        self.data_dir=os.path.abspath(data_dir); os.makedirs(self.data_dir,exist_ok=True); self.catalog=PagedCatalog(os.path.join(self.data_dir,'catalog.dat'),error_cls=SemanticError); self.current_user='admin'; self.buffer=BufferPool(buffer_capacity,policy); self.txn_snapshot=None; self.txn_rows=None; self.txn_visible=None; self._txn_write_tables=set(); self.transaction=None
        with _LOCK_REGISTRY_GUARD:
            self.lock_manager=_LOCK_REGISTRY.setdefault(self.data_dir,LockManager())
        self.wal=LogManager(os.path.join(self.data_dir,'wal.log')); self._recovering=False; self.lock=threading.RLock(); self._active_metrics=None
        self.session_id=None; self.lock_timeout=30.0; self._is_session=False; self._shared_metadata_lock=threading.RLock(); self._recover_wal()
        self.executor=PlanExecutor(self)
    def create_session(self, session_id=None):
        """Create a lightweight independent SQL session over shared storage.

        Sessions share Catalog, Buffer Pool, WAL and LockManager, while keeping
        transaction state and the execution mutex private to each browser tab.
        """
        session=object.__new__(Database)
        session.data_dir=self.data_dir; session.catalog=self.catalog; session.current_user=self.current_user
        session.buffer=self.buffer; session.lock_manager=self.lock_manager; session.wal=self.wal
        session.txn_snapshot=None; session.txn_rows=None; session.txn_visible=None; session._txn_write_tables=set(); session.transaction=None
        session._recovering=False; session.lock=threading.RLock(); session._active_metrics=None
        session.session_id=session_id; session.lock_timeout=self.lock_timeout; session._is_session=True
        session._shared_metadata_lock=self._shared_metadata_lock; session.executor=PlanExecutor(session)
        return session
    def close(self):
        """关闭数据库：回滚遗留事务，刷回缓存脏页并刷新 WAL。"""
        with self.lock:
            if self.transaction is not None:
                self._execute_one(Txn(action='ROLLBACK'))
            self.lock_manager.release_all(self.transaction.txn_id) if self.transaction else None
            if not self._is_session:
                self.buffer.flush_all(); self.wal.flush()
    def __enter__(self): return self
    def __exit__(self,exc_type,exc_value,traceback): self.close(); return False
    def _pf(self,t): return PageManager(os.path.join(self.data_dir,f"table_{t.name.lower()}.dat"))
    def _heap(self,t): return TableHeap(self._pf(t),self.buffer,t.pages)
    def _index_tree(self,t,index_name,meta):
        # 根据 Catalog 中的根页等信息，打开指定表的持久化 B+ 树文件。
        path=os.path.join(self.data_dir,f"index_{t.name.lower()}_{index_name.lower()}.dat")
        return BPlusTree(PageManager(path),self.buffer,meta.get('root_page_id'),order=meta.get('order',16),unique=meta.get('unique',False))
    def _index_insert(self,t,row,rid):
        # 插入表记录后，同步把各索引列的“键 → RID”写入所有相关索引。
        for name,meta in t.indexes.items():
            key=row.get(meta['column'])
            if key is None: continue  # SQL NULL is not an ordered index key
            tree=self._index_tree(t,name,meta); tree.insert(key,rid); meta['root_page_id']=tree.root_page_id
    def _index_delete(self,t,row,rid):
        # 删除/更新表记录前，移除该行在所有相关索引中的旧条目。
        for name,meta in t.indexes.items():
            key=row.get(meta['column'])
            if key is None: continue
            tree=self._index_tree(t,name,meta); tree.delete(key,rid); meta['root_page_id']=tree.root_page_id
    def _check_privilege(self,privilege,table=None):
        # admin 直接放行；普通用户必须拥有对应表或全局权限。
        if getattr(self,'current_user','admin')=='admin': return
        user=next((u for u in self.catalog.users if u.lower()==str(self.current_user).lower()),str(self.current_user))
        key=f"{user}:{(table or '*').lower()}"
        allowed=set(self.catalog.privileges.get(key,[])) | set(self.catalog.privileges.get(f"{self.current_user}:*",[]))
        if privilege not in allowed: raise SemanticError(f"user '{self.current_user}' lacks {privilege} privilege on '{table or '*'}'")
    def _log_change(self, action, table, rid, before=None, after=None):
        # 将行级 INSERT/UPDATE/DELETE 的前镜像和后镜像写入 WAL。
        if self._recovering: return
        txn_id=self.transaction.txn_id if self.transaction else 0
        rec=self.wal.append(txn_id,action,page_id=rid.page_id,slot_id=rid.slot_id,before={'table':table,'row':before} if before is not None else {'table':table},after={'table':table,'row':after} if after is not None else {'table':table})
        if self.transaction:
            self.transaction.last_lsn=rec.lsn
            self.transaction.undo.append({'action':action,'table':table,'rid':rid.to_dict(),'before':before,'after':after})
    def _checkpoint_if_autocommit(self):
        """自动提交写操作后建立检查点；无活动事务时截断旧 WAL。"""
        if self.transaction or self._recovering: return
        active=self.wal.active_transactions(); marker=self.wal.checkpoint(active)
        if not active: self.wal.truncate_before(marker.lsn)
    def _recover_wal(self):
        """启动恢复：已提交事务按后镜像 REDO，未提交事务按前镜像 UNDO。"""
        records=self.wal.records(); checkpoints=[r for r in records if r.action=='CHECKPOINT']
        self.recovery_report={'records':len(records),'redone':0,'undone':0,
                              'last_checkpoint_lsn':checkpoints[-1].lsn if checkpoints else None}
        if not records: return
        committed={0} | {r.txn_id for r in records if r.action=='COMMIT'}
        changes=[r for r in records if r.action in ('INSERT','UPDATE','DELETE') and r.page_id is not None]
        if not changes: return
        self._recovering=True
        try:
            for rec in changes:
                payload=rec.after if rec.txn_id in committed else rec.before
                table=(payload or {}).get('table'); row=(payload or {}).get('row')
                if not table: continue
                try: meta=self.catalog.get(table)
                except DBError: continue
                heap=self._heap(meta)
                from storage.record import RID
                rid=RID(rec.page_id,rec.slot_id)
                try:
                    if rec.action=='INSERT':
                        if rec.txn_id in committed: heap.update(rid,row) if heap.get(rid) is not None else heap.insert(row); self.recovery_report['redone']+=1
                        elif heap.get(rid) is not None: heap.delete(rid); self.recovery_report['undone']+=1
                    elif rec.action=='UPDATE':
                        if row is not None: heap.update(rid,row); self.recovery_report['redone' if rec.txn_id in committed else 'undone']+=1
                    elif rec.action=='DELETE':
                        if rec.txn_id in committed:
                            if heap.get(rid) is not None: heap.delete(rid); self.recovery_report['redone']+=1
                        elif row is not None: heap.update(rid,row); self.recovery_report['undone']+=1
                except Exception: pass
        finally: self._recovering=False
    def _rows(self,t):
        # 读取表中全部有效行，并在页列表变化时同步更新 Catalog。
        heap=self._heap(t); out=[row for _rid,row in heap.scan() if row and not row.get('_deleted')]
        if heap.page_ids != t.pages:
            t.pages=heap.page_ids; self.catalog.save()
        return out
    def _save_rows(self,t,rows):
        pf=self._pf(t); pages=[rows[i:i+30] for i in range(0,len(rows),30)] or [[]]
        for i,p in enumerate(pages):
            if i<pf.count(): self.buffer.put(pf,i,p)
            else: pf.append(p)
        t.pages=list(range(len(pages))); self.catalog.save()
    def _restore_rows(self,t,target_rows):
        """ROLLBACK 用：按 RID 恢复事务前快照，并重建该表的全部索引。"""
        heap=self._heap(t); current=list(heap.scan())
        target={r.get('_id'):r for r in target_rows if r is not None and r.get('_id') is not None}
        seen=set()
        for rid,row in current:
            key=row.get('_id') if row else None
            if key in target:
                desired=target[key]; seen.add(key)
                if row != desired: heap.update(rid,desired)
            else:
                heap.delete(rid)
        for key,row in target.items():
            if key not in seen: heap.insert(dict(row))
        t.pages=heap.page_ids
        for name,meta in t.indexes.items():
            path=os.path.join(self.data_dir,f"index_{t.name.lower()}_{name.lower()}.dat")
            for key in list(getattr(self.buffer,'frames',{})):
                if key[0]==path: self.buffer.frames.pop(key,None)
            try: os.remove(path)
            except FileNotFoundError: pass
            tree=self._index_tree(t,name,{**meta,'root_page_id':None})
            for rid,row in heap.scan():
                key=row.get(meta['column']) if row else None
                if key is not None: tree.insert(key,rid)
            meta['root_page_id']=tree.root_page_id
    def _undo_transaction(self):
        """Undo only this session's writes, preserving other sessions' commits."""
        if not self.transaction: return
        from storage.record import RID
        for change in reversed(self.transaction.undo):
            table_name=change['table']; rid=RID(**change['rid']); t=self.catalog.get(table_name); heap=self._heap(t)
            current=heap.get(rid)
            if change['action']=='INSERT':
                if current is not None:
                    self._index_delete(t,current,rid); heap.delete(rid)
            elif change['action']=='UPDATE':
                before=change.get('before')
                if current is not None and before is not None:
                    self._index_delete(t,current,rid); heap.update(rid,before); self._index_insert(t,before,rid)
            elif change['action']=='DELETE':
                before=change.get('before')
                if current is None and before is not None:
                    heap.restore(rid,before); self._index_insert(t,before,rid)
            t.pages=heap.page_ids
        self.catalog.save()
    def _abort_active_transaction(self):
        """Abort and clean up a session after a lock timeout or deadlock."""
        if not self.transaction: return
        txn_id=self.transaction.txn_id
        try: self._undo_transaction()
        finally:
            self.wal.append(txn_id,'ABORT'); self.transaction.abort(); self.lock_manager.release_all(txn_id)
            self.txn_snapshot=None; self.txn_rows=None; self.txn_visible=None; self._txn_write_tables=set(); self.transaction=None
    def _check_row(self,t,vals):
        # 检查单行值是否满足列类型、主键非空和 NOT NULL 要求。
        for c in t.columns:
            v=vals.get(c['name']); typ=c['type']
            if (c.get('primary_key') or c.get('not_null')) and v is None: raise SemanticError(f"column '{c['name']}' cannot be NULL")
            if v is None: continue
            if typ=='INT' and (not isinstance(v,int) or isinstance(v,bool)): raise SemanticError(f"{c['name']} expects INT")
            if typ=='FLOAT' and (not isinstance(v,(int,float)) or isinstance(v,bool)): raise SemanticError(f"{c['name']} expects FLOAT")
            if typ=='VARCHAR' and not isinstance(v,str): raise SemanticError(f"{c['name']} expects VARCHAR")
            if typ=='BOOL' and not isinstance(v,bool): raise SemanticError(f"{c['name']} expects BOOL")
    def _check_constraints(self,t,row,exclude_rid=None):
        # 扫描已有记录，检查 PRIMARY KEY / UNIQUE 值是否重复。
        heap=self._heap(t)
        for c in t.columns:
            if not (c.get('primary_key') or c.get('unique')): continue
            key,value=c['name'],row.get(c['name'])
            if value is None: continue
            for rid,old in heap.scan():
                if exclude_rid is not None and rid==exclude_rid: continue
                if old and not old.get('_deleted') and old.get(key)==value:
                    label='primary key' if c.get('primary_key') else 'unique column'
                    raise SemanticError(f"duplicate {label} value {value!r} for '{key}'")
    def resolve_column(self,table_name,column_name):
        """公开的列名解析接口：返回列所属表和数据类型。"""
        t=self.catalog.get(table_name); typ=_column_type([dict(c,table=t.name) for c in t.columns],column_name)
        if typ is None: raise SemanticError(f"column '{column_name}' does not exist in table '{table_name}'")
        return {'table':t.name,'column':column_name,'type':typ}
    def _uses_snapshot(self, table):
        # REPEATABLE READ / SERIALIZABLE 对尚未写入的表读取事务开始快照。
        return bool(self.transaction and self.transaction.isolation in (IsolationLevel.REPEATABLE_READ, IsolationLevel.SERIALIZABLE)
                    and self.txn_visible is not None and table.name.lower() not in self._txn_write_tables)
    def compile(self,sql):
        """只编译不执行：返回每条 SQL 的 Token、AST、语义结果及优化前后计划。"""
        tokens=Lexer(sql).tokens(); statements=Parser(tokens).parse(); stages=[]
        token_groups=[]; current=[]
        for token in tokens:
            if token.type == 'EOF':
                if current: token_groups.append(current)
                break
            current.append(token)
            if token.type == ';':
                token_groups.append(current); current=[]
        if current: token_groups.append(current)
        for index, statement in enumerate(statements):
            validate_statement(statement,self.catalog)
            target=statement.inner if isinstance(statement,Explain) else statement
            raw=plan_for(target); optimized=optimize(choose_plan(target,self.catalog))
            statement_tokens=token_groups[index] if index < len(token_groups) else [t for t in tokens if t.type != 'EOF']
            stages.append({'tokens':[t.to_dict() for t in statement_tokens if t.type!=';'],'ast':statement.to_dict(),'semantic':'OK','plan':raw.to_dict(),'optimized_plan':optimized.to_dict()})
        return stages[0] if len(stages)==1 else stages
    def execute(self,sql,debug=False):
        """嵌入式原始执行入口：解析多条 SQL，逐条执行并管理语句级锁释放。"""
        with self.lock:
            toks=Lexer(sql).tokens(); stmts=Parser(toks).parse(); results=[]
            for s in stmts:
                try:
                    results.append(self._execute_one(s,debug))
                finally:
                    if not self.transaction and not isinstance(s,Txn):
                        self.lock_manager.release_all(getattr(self,'_statement_lock_txn',0))
                    elif self.transaction and self.transaction.isolation==IsolationLevel.READ_COMMITTED:
                        # READ COMMITTED releases shared locks at statement end;
                        # write locks remain until COMMIT/ROLLBACK.
                        for resource in list(getattr(self,'_statement_shared_locks',())):
                            self.lock_manager.release(self.transaction.txn_id,resource)
                            self.transaction.locks.discard(resource)
            return results[0] if len(results)==1 else results
    def execute_structured(self, sql, optimize_enabled=True):
        """CLI/Web 主入口：执行 SQL，采集指标并返回统一的前端结果结构。"""
        with self.lock:
            statements=Parser(Lexer(sql).tokens()).parse(); results=[]
            for statement in statements:
                metrics=ExecutionMetrics.start(self.buffer); started=time.perf_counter_ns()
                self._active_metrics=metrics
                try:
                    raw=self._execute_one(statement,optimize_enabled=optimize_enabled)
                    metrics.add_planning_ns(0)
                    returned=raw.get('count',raw.get('row_count',raw.get('affected',raw.get('inserted',0)))) if isinstance(raw,dict) else 0
                    if isinstance(raw,dict) and isinstance(raw.get('actual'),dict): returned=raw['actual'].get('count',returned)
                    metrics.finish(self.buffer,returned)
                    results.append(normalize_result(statement,raw,metrics))
                except DBError as exc:
                    metrics.finish(self.buffer,0)
                    if exc.stage=='Concurrency' and self.transaction is not None:
                        self._abort_active_transaction()
                    results.append({'success':False,'type':'error','error':{'stage':exc.stage,'message':exc.message,'line':exc.line,'column':exc.column},'metrics':metrics.to_dict()})
                finally:
                    self._active_metrics=None
                    if not self.transaction and not isinstance(statement,Txn):
                        self.lock_manager.release_all(getattr(self,'_statement_lock_txn',0))
                    elif self.transaction and self.transaction.isolation==IsolationLevel.READ_COMMITTED:
                        for resource in list(getattr(self,'_statement_shared_locks',())):
                            self.lock_manager.release(self.transaction.txn_id,resource); self.transaction.locks.discard(resource)
            return results[0] if len(results)==1 else results
    def _execute_one(self,s,debug=False,optimize_enabled=True):
        """单条语句总分派器：先事务/语义/权限/锁/计划，再按语句类型实际执行。"""
        self._statement_shared_locks=[]
        if isinstance(s,Txn):
            # 事务控制分支：BEGIN 建快照，ROLLBACK 恢复快照，COMMIT 释放锁并检查点。
            if s.action=='BEGIN':
                if self.transaction is not None: raise DBError('Concurrency','transaction already active',s.line,s.column)
                self.txn_snapshot=True; self.txn_rows=None; self.txn_visible={}; self._txn_write_tables=set()
                for k,v in self.catalog.tables.items(): self.txn_visible[k]=[(rid,copy.deepcopy(row)) for rid,row in self._heap(v).scan() if row and not row.get('_deleted')]
                try: iso=IsolationLevel(s.isolation)
                except ValueError: iso=IsolationLevel.READ_COMMITTED
                self.transaction=Transaction(iso,session_id=self.session_id); self.wal.append(self.transaction.txn_id,'BEGIN'); return 'BEGIN'
            if s.action=='ROLLBACK':
                if self.transaction is None:
                    raise DBError('Concurrency','no active transaction to rollback',s.line,s.column)
                self._undo_transaction(); self.wal.append(self.transaction.txn_id,'ABORT'); self.transaction.abort(); self.lock_manager.release_all(self.transaction.txn_id)
                self.txn_snapshot=None; self.txn_rows=None; self.txn_visible=None; self._txn_write_tables=set(); self.transaction=None
                return 'ROLLBACK'
            if self.transaction:
                self.wal.append(self.transaction.txn_id,'COMMIT'); self.transaction.commit(); self.transaction.undo.clear(); self.lock_manager.release_all(self.transaction.txn_id)
                active=self.wal.active_transactions(); marker=self.wal.checkpoint(active)
                if not active: self.wal.truncate_before(marker.lsn)
            else:
                raise DBError('Concurrency','no active transaction to commit',s.line,s.column)
            self.txn_snapshot=None; self.txn_rows=None; self.txn_visible=None; self._txn_write_tables=set(); self.transaction=None; return 'COMMIT'
        planning_started=time.perf_counter_ns()
        # 所有普通语句在执行前统一经过语义检查、权限检查和表级加锁。
        if self.transaction and isinstance(s,(CreateTable,CreateIndex,DropTable,DropIndex,CreateUser,Grant,Revoke)):
            raise DBError('Concurrency','DDL is not supported inside an explicit transaction',s.line,s.column)
        validate_statement(s,self.catalog)
        privilege={'SELECT':'SELECT','Insert':'INSERT','Delete':'DELETE','Update':'UPDATE','CreateTable':'CREATE','CreateIndex':'INDEX','DropTable':'CREATE','DropIndex':'INDEX'}.get(type(s).__name__)
        table=getattr(s,'table',None)
        if table is None and isinstance(s,DropTable): table=s.name
        if table is None and isinstance(s,DropIndex):
            table=next((t.name for t in self.catalog.tables.values() if any(n.lower()==s.name.lower() for n in t.indexes)),None)
        if privilege: self._check_privilege(privilege,table)
        lock_txn=self.transaction.txn_id if self.transaction else -abs(id(self))
        self._statement_lock_txn=lock_txn
        lock_resource=None
        if isinstance(s,(Select,Insert,Delete,Update,CreateTable,CreateIndex,DropTable,DropIndex)):
            lock_table=getattr(s,'table',getattr(s,'name',''))
            if isinstance(s,DropIndex):
                lock_table=next((t.name for t in self.catalog.tables.values() if any(n.lower()==s.name.lower() for n in t.indexes)),s.name)
            lock_resource=f"table:{lock_table.lower()}"
            # DML uses a shared table lock plus row-level X locks so two
            # transactions can update different rows and demonstrate waits
            # and deadlocks.  DDL and INSERT retain table-level exclusivity.
            lock_mode='SHARED' if isinstance(s,(Select,Update,Delete)) else 'EXCLUSIVE'
            try:
                self.lock_manager.acquire(lock_txn,lock_resource,lock_mode,timeout=self.lock_timeout)
                if self.transaction: self.transaction.locks.add(lock_resource)
                if self.transaction and lock_mode=='SHARED': self._statement_shared_locks.append(lock_resource)
            except TimeoutError as e:
                raise DBError('Concurrency',str(e))
        target=s.inner if isinstance(s,Explain) else s
        # 同时生成原始计划和优化计划；EXPLAIN 只展示，SELECT 才交给物理算子树。
        plan=plan_for(target)
        opt=optimize(choose_plan(target,self.catalog)) if optimize_enabled else plan
        if self._active_metrics: self._active_metrics.add_planning_ns(time.perf_counter_ns()-planning_started)
        if isinstance(s,Explain):
            result={'plan':plan.to_dict(),'optimized_plan':opt.to_dict(),'text':opt.text()}
            if s.analyze and isinstance(s.inner,Select):
                result['actual']=self.executor.select(opt,self.catalog.get(s.inner.table))
            return result
        if isinstance(s,ShowTables):
            # 元数据命令：列出 Catalog 中所有表。
            return {'tables':sorted(t.name for t in self.catalog.tables.values()),'count':len(self.catalog.tables)}
        if isinstance(s,Describe):
            # 元数据命令：返回表的列、数据页和索引信息。
            t=self.catalog.get(s.name)
            return {'table':t.name,'columns':[dict(c) for c in t.columns],
                    'pages':list(t.pages),'indexes':sorted(t.indexes)}
        # DDL：建表只创建并持久化 Catalog 元数据，数据页在首次插入时分配。
        if isinstance(s,CreateTable): self.catalog.create(s.name,s.columns); return 'OK'
        if isinstance(s,CreateUser):
            # 用户管理：新增用户并写入 Catalog。
            if s.name.lower() in {u.lower() for u in self.catalog.users}: raise SemanticError(f"user '{s.name}' already exists")
            self.catalog.users.add(s.name); self.catalog.save(); return 'OK'
        if isinstance(s,(Grant,Revoke)):
            # 权限管理：修改“用户:表 → 权限列表”映射并持久化。
            user=next((u for u in self.catalog.users if u.lower()==s.user.lower()),None)
            if user is None: raise SemanticError(f"user '{s.user}' does not exist")
            key=f"{user}:{s.table.lower()}"; current=set(self.catalog.privileges.get(key,[]))
            if isinstance(s,Grant): current.add(s.privilege)
            else: current.discard(s.privilege)
            self.catalog.privileges[key]=sorted(current); self.catalog.save(); return 'OK'
        if isinstance(s,DropTable):
            # DROP TABLE：删除 Catalog 项、表文件、空闲页文件及该表全部索引文件。
            key=s.name.lower(); t=self.catalog.tables.pop(key,None)
            if t is None: raise SemanticError(f"table '{s.name}' does not exist")
            for path in [os.path.join(self.data_dir,f'table_{key}.dat'),os.path.join(self.data_dir,f'table_{key}.dat.free')]:
                for frame_key in list(getattr(self.buffer,'frames',{})):
                    if frame_key[0]==os.path.abspath(path): self.buffer.frames.pop(frame_key,None)
                try: os.remove(path)
                except FileNotFoundError: pass
            for index_name in list(t.indexes):
                try: os.remove(os.path.join(self.data_dir,f'index_{key}_{index_name.lower()}.dat'))
                except FileNotFoundError: pass
            self.catalog.save(); return 'OK'
        if isinstance(s,DropIndex):
            # DROP INDEX：定位所属表，移除索引元数据并删除索引文件。
            for t in self.catalog.tables.values():
                match=next((name for name in t.indexes if name.lower()==s.name.lower()),None)
                if match is not None:
                    t.indexes.pop(match); self.catalog.save()
                    path=os.path.abspath(os.path.join(self.data_dir,f"index_{t.name.lower()}_{match.lower()}.dat"))
                    for frame_key in list(getattr(self.buffer,'frames',{})):
                        if frame_key[0]==path: self.buffer.frames.pop(frame_key,None)
                    try: os.remove(os.path.join(self.data_dir,f"index_{t.name.lower()}_{match.lower()}.dat"))
                    except FileNotFoundError: pass
                    return 'OK'
            raise SemanticError(f"index '{s.name}' does not exist")
        if isinstance(s,CreateIndex):
            # CREATE INDEX：扫描现有表行构建 B+ 树，再把根页等信息写回 Catalog。
            t=self.catalog.get(s.table)
            if not any(c['name'].lower()==s.index_column.lower() for c in t.columns): raise SemanticError(f"column '{s.index_column}' does not exist")
            if any(name.lower()==s.name.lower() for name in t.indexes): raise SemanticError(f"index '{s.name}' already exists")
            meta={'column':s.index_column,'unique':s.unique,'order':16}; index_path=os.path.join(self.data_dir,f"index_{t.name.lower()}_{s.name.lower()}.dat"); tree=self._index_tree(t,s.name,meta)
            try:
                for rid,r in self._heap(t).scan():
                    if r.get(s.index_column) is not None: tree.insert(r.get(s.index_column),rid)
            except ValueError as e:
                try: os.remove(index_path)
                except (FileNotFoundError,UnboundLocalError): pass
                raise SemanticError(str(e),s.line,s.column)
            meta['root_page_id']=tree.root_page_id; t.indexes[s.name]=meta; self.catalog.save(); return 'OK'
        if isinstance(s,Insert):
            # INSERT：组装并校验行，写 TableHeap，维护索引和 WAL，最后保存页列表。
            t=self.catalog.get(s.table); cols=s.columns or [c['name'] for c in t.columns]
            if len(cols)!=len(s.values): raise SemanticError('INSERT column/value count mismatch',s.line,s.column)
            row={c:eval_expr(v,{}) for c,v in zip(cols,s.values)}; self._check_row(t,row); self._check_constraints(t,row); rows=self._rows(t); row['_id']=(max([r.get('_id',0) for r in rows],default=0)+1)
            heap=self._heap(t); rid=heap.insert(row); self._index_insert(t,row,rid); self._log_change('INSERT',t.name,rid,after=row); t.pages=heap.page_ids; self.catalog.save();
            if self.transaction: self._txn_write_tables.add(t.name.lower())
            self._checkpoint_if_autocommit(); return {'inserted':1,'rid':rid.to_dict()}
        if isinstance(s,(Delete,Update)):
            # UPDATE/DELETE：等值索引可直接定位 RID，其余条件顺序扫描后逐行修改。
            t=self.catalog.get(s.table); heap=self._heap(t); n=0
            indexed=_index_candidate(s,self.catalog)
            if indexed and indexed[2] in ('=','=='):
                iname,imeta,iop,ikey=indexed; scan_items=[(rid,heap.get(rid)) for rid in self._index_tree(t,iname,imeta).search(ikey)]
            else: scan_items=list(heap.scan())
            if self._active_metrics: self._active_metrics.examine(len(scan_items))
            for rid,r in scan_items:
                if r is None: continue
                if eval_expr(s.where,r):
                    try:
                        row_resource=f"row:{t.name.lower()}:{rid.page_id}:{rid.slot_id}"
                        self.lock_manager.acquire(lock_txn,row_resource,'EXCLUSIVE',timeout=self.lock_timeout)
                        if self.transaction:
                            self.transaction.locks.add(row_resource)
                        # The row may have changed while this transaction
                        # waited.  Re-read it after acquiring X lock.
                        r=heap.get(rid)
                        if r is None or not eval_expr(s.where,r): continue
                    except TimeoutError as e: raise DBError('Concurrency',str(e),s.line,s.column)
                    if isinstance(s,Delete): self._index_delete(t,r,rid); heap.delete(rid); self._log_change('DELETE',t.name,rid,before=r)
                    else:
                        new_row=dict(r)
                        for k,e in s.assignments.items():
                            if k not in new_row: raise SemanticError(f"column '{k}' does not exist")
                            new_row[k]=eval_expr(e,new_row)
                        self._check_row(t,new_row); self._check_constraints(t,new_row,exclude_rid=rid); self._index_delete(t,r,rid); heap.update(rid,new_row); self._index_insert(t,new_row,rid); self._log_change('UPDATE',t.name,rid,before=r,after=new_row)
                    n+=1
            t.pages=heap.page_ids; self.catalog.save();
            if self.transaction and n: self._txn_write_tables.add(t.name.lower())
            self._checkpoint_if_autocommit(); return {'affected':n}
        # SELECT 是唯一完整交给 PlanExecutor 递归执行物理算子树的语句类型。
        if isinstance(s,Select): return self.executor.select(opt,self.catalog.get(s.table))
        raise SemanticError('unsupported statement')
