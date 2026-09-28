from dataclasses import dataclass, field
from enum import Enum
from threading import Condition, RLock
from typing import Optional
import time

class LockMode(str,Enum): SHARED='SHARED'; EXCLUSIVE='EXCLUSIVE'
@dataclass
class _Lock:
    shared:set=field(default_factory=set); exclusive:Optional[int]=None

class LockManager:
    """共享/排他锁管理器：负责兼容性等待、超时、释放及等待图死锁检测。"""
    def __init__(self): self._locks={}; self._waits={}; self._waiting={}; self._cv=Condition(RLock())
    def _owners(self,lock): return set(lock.shared)|({lock.exclusive} if lock.exclusive is not None else set())
    def detect_deadlocks(self):
        """在“等待事务 → 持锁事务”图中检测环；有环即发生死锁。"""
        graph={k:set(v) for k,v in self._waits.items()}
        visiting=set(); done=set()
        def visit(n):
            if n in visiting: return True
            if n in done: return False
            visiting.add(n)
            if any(visit(x) for x in graph.get(n,set())): return True
            visiting.remove(n); done.add(n); return False
        return any(visit(n) for n in graph)
    def acquire(self,txn_id,resource,mode=LockMode.SHARED,timeout=5.0):
        """请求资源锁；不兼容时条件等待，检测到死锁或超时则失败。"""
        mode=LockMode(mode); deadline=time.monotonic()+timeout
        with self._cv:
            lock=self._locks.setdefault(resource,_Lock())
            while True:
                compatible=(mode==LockMode.SHARED and (lock.exclusive is None or lock.exclusive==txn_id)) or (mode==LockMode.EXCLUSIVE and (lock.exclusive in (None,txn_id) and not (lock.shared-{txn_id})))
                if compatible:
                    self._waits.pop(txn_id,None)
                    self._waiting.pop(txn_id,None)
                    if mode==LockMode.SHARED: lock.shared.add(txn_id)
                    else: lock.exclusive=txn_id; lock.shared.discard(txn_id)
                    return True
                self._waits[txn_id]=self._owners(lock)
                self._waiting[txn_id]={'resource':resource,'mode':mode.value,'since':time.time()}
                if self.detect_deadlocks():
                    self._waits.pop(txn_id,None)
                    self._waiting.pop(txn_id,None)
                    raise TimeoutError(f'deadlock detected on {resource!r}')
                remain=deadline-time.monotonic()
                if remain<=0:
                    self._waits.pop(txn_id,None)
                    self._waiting.pop(txn_id,None)
                    raise TimeoutError(f'lock timeout on {resource!r}')
                self._cv.wait(remain)
    def release(self,txn_id,resource):
        # 释放事务在单个资源上的锁，并唤醒所有等待者重新竞争。
        with self._cv:
            lock=self._locks.get(resource)
            if not lock: return
            lock.shared.discard(txn_id); self._waits.pop(txn_id,None); self._waiting.pop(txn_id,None)
            if lock.exclusive==txn_id: lock.exclusive=None
            if not lock.shared and lock.exclusive is None: self._locks.pop(resource,None)
            self._cv.notify_all()
    def release_all(self,txn_id):
        # COMMIT/ROLLBACK 时释放该事务持有的全部资源锁。
        with self._cv:
            for resource in list(self._locks): self.release(txn_id,resource)
    def snapshot(self):
        """返回锁持有与等待状态，供并发演示页面只读展示。"""
        with self._cv:
            locks=[]
            for resource, lock in sorted(self._locks.items()):
                owners=[{'txn_id':txn_id,'mode':'SHARED'} for txn_id in sorted(lock.shared)]
                if lock.exclusive is not None:
                    owners.append({'txn_id':lock.exclusive,'mode':'EXCLUSIVE'})
                locks.append({'resource':resource,'owners':owners})
            waiting=[{'txn_id':txn_id,**item,'blocked_by':sorted(self._waits.get(txn_id,set()))}
                     for txn_id,item in sorted(self._waiting.items())]
            return {'locks':locks,'waiting':waiting}
