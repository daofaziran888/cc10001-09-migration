"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据，并支持联合存储表。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import json
import os
from typing import Any

from app.seed import SEED_ROWS

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


class Store:
    def __init__(self) -> None:
        self._data_dir = _DATA_DIR
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 联合存储表：跨模块数据流的持久化产物
        self._joint: dict[str, list[dict[str, Any]]] = {
            "pipeline_state": [],      # 流水线执行状态与产物
            "audit_records": [],       # 质量审计记录（旧版 JSON 数组）
            "sync_positions": [],      # 增量同步水位
            "dispatch_logs": [],       # 数据分发日志
            "proxy_assignments": [],   # 代理分配记录
            "cookie_rotations": [],    # 凭证轮换记录
            "scheduler_triggers": [],  # 调度触发记录
            # 质量审计版本化迁移产物
            "audit_envelopes": [],     # 带版本号的质量审计记录信封数据集
            "audit_migrations": [],    # 迁移批次日志（整组一次提交的凭证）
        }
        self._load_joint()

    def set_data_dir(self, path: str) -> None:
        """重定向联合表落盘目录（测试隔离用），并重新加载该目录下的数据。"""
        self._data_dir = path
        for key in self._joint:
            self._joint[key] = []
        self._load_joint()

    def _joint_path(self, key: str) -> str:
        os.makedirs(self._data_dir, exist_ok=True)
        return os.path.join(self._data_dir, f"{key}.json")

    def _load_joint(self) -> None:
        for key in self._joint:
            path = self._joint_path(key)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        self._joint[key] = json.load(f)
                except Exception:
                    self._joint[key] = []

    def _save_joint(self, key: str) -> None:
        path = self._joint_path(key)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._joint[key], f, ensure_ascii=False, indent=2)

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

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
        # 跨模块数据流统计
        pipeline_runs = len(self._joint.get("pipeline_state", []))
        audit_count = len(self._joint.get("audit_records", []))
        dispatch_count = len(self._joint.get("dispatch_logs", []))
        cards.append({"label": "流水线执行", "value": pipeline_runs})
        cards.append({"label": "审计记录", "value": audit_count})
        cards.append({"label": "分发日志", "value": dispatch_count})
        return {"cards": cards, "modules": modules}

    # 联合存储表操作
    def joint_rows(self, key: str) -> list[dict[str, Any]]:
        return self._joint.setdefault(key, [])

    def joint_append(self, key: str, record: dict[str, Any]) -> None:
        self._joint.setdefault(key, []).append(record)
        self._save_joint(key)

    def joint_find(self, key: str, record_id: int) -> dict[str, Any] | None:
        for row in self.joint_rows(key):
            if int(row.get("id", 0)) == record_id:
                return row
        return None

    def joint_clear(self, key: str) -> None:
        self._joint[key] = []
        self._save_joint(key)

    def joint_replace(self, key: str, rows: list[dict[str, Any]]) -> None:
        """整组一次提交：用新列表整体替换某张联合表并落盘（迁移批次的原子写入点）。"""
        self._joint[key] = [dict(row) for row in rows]
        self._save_joint(key)

    def joint_save(self, key: str) -> None:
        """对已在内存中修改的联合表显式落盘。"""
        self._save_joint(key)

    def joint_all(self) -> dict[str, list[dict[str, Any]]]:
        return dict(self._joint)


store = Store()
