"""asset_catalog领域服务：资产编目、发布、归档和数据资产管理。

跨模块数据流：
    asset_catalog → tier_policy → cold_storage → hot_prefetch: 编目→分层→冷存→预热
    asset_catalog → lineage_graph → change_notice: 编目→血缘→变更通知
    asset_catalog → schema_registry → version_snapshot: 编目→Schema→快照
    asset_catalog → label_tag → access_gate: 编目→标签→访问网关
    asset_catalog → retention_rule → reclaim_job: 编目→保留→回收
    asset_catalog → cost_tracker: 编目→成本追踪
    asset_catalog → quality_report: 编目→质量报告
    asset_catalog → compliance_check → audit_log: 编目→合规检查→审计日志
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


def _service_for(module: str):
    """延迟导入服务定位器，避免 services 包初始化阶段的循环导入。"""
    from app.services import service_for
    return service_for(module)


class AssetCatalogService(BaseModuleService):
    MODULE = "asset_catalog"
    ENTITY = "数据资产"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["资产编号", "资产名称", "所属域", "资产类型", "负责人", "创建时间", "最近更新", "发布版本", "资产描述"]
    REQUIRED_FIELDS = ["资产编号", "资产名称", "所属域"]
    STATUS_ORDER = ["草稿", "待审核", "已发布", "已归档"]
    ACTION_RULES = {"提交审核": "待审核", "确认发布": "已发布", "标记归档": "已归档", "提交新版本": "待审核"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)
        self._snapshot_root = os.path.join(os.path.dirname(__file__), "..", "data", "snapshots")
        os.makedirs(self._snapshot_root, exist_ok=True)

    def publish_asset(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """发布资产并写入asset数据产物，触发下游消费链。

        同一发布版本重复确认（含丢失回执后的重发）按幂等处理：不再重复生成
        分层策略种子与发布凭据，只有真正的新版本才走新的确认流程。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if entry["status"] not in ("待审核", "已发布"):
            return None, f"{self.ENTITY} {entry_id} 必须先审核通过才能发布"

        asset_id = str(entry.get("资产编号", f"ASSET-{entry_id}"))
        asset_name = str(entry.get("资产名称", ""))
        domain = str(entry.get("所属域", ""))
        version = str(entry.get("发布版本", "v1"))

        credential_svc = _service_for("publish_credential")
        existing_cred = credential_svc.find_credential(asset_id, version)
        if existing_cred is not None:
            # 同版本重发/重复确认：只刷新资产产物时间戳与审计，绝不重复播种
            self._write_json(f"{asset_id}.json", {
                "asset_id": asset_id,
                "asset_name": asset_name,
                "asset_type": str(entry.get("资产类型", "")),
                "owner": str(entry.get("负责人", "")),
                "domain": domain,
                "description": str(entry.get("资产描述", "")),
                "published_at": datetime.now(timezone.utc).isoformat(),
                "version": version,
                "status": "已发布",
                "idempotent_resend": True,
                "credential": existing_cred.get("凭据编号", ""),
            })
            entry["最近更新"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry["status"] = "已发布"
            entry["pending"] = False
            self._write_audit_entry(
                entity="数据资产",
                action="发布重发(幂等拦截)",
                target_id=asset_id,
                detail=(f"资产 {asset_name} 版本 {version} 重复确认发布，复用凭据 "
                        f"{existing_cred.get('凭据编号', '')}，未重复生成种子"),
            )
            return entry, (f"{self.ENTITY} {entry_id} 版本 {version} 已存在发布凭据，"
                           "重复发布按幂等处理，未重复生成分层种子")

        # 写入数据产物
        asset_record = {
            "asset_id": asset_id,
            "asset_name": asset_name,
            "asset_type": str(entry.get("资产类型", "")),
            "owner": str(entry.get("负责人", "")),
            "domain": domain,
            "description": str(entry.get("资产描述", "")),
            "published_at": datetime.now(timezone.utc).isoformat(),
            "version": version,
            "status": "已发布",
        }
        self._write_json(f"{asset_id}.json", asset_record)

        # 更新内存状态
        entry["最近更新"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry["status"] = "已发布"
        entry["pending"] = False

        # 触发下游模块: tier_policy（按发布版本创建唯一分层策略种子与发布凭据）
        self._seed_tier_policy(asset_id, domain, version)

        # 触发下游模块: cost_tracker（创建成本追踪种子）
        self._seed_cost_tracker(asset_id, domain)

        # 触发下游模块: lineage_graph（注册资产血缘节点）
        self._seed_lineage_node(asset_id, asset_name, domain)

        # 记录审计日志
        self._write_audit_entry(
            entity="数据资产",
            action="发布",
            target_id=asset_id,
            detail=f"资产 {asset_name} 发布版本 {version}，所属域 {domain}",
        )

        return entry, f"{self.ENTITY} {entry_id} 版本 {version} 已发布并触发下游消费链"

    def submit_new_version(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """已发布资产提交真正的新版本：版本号递增并回到待审核，独立走确认流程。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if entry["status"] != "已发布":
            return None, f"{self.ENTITY} {entry_id} 仅已发布资产可以提交新版本"

        current = str(entry.get("发布版本", "v1"))
        try:
            current_no = int(current.lstrip("vV"))
        except ValueError:
            current_no = 1
        new_version = f"v{current_no + 1}"

        entry["发布版本"] = new_version
        entry["status"] = "待审核"
        entry["pending"] = True
        entry["最近更新"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        self._write_audit_entry(
            entity="数据资产",
            action="提交新版本",
            target_id=str(entry.get("资产编号", "")),
            detail=f"资产 {entry.get('资产名称', '')} 由 {current} 提交新版本 {new_version}，等待独立发布确认",
        )
        return entry, f"{self.ENTITY} {entry_id} 已提交新版本 {new_version}，需独立确认发布"

    def archive_asset(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """归档资产：创建版本快照并更新归档状态。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        entry["status"] = "已归档"
        entry["pending"] = False
        entry["最近更新"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 创建归档快照
        self._create_snapshot(entry)

        # 停用关联的访问策略
        self._revoke_access_gates(str(entry.get("资产编号", "")))

        # 记录审计
        self._write_audit_entry(
            entity="数据资产",
            action="归档",
            target_id=str(entry.get("资产编号", "")),
            detail=f"资产 {entry.get('资产名称', '')} 已归档",
        )

        return entry, f"{self.ENTITY} {entry_id} 已归档"

    def get_asset_snapshot(self, asset_id: str) -> dict[str, Any] | None:
        """读取资产数据产物。"""
        return self._read_json(f"{asset_id}.json")

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """重载：发布操作触发跨模块下游消费，归档操作触发快照创建。"""
        if action == "确认发布":
            return self.publish_asset(entry_id)
        if action == "标记归档":
            return self.archive_asset(entry_id)
        if action == "提交新版本":
            return self.submit_new_version(entry_id)
        return super().run_action(entry_id, action)

    # ---------- 文件 I/O ----------
    def _read_json(self, filename: str) -> dict[str, Any] | None:
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, filename: str, data: dict[str, Any]) -> None:
        path = os.path.join(DATA_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    # ---------- 跨模块副作用 ----------
    def _seed_tier_policy(self, asset_id: str, domain: str, version: str = "v1") -> None:
        """为已发布资产按发布版本生成唯一分层策略种子与唯一发布凭据。

        去重键为（目标资产, 发布版本）：同版本重发不再产生第二份种子；
        不同发布版本各自生成独立种子、独立凭据，分别走自己的确认流程。
        """
        credential_svc = _service_for("publish_credential")
        if credential_svc.find_credential(asset_id, version) is not None:
            return

        tier_rows = store.rows("tier_policy")
        next_id = max((int(r.get("id", 0)) for r in tier_rows), default=0) + 1
        policy_code = f"TIER-{next_id:04d}"
        policy = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "策略编号": policy_code,
            "目标资产": asset_id,
            "当前层级": "HOT",
            "迁移条件": "访问频次<5次/天",
            "目标层级": "COLD",
            "冷却天数": "90",
            "访问频次阈值": "5",
            "策略版本": "v1",
            "发布版本": version,
            "关联凭据": "",
            "编辑人": "系统播种",
            "审批来源": "资产发布种子",
        }
        tier_rows.append(policy)

        credential = credential_svc.issue_credential(
            asset_id=asset_id,
            version=version,
            seed_policy_code=policy_code,
            domain=domain,
        )
        policy["关联凭据"] = str(credential.get("凭据编号", ""))

    def _seed_cost_tracker(self, asset_id: str, domain: str) -> None:
        """为已发布资产创建成本追踪种子。"""
        cost_rows = store.rows("cost_tracker")
        next_id = max((int(r.get("id", 0)) for r in cost_rows), default=0) + 1
        record = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "记录编号": f"COST-{next_id:04d}",
            "关联资产": asset_id,
            "存储成本": "0.00",
            "计算成本": "0.00",
            "传输成本": "0.00",
            "统计周期": datetime.now(timezone.utc).strftime("%Y-%m"),
            "趋势方向": "持平",
            "异常标记": "无",
        }
        cost_rows.append(record)

    def _seed_lineage_node(self, asset_id: str, asset_name: str, domain: str) -> None:
        """注册资产为血缘图谱节点。"""
        lineage_rows = store.rows("lineage_graph")
        next_id = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
        record = {
            "id": next_id,
            "status": "已注册",
            "pending": False,
            "abnormal": False,
            "记录编号": f"LINE-{next_id:04d}",
            "上游资产": "SOURCE",
            "下游资产": asset_id,
            "依赖类型": "数仓入湖",
            "加工逻辑": f"资产编目自动注册:{asset_name}",
            "生效时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "变更SHA": f"ac-{int(time.time())}",
            "血缘状态": "已生效",
        }
        lineage_rows.append(record)

    def _create_snapshot(self, entry: dict[str, Any]) -> None:
        """创建归档资产的版本快照。"""
        snap_rows = store.rows("version_snapshot")
        next_id = max((int(r.get("id", 0)) for r in snap_rows), default=0) + 1
        snap = {
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"VERS-{next_id:04d}",
            "关联资产": str(entry.get("资产编号", "")),
            "快照时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "数据量": "0MB",
            "快照类型": "归档快照",
            "保留天数": "365",
            "依赖快照": "",
            "快照状态": "已完成",
        }
        snap_rows.append(snap)

    def _revoke_access_gates(self, asset_id: str) -> None:
        """归档时撤销关联的访问策略。"""
        access_rows = store.rows("access_gate")
        for row in access_rows:
            if str(row.get("目标资产")) == asset_id and row.get("status") != "已撤销":
                row["status"] = "已撤销"
                row["pending"] = False

    def _write_audit_entry(self, entity: str, action: str, target_id: str,
                           detail: str) -> None:
        """写入审计日志。"""
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