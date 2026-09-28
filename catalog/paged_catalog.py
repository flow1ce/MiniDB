"""Page-backed system catalog.

Page 0 is a catalog header containing the IDs of catalog-record pages. The
remaining pages store serialized table metadata. A legacy catalog.json is
read once and migrated when present.
"""
from __future__ import annotations
import json, os
from dataclasses import dataclass, field, asdict
from threading import RLock
from storage.page import PageManager, StorageError

@dataclass
class CatalogTableMeta:
    """单表元数据：表名、列定义、数据页列表和索引描述。"""
    name: str
    columns: list
    pages: list = field(default_factory=list)
    indexes: dict = field(default_factory=dict)

class PagedCatalog:
    """分页系统目录：持久化表结构、页列表、索引、用户和权限。"""
    def __init__(self,path,error_cls=RuntimeError):
        self.path=path; self.error_cls=error_cls; self.manager=PageManager(path); self.tables={}; self.users={'admin'}; self.privileges={}; self._lock=RLock(); self._load()
    def _fail(self,msg): raise self.error_cls(msg)
    def _load(self):
        """从 catalog.dat 恢复元数据；首次启动可迁移旧 catalog.json。"""
        legacy=self.path.rsplit('.',1)[0]+'.json'
        if self.manager.page_count()==0 and os.path.exists(legacy):
            try:
                with open(legacy,encoding='utf8') as f: data=json.load(f)
                self.tables={k:CatalogTableMeta(**v) for k,v in data.items()}; self.save(); return
            except Exception as e: self._fail(f'cannot migrate catalog: {e}')
        if self.manager.page_count()==0:
            self.manager.allocate_page(); self.save(); return
        try:
            header=self.manager.read_page(0); pages=header.get('table_pages',[]) if isinstance(header,dict) else []
            # Page 0 is reserved for the catalog header.  Older versions had
            # a migration bug which could write it into table_pages itself.
            # If the legacy JSON is still available, repair the catalog from
            # that authoritative source instead of trying to unpack the
            # header dictionary as table metadata.
            invalid_pages=(
                not isinstance(header,dict)
                or header.get('kind') != 'system_catalog_header'
                or any(not isinstance(pid,int) or pid <= 0 or pid >= self.manager.page_count() for pid in pages)
            )
            if invalid_pages and os.path.exists(legacy):
                with open(legacy,encoding='utf8') as f: data=json.load(f)
                self.tables={k:CatalogTableMeta(**v) for k,v in data.items()}
                self.users={'admin'}
                self.privileges={}
                self.save()
                return
            self.users=set((header or {}).get('users',['admin']))
            self.privileges=dict((header or {}).get('privileges',{}))
            for pid in pages:
                for item in self.manager.read_page(pid):
                    meta=CatalogTableMeta(**item); self.tables[meta.name.lower()]=meta
        except Exception as e: self._fail(f'cannot load catalog pages: {e}')
    def save(self):
        with self._lock: return self._save_unlocked()
    def _save_unlocked(self):
        """重写 Catalog 数据页，并在第 0 页保存目录页号、用户和权限头信息。"""
        items=[asdict(v) for v in self.tables.values()]
        chunks=[]; current=[]; size=0
        for item in items:
            encoded=len(json.dumps(item,ensure_ascii=False,separators=(',',':')).encode('utf8'))
            if current and size+encoded>3500: chunks.append(current); current=[]; size=0
            current.append(item); size+=encoded
        if current or not chunks: chunks.append(current)
        # Reserve page 0 for the catalog header.  This is especially
        # important during legacy JSON migration, when the file may not have
        # any pages yet.
        if self.manager.page_count()==0:
            self.manager.allocate_page()
        header=self.manager.read_page(0)
        old=list(header.get('table_pages',[])) if isinstance(header,dict) else []
        for pid in old:
            if pid == 0:
                continue
            try: self.manager.release_page(pid)
            except StorageError: pass
        pages=[]
        for chunk in chunks:
            pid=self.manager.allocate_page(); self.manager.write_page(pid,chunk); pages.append(pid)
        self.manager.write_page(0,{'kind':'system_catalog_header','table_pages':pages,'users':sorted(self.users),'privileges':self.privileges})
    def get(self,name):
        # 表名按小写键查找，实现不区分大小写的表定位。
        meta=self.tables.get(name.lower())
        if not meta: self._fail(f"table '{name}' does not exist")
        return meta
    def create(self,name,columns):
        # 校验表名/列名重复后创建内存元数据，并立即持久化 Catalog。
        key=name.lower()
        if key in self.tables: self._fail(f"table '{name}' already exists")
        if len({c['name'].lower() for c in columns})!=len(columns): self._fail('duplicate column name')
        meta=CatalogTableMeta(name,columns); self.tables[key]=meta; self.save(); return meta

__all__=['PagedCatalog','CatalogTableMeta']
