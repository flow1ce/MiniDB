"""Page-backed B+ tree for educational database indexes."""
from bisect import bisect_left, bisect_right
from functools import wraps
from threading import RLock
from storage.record import RID

_TREE_LOCKS={}
_TREE_LOCKS_GUARD=RLock()

def _tree_lock(path):
    with _TREE_LOCKS_GUARD: return _TREE_LOCKS.setdefault(path,RLock())

def _synchronized(method):
    @wraps(method)
    def wrapped(self,*args,**kwargs):
        with self._lock: return method(self,*args,**kwargs)
    return wrapped

class BPlusTree:
    """页式 B+ 树：持久化“索引键 → RID 列表”，支持等值与范围扫描。"""
    def __init__(self,manager,buffer_pool=None,root_page_id=None,order=16,unique=False):
        self.manager=manager; self.buffer=buffer_pool; self.order=max(4,order); self.unique=unique; self._lock=_tree_lock(manager.path)
        with self._lock:
            if root_page_id is None:
                self.root_page_id=manager.allocate_page(); self._write(self.root_page_id,self._leaf())
            else: self.root_page_id=int(root_page_id)
    def _leaf(self,parent=-1): return {'page_type':'bplus_leaf','parent':parent,'keys':[],'values':[],'prev':-1,'next':-1}
    def _internal(self,parent=-1): return {'page_type':'bplus_internal','parent':parent,'keys':[],'children':[]}
    def _read(self,pid): return self.buffer.get_page(self.manager,pid) if self.buffer else self.manager.read_page(pid)
    def _write(self,pid,node):
        # 索引节点同样通过 Buffer Pool/页文件读写，并在修改后刷盘。
        if self.buffer:
            self.buffer.put_page(self.manager,pid,node,True); self.buffer.flush_page(self.manager,pid)
        else: self.manager.write_page(pid,node)
    def _rid(self,rid): return rid.to_dict() if isinstance(rid,RID) else {'page_id':int(rid['page_id']),'slot_id':int(rid['slot_id'])}
    def _find_leaf(self,key):
        """从根沿分隔键下降，找到目标键应位于的叶子页。"""
        pid=self.root_page_id
        while True:
            node=self._read(pid)
            if node['page_type']=='bplus_leaf': return pid,node
            pid=node['children'][bisect_right(node['keys'],key)]
    @_synchronized
    def search(self,key):
        """等值查询：返回该键关联的全部 RID；不存在时返回空列表。"""
        _pid,node=self._find_leaf(key); i=bisect_left(node['keys'],key)
        if i>=len(node['keys']) or node['keys'][i]!=key: return []
        return [RID(**r) for r in node['values'][i]]
    @_synchronized
    def range_scan(self,low=None,high=None,low_inclusive=True,high_inclusive=True):
        """沿叶子链执行范围扫描，支持上下界及开闭区间。"""
        if low is None:
            pid=self.root_page_id; node=self._read(pid)
            while node['page_type']!='bplus_leaf': pid=node['children'][0]; node=self._read(pid)
        else: pid,node=self._find_leaf(low)
        out=[]
        while True:
            for k,vals in zip(node['keys'],node['values']):
                if low is not None and (k<low or (not low_inclusive and k==low)): continue
                if high is not None and (k>high or (not high_inclusive and k==high)): return out
                out.extend(RID(**r) for r in vals)
            if node.get('next',-1)<0: return out
            pid=node['next']; node=self._read(pid)
    range=range_scan
    @_synchronized
    def validate(self):
        """校验父指针、键顺序、子节点数量和叶子链，损坏时抛出断言。"""
        seen=set(); leaves=[]
        def walk(pid,parent=-1):
            if pid in seen: raise AssertionError('cycle or duplicate page in B+ tree')
            seen.add(pid); node=self._read(pid)
            if node.get('parent',-1)!=parent: raise AssertionError(f'bad parent pointer at page {pid}')
            if node['keys']!=sorted(node['keys']): raise AssertionError(f'unsorted keys at page {pid}')
            if node['page_type']=='bplus_leaf': leaves.append((pid,node)); return
            if len(node['children'])!=len(node['keys'])+1: raise AssertionError(f'bad child count at page {pid}')
            for child in node['children']: walk(child,pid)
        walk(self.root_page_id)
        for i,(pid,node) in enumerate(leaves):
            expected_prev=leaves[i-1][0] if i else -1; expected_next=leaves[i+1][0] if i+1<len(leaves) else -1
            if node.get('prev',-1)!=expected_prev or node.get('next',-1)!=expected_next: raise AssertionError(f'bad leaf link at page {pid}')
        return {'pages':len(seen),'leaves':len(leaves),'keys':sum(len(n['keys']) for _,n in leaves),'valid':True}
    @_synchronized
    def insert(self,key,rid):
        """向叶子插入键/RID；唯一索引拒绝重复键，节点满时触发分裂。"""
        pid,node=self._find_leaf(key); i=bisect_left(node['keys'],key); rv=self._rid(rid)
        if i<len(node['keys']) and node['keys'][i]==key:
            if self.unique and node['values'][i]: raise ValueError(f'duplicate key {key!r}')
            if rv not in node['values'][i]: node['values'][i].append(rv)
        else: node['keys'].insert(i,key); node['values'].insert(i,[rv])
        self._write(pid,node)
        if len(node['keys'])>=self.order: self._split_leaf(pid,node)
    def _split_leaf(self,pid,node):
        # 叶子对半分裂，修复前后链指针，并把右页首键提升到父节点。
        mid=len(node['keys'])//2; right=self._leaf(node['parent']); right['keys']=node['keys'][mid:]; right['values']=node['values'][mid:]; node['keys']=node['keys'][:mid]; node['values']=node['values'][:mid]
        rpid=self.manager.allocate_page(); right['next']=node.get('next',-1); right['prev']=pid; node['next']=rpid
        if right['next']>=0:
            nxt=self._read(right['next']); nxt['prev']=rpid; self._write(right['next'],nxt)
        self._write(pid,node); self._write(rpid,right); self._insert_parent(pid,right['keys'][0],rpid,node['parent'])
    def _insert_parent(self,left,key,right,parent_pid):
        # 将分裂产生的分隔键插入父节点；原根分裂时创建新根。
        if parent_pid<0:
            root=self._internal(); root['keys']=[key]; root['children']=[left,right]; rpid=self.manager.allocate_page()
            for child in (left,right): n=self._read(child); n['parent']=rpid; self._write(child,n)
            self._write(rpid,root); self.root_page_id=rpid; return
        parent=self._read(parent_pid); pos=parent['children'].index(left)+1; parent['children'].insert(pos,right); parent['keys'].insert(pos-1,key)
        rn=self._read(right); rn['parent']=parent_pid; self._write(right,rn); self._write(parent_pid,parent)
        if len(parent['keys'])>=self.order: self._split_internal(parent_pid,parent)
    def _split_internal(self,pid,node):
        # 内部节点分裂：中间键提升，右半子节点改挂到新内部页。
        mid=len(node['keys'])//2; promoted=node['keys'][mid]; right=self._internal(node['parent']); right['keys']=node['keys'][mid+1:]; right['children']=node['children'][mid+1:]; node['keys']=node['keys'][:mid]; node['children']=node['children'][:mid+1]; rpid=self.manager.allocate_page()
        for child in right['children']:
            c=self._read(child); c['parent']=rpid; self._write(child,c)
        self._write(pid,node); self._write(rpid,right); self._insert_parent(pid,promoted,rpid,node['parent'])
    @_synchronized
    def delete(self,key,rid=None):
        """删除指定键或键下指定 RID；低于最小占用时进行再平衡。"""
        pid,node=self._find_leaf(key); i=bisect_left(node['keys'],key)
        if i>=len(node['keys']) or node['keys'][i]!=key: return False
        if rid is None: node['keys'].pop(i); node['values'].pop(i)
        else:
            rv=self._rid(rid); node['values'][i]=[r for r in node['values'][i] if r!=rv]
            if not node['values'][i]: node['keys'].pop(i); node['values'].pop(i)
        self._write(pid,node)
        if pid!=self.root_page_id and len(node['keys']) < max(1,(self.order-1)//2): self._rebalance_leaf(pid,node)
        return True
    def _parent_slot(self,pid,parent): return parent['children'].index(pid)
    def _rebalance_leaf(self,pid,node):
        # 叶子下溢时优先向兄弟借键，否则合并并更新父节点。
        parent=self._read(node['parent']); pos=self._parent_slot(pid,parent); min_keys=max(1,(self.order-1)//2)
        left=self._read(parent['children'][pos-1]) if pos>0 else None
        right=self._read(parent['children'][pos+1]) if pos+1<len(parent['children']) else None
        if left and len(left['keys'])>min_keys:
            node['keys'].insert(0,left['keys'].pop()); node['values'].insert(0,left['values'].pop()); parent['keys'][pos-1]=node['keys'][0]
            self._write(left['page_id'] if 'page_id' in left else parent['children'][pos-1],left); self._write(pid,node); self._write(node['parent'],parent); return
        if right and len(right['keys'])>min_keys:
            node['keys'].append(right['keys'].pop(0)); node['values'].append(right['values'].pop(0)); parent['keys'][pos]=right['keys'][0]
            self._write(parent['children'][pos+1],right); self._write(pid,node); self._write(node['parent'],parent); return
        if left:
            left['keys'].extend(node['keys']); left['values'].extend(node['values']); left['next']=node.get('next',-1)
            if node.get('next',-1)>=0: nxt=self._read(node['next']); nxt['prev']=parent['children'][pos-1]; self._write(node['next'],nxt)
            parent['children'].pop(pos); parent['keys'].pop(pos-1); self.manager.release_page(pid); self._write(parent['children'][pos-1],left); self._write(node['parent'],parent); self._rebalance_internal(node['parent'],parent)
        elif right:
            rpid=parent['children'][pos+1]; node['keys'].extend(right['keys']); node['values'].extend(right['values']); node['next']=right.get('next',-1)
            if right.get('next',-1)>=0: nxt=self._read(right['next']); nxt['prev']=pid; self._write(right['next'],nxt)
            parent['children'].pop(pos+1); parent['keys'].pop(pos); self.manager.release_page(rpid); self._write(pid,node); self._write(node['parent'],parent); self._rebalance_internal(node['parent'],parent)
    def _rebalance_internal(self,pid,node):
        # 内部节点下溢时借位/合并；空根只剩一个孩子时收缩树高。
        if pid==self.root_page_id:
            if len(node['keys'])==0 and node['children']:
                self.root_page_id=node['children'][0]; child=self._read(self.root_page_id); child['parent']=-1; self._write(self.root_page_id,child); self.manager.release_page(pid)
            return
        min_keys=max(1,(self.order-1)//2)
        if len(node['keys'])>=min_keys: self._write(pid,node); return
        parent=self._read(node['parent']); pos=self._parent_slot(pid,parent)
        left=self._read(parent['children'][pos-1]) if pos>0 else None; right=self._read(parent['children'][pos+1]) if pos+1<len(parent['children']) else None
        if left and len(left['keys'])>min_keys:
            node['keys'].insert(0,parent['keys'][pos-1]); parent['keys'][pos-1]=left['keys'].pop(); node['children'].insert(0,left['children'].pop())
            c=self._read(node['children'][0]); c['parent']=pid; self._write(node['children'][0],c); self._write(parent['children'][pos-1],left); self._write(pid,node); self._write(node['parent'],parent); return
        if right and len(right['keys'])>min_keys:
            node['keys'].append(parent['keys'][pos]); parent['keys'][pos]=right['keys'].pop(0); node['children'].append(right['children'].pop(0))
            c=self._read(node['children'][-1]); c['parent']=pid; self._write(node['children'][-1],c); self._write(parent['children'][pos+1],right); self._write(pid,node); self._write(node['parent'],parent); return
        if left:
            left['keys'].append(parent['keys'].pop(pos-1)); left['keys'].extend(node['keys']); left['children'].extend(node['children']); parent['children'].pop(pos)
            for cpid in node['children']:
                c=self._read(cpid); c['parent']=parent['children'][pos-1]; self._write(cpid,c)
            self.manager.release_page(pid); self._write(parent['children'][pos-1],left); self._write(node['parent'],parent); self._rebalance_internal(node['parent'],parent)
        elif right:
            rpid=parent['children'][pos+1]; node['keys'].append(parent['keys'].pop(pos)); node['keys'].extend(right['keys']); node['children'].extend(right['children']); parent['children'].pop(pos+1)
            for cpid in right['children']:
                c=self._read(cpid); c['parent']=pid; self._write(cpid,c)
            self.manager.release_page(rpid); self._write(pid,node); self._write(node['parent'],parent); self._rebalance_internal(node['parent'],parent)

__all__=['BPlusTree']
