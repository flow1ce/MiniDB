"""Heap-file table storage with stable record identifiers."""
from .record import RID
from .slotted_page import SlottedPage
from .page import StorageError
from threading import RLock

_PAGE_LOCKS={}
_PAGE_LOCKS_GUARD=RLock()

def _page_lock(path,page_id):
    key=(path,int(page_id))
    with _PAGE_LOCKS_GUARD:
        return _PAGE_LOCKS.setdefault(key,RLock())

class TableHeap:
    """表堆：跨多个槽页管理一张表，并向上提供稳定 RID 的增删改查。"""
    def __init__(self,manager,buffer_pool=None,page_ids=None):
        self.manager=manager; self.buffer=buffer_pool; self.page_ids=list(page_ids or [])
    def _read(self,pid):
        # 经 Buffer Pool（或直接磁盘）读取负载，再封装为 SlottedPage。
        payload=self.buffer.get_page(self.manager,pid) if self.buffer else self.manager.read_page(pid)
        return SlottedPage(pid,payload)
    def _write(self,page):
        # 把槽页标脏并显式刷回，保证当前教学实现的语句级持久化。
        if self.buffer:
            self.buffer.put_page(self.manager,page.page_id,page.to_payload(),dirty=True)
            # TableHeap mutations are durable at statement completion. The
            # buffer still tracks the frame as clean after this explicit flush.
            self.buffer.flush_page(self.manager,page.page_id)
        else: self.manager.write_page(page.page_id,page.to_payload())
    def insert(self,row):
        """在已有页中寻找空间；都放不下时分配新页并返回新行 RID。"""
        with _page_lock(self.manager.path,-1):
            for pid in self.page_ids:
                with _page_lock(self.manager.path,pid):
                    page=self._read(pid)
                    try: slot=page.insert(row); self._write(page); return RID(pid,slot)
                    except StorageError:
                        continue
            pid=self.manager.allocate_page(); self.page_ids.append(pid); page=SlottedPage(pid); slot=page.insert(row); self._write(page); return RID(pid,slot)
    def get(self,rid): return self._read(rid.page_id).get(rid.slot_id)
    def update(self,rid,row):
        with _page_lock(self.manager.path,rid.page_id):
            page=self._read(rid.page_id); page.update(rid.slot_id,row); self._write(page)
    def restore(self,rid,row):
        """事务回滚时在原 RID 恢复一条被删除的记录。"""
        with _page_lock(self.manager.path,rid.page_id):
            page=self._read(rid.page_id)
            if rid.page_id not in self.page_ids: self.page_ids.append(rid.page_id)
            if rid.slot_id<0 or rid.slot_id>=len(page.slots):
                raise StorageError('record slot is out of range',rid.page_id)
            page.slots[rid.slot_id]={'value':row,'deleted':False}; self._write(page)
    def delete(self,rid):
        # 删除指定槽；完全空的数据页可回收，但空表至少保留一个页。
        with _page_lock(self.manager.path,rid.page_id):
            page=self._read(rid.page_id); page.delete(rid.slot_id)
            self._write(page)
            if not page.live() and len(self.page_ids)>1:
                # Reclaim completely empty data pages while retaining one page for
                # an empty table.  The caller persists the updated page list.
                self.page_ids=[p for p in self.page_ids if p!=rid.page_id]
                if self.buffer: self.buffer.delete_page(self.manager,rid.page_id)
                else: self.manager.release_page(rid.page_id)
    def scan(self,columns=None):
        """顺序遍历表的全部页和有效槽，逐条产出 (RID, row)。"""
        for pid in list(self.page_ids):
            page=self._read(pid)
            for slot,row in page.live():
                if columns:
                    projected={k:row.get(k) for k in columns}
                    if '_id' in row: projected['_id']=row['_id']
                    yield RID(pid,slot),projected
                else: yield RID(pid,slot),row
    def page_count(self): return len(self.page_ids)

__all__=['TableHeap']
