"""Fixed-size page and disk management."""
from __future__ import annotations
import json, os, struct
from threading import RLock

PAGE_SIZE = 4096

class StorageError(Exception):
    """页文件层错误，可附带发生问题的 page_id。"""
    def __init__(self, message, page_id=None):
        self.page_id = page_id
        super().__init__(f"StorageError{f' (page {page_id})' if page_id is not None else ''}: {message}")

class PageManager:
    """固定 4KB 页文件管理器：负责页分配、回收以及按页号随机读写。"""
    def __init__(self, path, page_size=PAGE_SIZE):
        self.path=path; self.page_size=page_size; self.meta_path=path+'.free'; self._lock=RLock()
        os.makedirs(os.path.dirname(path) or '.',exist_ok=True); open(path,'ab').close(); self._free=self._load_free()
    def _load_free(self):
        # 从 .free 辅助文件恢复可复用页号列表。
        if not os.path.exists(self.meta_path): return []
        try:
            with open(self.meta_path,encoding='utf8') as f: return sorted({int(x) for x in json.load(f) if int(x)>=0})
        except Exception as e: raise StorageError(f'cannot load free-page list: {e}')
    def _save_free(self):
        tmp=self.meta_path+'.tmp'
        with open(tmp,'w',encoding='utf8') as f: json.dump(self._free,f)
        os.replace(tmp,self.meta_path)
    def page_count(self): return os.path.getsize(self.path)//self.page_size
    count=page_count
    def allocate_page(self):
        """优先复用空闲页；没有空闲页时扩展数据文件并初始化新页。"""
        with self._lock:
            reused=bool(self._free)
            pid=self._free.pop(0) if reused else self.page_count()
            if reused: self._save_free()
            elif pid==self.page_count():
                with open(self.path,'ab') as f: f.write(bytes(self.page_size)); f.flush(); os.fsync(f.fileno())
            self.write_page(pid,[]); return pid
    def release_page(self,page_id):
        """把页清空并登记为空闲页，后续 allocate_page 可再次利用。"""
        with self._lock:
            if page_id<0 or page_id>=self.page_count(): raise StorageError('page is out of range',page_id)
            if page_id not in self._free: self._free.append(page_id); self._free.sort(); self._save_free()
            self.write_page(page_id,[])
    def read_page(self,page_id):
        """定位并读取固定页，解析“4 字节长度 + JSON 负载”的物理格式。"""
        with self._lock:
            if page_id<0 or page_id>=self.page_count(): raise StorageError('page is out of range',page_id)
            with open(self.path,'rb') as f: f.seek(page_id*self.page_size); raw=f.read(self.page_size)
            if len(raw)!=self.page_size: raise StorageError('short page read',page_id)
            size=struct.unpack('>I',raw[:4])[0]
            if size>self.page_size-4: raise StorageError('invalid payload length',page_id)
            try: return json.loads(raw[4:4+size].decode('utf8')) if size else []
            except Exception as e: raise StorageError(f'corrupt page: {e}',page_id)
    def write_page(self,page_id,data):
        """将数据编码进已分配页，检查越界/溢出并同步到磁盘。"""
        with self._lock:
            if page_id<0: raise StorageError('negative page id',page_id)
            if page_id>=self.page_count(): raise StorageError('page is out of range; allocate it first',page_id)
            raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode('utf8')
            if len(raw)>self.page_size-4: raise StorageError('page payload overflow',page_id)
            encoded=struct.pack('>I',len(raw))+raw+bytes(self.page_size-4-len(raw))
            mode='r+b' if os.path.exists(self.path) else 'w+b'
            with open(self.path,mode) as f: f.seek(page_id*self.page_size); f.write(encoded); f.flush(); os.fsync(f.fileno())
    def sync(self):
        with open(self.path,'ab') as f: f.flush(); os.fsync(f.fileno())
    read=read_page; write=write_page
    def append(self,data): pid=self.allocate_page(); self.write_page(pid,data); return pid

__all__=['PAGE_SIZE','StorageError','PageManager']
