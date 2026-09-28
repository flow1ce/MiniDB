from dataclasses import dataclass,asdict
import json,os,threading,time
from typing import Optional

@dataclass
class LogRecord:
    """一条 WAL：记录 LSN、事务、行地址以及修改前后镜像。"""
    lsn:int; txn_id:int; action:str; page_id:Optional[int]=None; slot_id:Optional[int]=None; before:object=None; after:object=None; prev_lsn:Optional[int]=None; timestamp:float=0.0
    def to_dict(self): return asdict(self)

class LogManager:
    """预写日志管理器：追加并强制刷盘日志，提供恢复读取、检查点和截断。"""
    def __init__(self,path): self.path=path; self._lock=threading.RLock(); self._lsn=0; os.makedirs(os.path.dirname(path) or '.',exist_ok=True); self._load_lsn()
    def _load_lsn(self):
        # 启动时扫描已有日志，恢复下一条日志应使用的 LSN。
        if not os.path.exists(self.path): return
        try:
            with open(self.path,encoding='utf8') as f:
                for line in f:
                    if line.strip(): self._lsn=max(self._lsn,int(json.loads(line)['lsn']))
        except Exception: self._lsn=0
    def append(self,txn_id,action,**kwargs):
        """追加 JSON 行日志并 fsync，确保记录真正进入持久化文件。"""
        with self._lock:
            self._lsn+=1; rec=LogRecord(self._lsn,txn_id,action,timestamp=time.time(),**kwargs)
            with open(self.path,'a',encoding='utf8') as f: f.write(json.dumps(rec.to_dict(),ensure_ascii=False)+'\n'); f.flush(); os.fsync(f.fileno())
            return rec
    def records(self):
        # 将 wal.log 中每行 JSON 还原为 LogRecord 列表，供恢复和诊断使用。
        if not os.path.exists(self.path): return []
        with open(self.path,encoding='utf8') as f: return [LogRecord(**json.loads(line)) for line in f if line.strip()]
    def active_transactions(self):
        """找出已有 BEGIN 但尚无 COMMIT/ABORT 的活动事务。"""
        active=set()
        for rec in self.records():
            if rec.txn_id==0: continue
            if rec.action=='BEGIN': active.add(rec.txn_id)
            elif rec.action in ('COMMIT','ABORT'): active.discard(rec.txn_id)
        return sorted(active)
    def checkpoint(self,active_txns=()):
        """追加持久化检查点，记录当前活动事务以及检查点自身 LSN。"""
        with self._lock:
            marker_lsn=self._lsn+1
            return self.append(0,'CHECKPOINT',after={'active_txns':list(active_txns),'last_lsn':marker_lsn})
    def truncate_before(self, lsn):
        """保留指定检查点及更新日志，通过临时文件原子替换安全截断旧 WAL。"""
        with self._lock:
            records=[r for r in self.records() if r.lsn>=int(lsn)]
            tmp=self.path+'.tmp'
            with open(tmp,'w',encoding='utf8') as f:
                for rec in records: f.write(json.dumps(rec.to_dict(),ensure_ascii=False)+'\n')
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp,self.path)
    def flush(self):
        # 数据库关闭时显式刷新 WAL 文件缓冲区。
        with self._lock:
            if os.path.exists(self.path):
                with open(self.path,'a',encoding='utf8') as f: f.flush(); os.fsync(f.fileno())
