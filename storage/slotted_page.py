"""Slot-directory page abstraction used by TableHeap."""
from __future__ import annotations
import json
from .page import PAGE_SIZE, StorageError

class SlottedPage:
    """槽目录页：在一个固定页内插入、定位、更新、删除多条记录。"""
    def __init__(self, page_id, payload=None, page_size=PAGE_SIZE):
        self.page_id=page_id; self.page_size=page_size
        if isinstance(payload,list):
            self.slots=[{'value':v,'deleted':False} for v in payload]
        elif isinstance(payload,dict) and 'slots' in payload:
            self.slots=list(payload.get('slots') or [])
        else: self.slots=[]
    def _size(self): return len(json.dumps({'slots':self.slots},ensure_ascii=False,separators=(',',':')).encode('utf8'))+4
    def has_space(self,value):
        # 估算加入新槽后的 JSON 负载是否仍可放入一个固定页。
        return self._size()+len(json.dumps({'value':value,'deleted':False},ensure_ascii=False).encode('utf8')) <= self.page_size
    def insert(self,value):
        """优先复用已删除槽，否则追加新槽并返回 slot_id。"""
        for i,s in enumerate(self.slots):
            if s.get('deleted'):
                self.slots[i]={'value':value,'deleted':False}; return i
        if not self.has_space(value): raise StorageError('page has insufficient free space',self.page_id)
        self.slots.append({'value':value,'deleted':False}); return len(self.slots)-1
    def get(self,slot_id):
        if slot_id<0 or slot_id>=len(self.slots): raise StorageError('slot is out of range',self.page_id)
        s=self.slots[slot_id]
        return None if s.get('deleted') else s.get('value')
    def update(self,slot_id,value):
        # 原位更新指定槽；若更新后页面溢出则恢复旧值并报错。
        if self.get(slot_id) is None: raise StorageError('record is deleted or missing',self.page_id)
        old=self.slots[slot_id]; self.slots[slot_id]={'value':value,'deleted':False}
        if self._size()>self.page_size: self.slots[slot_id]=old; raise StorageError('updated record does not fit page',self.page_id)
    def delete(self,slot_id):
        # 逻辑删除槽并清空值，槽位置保留供以后复用。
        if slot_id<0 or slot_id>=len(self.slots): raise StorageError('slot is out of range',self.page_id)
        self.slots[slot_id]['deleted']=True; self.slots[slot_id]['value']=None
    def to_payload(self): return {'slots':self.slots}
    def live(self): return [(i,s.get('value')) for i,s in enumerate(self.slots) if not s.get('deleted')]

__all__=['SlottedPage']
