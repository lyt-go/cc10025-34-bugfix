"""cold_storage 领域服务：冷存储迁移管理，接收 tier_policy 上游事件触发冷存任务。

跨模块数据流：
    tier_policy → cold_storage: 分层策略生效触发冷存储迁移
    cold_storage → hot_prefetch: 冷存完成可能触发预热回迁需求
    cold_storage → cost_tracker: 迁移任务更新成本追踪
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class ColdStorageService(BaseModuleService):
    MODULE = "cold_storage"
    ENTITY = "冷存任务"
    OPERATION_TERM = "迁移"
    LIST_FIELDS = ["任务编号", "源资产", "发布版本", "目标存储", "压缩算法", "迁移量", "预计耗时", "迁移状态", "校验摘要"]
    REQUIRED_FIELDS = ["任务编号", "源资产", "目标存储"]
    STATUS_ORDER = ["待调度", "迁移中", "已完成", "已失败"]
    ACTION_RULES = {"发起迁移": "迁移中", "确认完成": "已完成", "标记失败": "已失败"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def start_migration(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """发起冷存储迁移：记录迁移状态并更新成本追踪。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("源资产", ""))
        target_storage = str(entry.get("目标存储", ""))

        entry["status"] = "迁移中"
        entry["pending"] = True
        entry["迁移状态"] = "迁移中"

        # 写入迁移记录
        self._write_cold_storage_record(asset_id, entry)

        # 更新成本追踪
        self._update_cost_tracker(asset_id, target_storage)

        # 记录审计日志
        self._write_audit_entry(
            entity="冷存任务",
            action="发起迁移",
            target_id=str(entry.get("任务编号", "")),
            detail=f"冷存迁移 {asset_id} → {target_storage}",
        )

        return entry, f"{self.ENTITY} {entry_id} 迁移中，{asset_id}→{target_storage}"

    def complete_migration(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """确认迁移完成：记录校验摘要并检查是否需要预热回迁。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("源资产", ""))
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["迁移状态"] = "已完成"
        entry["校验摘要"] = "CRC32校验通过"

        # 更新数据产物
        self._write_cold_storage_record(asset_id, entry)

        # 如果存在预热回迁需求，触发hot_prefetch
        self._check_prefetch_need(asset_id)

        return entry, f"{self.ENTITY} {entry_id} 已完成，数据已迁移到冷存储"

    def get_migration_status(self, asset_id: str) -> dict[str, Any] | None:
        return self._read_cold_storage_record(asset_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "发起迁移":
            return self.start_migration(entry_id)
        if action == "确认完成":
            return self.complete_migration(entry_id)
        return super().run_action(entry_id, action)

    # ---------- 文件 I/O ----------
    def _write_cold_storage_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        filename = f"cold_storage_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "asset_id": asset_id,
            "task_id": str(entry.get("任务编号", "")),
            "target_storage": str(entry.get("目标存储", "")),
            "compression": str(entry.get("压缩算法", "")),
            "migration_volume": str(entry.get("迁移量", "")),
            "status": str(entry.get("迁移状态", "")),
            "checksum": str(entry.get("校验摘要", "")),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _read_cold_storage_record(self, asset_id: str) -> dict[str, Any] | None:
        filename = f"cold_storage_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------- 跨模块副作用 ----------
    def _update_cost_tracker(self, asset_id: str, target_storage: str) -> None:
        """更新成本追踪记录中的存储成本。"""
        cost_rows = store.rows("cost_tracker")
        for row in cost_rows:
            if str(row.get("关联资产")) == asset_id:
                row["存储成本"] = f"冷存:{target_storage}"
                row["status"] = "已核算"
                row["pending"] = False
                break

    def _check_prefetch_need(self, asset_id: str) -> None:
        """检查冷存资产是否有预热回迁需求。"""
        hot_rows = store.rows("hot_prefetch")
        for row in hot_rows:
            if str(row.get("冷存位置", "")).find(asset_id) >= 0 and row.get("status") == "排队中":
                return
        # 无已有预热任务则创建种子
        next_id = max((int(r.get("id", 0)) for r in hot_rows), default=0) + 1
        prefetch = {
            "id": next_id,
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "任务编号": f"HOT_-{next_id:04d}",
            "冷存位置": f"S3://glacier/{asset_id}",
            "目标分区": f"{asset_id}_hot",
            "回迁量": "待估算",
            "预计耗时": "待估算",
            "优先级": "低",
            "过期策略": "T+7",
            "任务状态": "排队中",
        }
        hot_rows.append(prefetch)

    def _write_audit_entry(self, entity: str, action: str, target_id: str,
                           detail: str) -> None:
        audit_rows = store.rows("audit_log")
        next_id = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        entry = {
            "id": next_id,
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "记录编号": f"AUDI-{next_id:04d}",
            "操作主体": entity,
            "操作类型": action,
            "操作对象": target_id,
            "操作时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "影响范围": detail,
            "审计摘要": detail,
            "归档状态": "已记录",
        }
        audit_rows.append(entry)