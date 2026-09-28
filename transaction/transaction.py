from dataclasses import dataclass, field
from enum import Enum
import itertools

# 事务状态机与本项目暴露的三种隔离级别。
class TransactionState(str,Enum): ACTIVE='ACTIVE'; COMMITTED='COMMITTED'; ABORTED='ABORTED'
class IsolationLevel(str,Enum): READ_COMMITTED='READ_COMMITTED'; REPEATABLE_READ='REPEATABLE_READ'; SERIALIZABLE='SERIALIZABLE'
_ids=itertools.count(1)
@dataclass
class Transaction:
    """单个事务的运行状态：事务号、隔离级别、持有锁和最后日志序号。"""
    isolation: IsolationLevel=IsolationLevel.READ_COMMITTED
    txn_id:int=field(default_factory=lambda:next(_ids))
    state:TransactionState=TransactionState.ACTIVE
    locks:set=field(default_factory=set)
    last_lsn:int=0
    session_id:str=None
    undo:list=field(default_factory=list)
    def commit(self): self.state=TransactionState.COMMITTED
    def abort(self): self.state=TransactionState.ABORTED
