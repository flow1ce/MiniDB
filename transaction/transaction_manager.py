from .transaction import Transaction, IsolationLevel, TransactionState
from .lock_manager import LockManager
from .log_manager import LogManager

class TransactionManager:
    """事务生命周期协调器：统一串联事务对象、锁释放和 WAL 边界。"""
    def __init__(self, wal_path):
        self.lock_manager=LockManager(); self.log_manager=LogManager(wal_path); self.active={}
    def begin(self,isolation=IsolationLevel.READ_COMMITTED):
        # 创建活动事务并先写 BEGIN 日志。
        tx=Transaction(isolation); self.active[tx.txn_id]=tx; self.log_manager.append(tx.txn_id,'BEGIN'); return tx
    def commit(self,tx):
        # 写 COMMIT、更新状态、释放全部锁；无活动事务时检查点并截断 WAL。
        self.log_manager.append(tx.txn_id,'COMMIT'); tx.commit(); self.lock_manager.release_all(tx.txn_id); self.active.pop(tx.txn_id,None)
        marker=self.log_manager.checkpoint(self.active)
        if not self.active: self.log_manager.truncate_before(marker.lsn)
    def abort(self,tx):
        # 写 ABORT、标记事务终止并释放全部锁。
        self.log_manager.append(tx.txn_id,'ABORT'); tx.abort(); self.lock_manager.release_all(tx.txn_id); self.active.pop(tx.txn_id,None)

__all__=['TransactionManager']
