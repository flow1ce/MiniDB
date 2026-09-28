from dataclasses import dataclass

@dataclass(frozen=True)
class RID:
    """记录物理地址：用“页号 + 槽号”唯一定位表中的一行。"""
    page_id: int
    slot_id: int
    def to_dict(self): return {'page_id': self.page_id, 'slot_id': self.slot_id}

__all__ = ['RID']
