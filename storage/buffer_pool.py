"""Buffer pool with explicit page pinning, dirty tracking and flushing."""
from __future__ import annotations
from collections import OrderedDict
from threading import RLock

class BufferFrame:
    """单个缓存帧：保存页面内容、脏标记、Pin 计数和淘汰策略时间信息。"""
    def __init__(self,page_id,data,manager):
        self.page_id=page_id; self.data=data; self.manager=manager; self.dirty=False; self.pin_count=0; self.load_sequence=0; self.last_access=0

class BufferPool:
    """磁盘页缓存：支持 LRU/FIFO、Pin、脏页刷回以及命中/I/O 统计。"""
    def __init__(self,capacity=16,policy='LRU'):
        self.capacity=max(1,int(capacity)); self.policy=policy.upper()
        if self.policy not in ('LRU','FIFO'): raise ValueError('policy must be LRU or FIFO')
        self.frames=OrderedDict(); self.hits=self.misses=self.evictions=self.disk_reads=self.disk_writes=self.flushes=0; self._clock=0; self._lock=RLock()
    def _key(self,m,p): return (m.path,int(p))
    def _victim(self):
        # 只从未 Pin 的页面中，按 FIFO 或 LRU 规则选择淘汰对象。
        candidates=[(k,f) for k,f in self.frames.items() if f.pin_count==0]
        if not candidates: raise RuntimeError('all buffer frames are pinned')
        return min(candidates,key=lambda z:z[1].load_sequence if self.policy=='FIFO' else z[1].last_access)
    def get_page(self,manager,page_id,pin=False):
        """读取页：命中直接返回；未命中时必要地淘汰并从磁盘装入。"""
        with self._lock:
            key=self._key(manager,page_id); self._clock+=1
            if key in self.frames:
                f=self.frames[key]; self.hits+=1; f.last_access=self._clock; f.pin_count+=1 if pin else 0; return f.data
            self.misses+=1
            if len(self.frames)>=self.capacity:
                vk,v=self._victim()
                if v.dirty: v.manager.write_page(v.page_id,v.data); self.disk_writes+=1
                del self.frames[vk]; self.evictions+=1
            f=BufferFrame(page_id,manager.read_page(page_id),manager); self.disk_reads+=1; f.load_sequence=f.last_access=self._clock; f.pin_count=1 if pin else 0; self.frames[key]=f; return f.data
    def put_page(self,manager,page_id,data,dirty=True):
        """写入/更新缓存帧并标脏；空间不足时先淘汰可替换页面。"""
        with self._lock:
            key=self._key(manager,page_id); self._clock+=1; f=self.frames.get(key)
            if f is None:
                if len(self.frames)>=self.capacity:
                    vk,v=self._victim()
                    if v.dirty: v.manager.write_page(v.page_id,v.data); self.disk_writes+=1
                    del self.frames[vk]; self.evictions+=1
                f=BufferFrame(page_id,data,manager); f.load_sequence=self._clock; self.frames[key]=f
            f.data=data; f.last_access=self._clock; f.dirty=f.dirty or dirty; return data
    def unpin_page(self,manager,page_id,dirty=False):
        # 解除一次 Pin，并可同时声明该页已被修改。
        with self._lock:
            f=self.frames.get(self._key(manager,page_id))
            if f is None: return False
            f.pin_count=max(0,f.pin_count-1); f.dirty=f.dirty or dirty; return True
    def mark_dirty(self,manager,page_id):
        with self._lock:
            f=self.frames.get(self._key(manager,page_id))
            if f is None: return False
            f.dirty=True; return True
    def flush_page(self,manager,page_id):
        """将指定脏页写回磁盘并清除 dirty 标记。"""
        with self._lock:
            f=self.frames.get(self._key(manager,page_id))
            if f is None: return False
            if f.dirty: manager.write_page(page_id,f.data); f.dirty=False; self.disk_writes+=1
            self.flushes+=1; return True
    def flush_all(self):
        """关闭或显式刷新时，将所有脏页统一写回磁盘。"""
        with self._lock:
            for f in list(self.frames.values()):
                if f.dirty: f.manager.write_page(f.page_id,f.data); f.dirty=False; self.disk_writes+=1
            self.flushes+=1
    def clear(self):
        """刷回并清空全部未 Pin 帧，用于构造可控的冷缓存测试。"""
        with self._lock:
            if any(frame.pin_count > 0 for frame in self.frames.values()):
                raise RuntimeError('cannot clear buffer pool while pages are pinned')
            for frame in list(self.frames.values()):
                if frame.dirty:
                    frame.manager.write_page(frame.page_id,frame.data); self.disk_writes+=1
            self.frames.clear()
    def reset_stats(self):
        with self._lock:
            self.hits=self.misses=self.evictions=self.disk_reads=self.disk_writes=self.flushes=0
    def delete_page(self,manager,page_id):
        # 删除缓存映射并把底层页交还空闲列表；Pin 页禁止删除。
        with self._lock:
            key=self._key(manager,page_id); f=self.frames.get(key)
            if f is not None and f.pin_count>0: raise RuntimeError(f'cannot delete pinned page {page_id}')
            self.frames.pop(key,None); manager.release_page(page_id)
    # Compatibility aliases
    def get(self,manager,page_id): return self.get_page(manager,page_id)
    def put(self,manager,page_id,data): self.put_page(manager,page_id,data,True); return manager.write_page(page_id,data)
    def stats(self):
        """返回容量、命中率、I/O、淘汰、脏页和 Pin 页等观测指标。"""
        with self._lock:
            return {'capacity':self.capacity,'policy':self.policy,
                    'hits':self.hits,'misses':self.misses,'evictions':self.evictions,
                    'disk_reads':self.disk_reads,'disk_writes':self.disk_writes,
                    'flushes':self.flushes,'frames':len(self.frames),
                    'dirty_frames':sum(1 for f in self.frames.values() if f.dirty),
                    'pinned_frames':sum(1 for f in self.frames.values() if f.pin_count>0),
                    'frame_details':[{'page_id':f.page_id,'dirty':f.dirty,'pin_count':f.pin_count}
                                     for f in self.frames.values()],
                    'hit_rate':self.hits/max(1,self.hits+self.misses)}

__all__=['BufferPool','BufferFrame']
