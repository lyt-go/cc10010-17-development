"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    """内存数据仓库。

    时间线节点和基线状态必须一起成功或一起失败，因此这里提供一个最小事务：
    进入时深拷贝全部表并加锁，异常时整体回滚。内部工作表的表名以下划线开头，
    不出现在运营概览里。
    """

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()
        self._tx_seq = 0

    def module_names(self) -> list[str]:
        with self._lock:
            return sorted(name for name in self._tables if not name.startswith("_"))

    def rows(self, module: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self.rows(module):
                if int(row.get("id", 0)) == entry_id:
                    return row
            return None

    @contextmanager
    def transaction(self) -> Iterator[str]:
        """给时间线与基线状态的联合落库提供同事务语义。

        yield 出来的是事务号，写入的节点、基线和回执都会带上同一个号，
        便于回查这次计算结果是否属于同一次提交。
        """
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            self._tx_seq += 1
            tx_id = f"TX-{datetime.now():%Y%m%d%H%M%S}-{self._tx_seq:04d}"
            try:
                yield tx_id
            except BaseException:
                self._tables = snapshot
                raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
