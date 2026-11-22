"""publish_credential 领域服务：分层种子发布凭据。

同一次资产发布（目标资产 + 发布版本）在全平台只允许存在一份有效凭据，
tier_policy 的分层草稿激活、冲突裁决与下游迁移派发都以凭据为唯一核对入口。

跨模块数据流：
    asset_catalog → publish_credential: 资产发布按版本写入唯一凭据与唯一种子
    tier_policy → publish_credential: 草稿激活前核对凭据资格，裁决后回写采用结果
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 凭据状态：待确认（种子已写、等待草稿激活）→ 待裁决（多草稿冲突）→ 已裁决 / 已失效
STATUS_PENDING = "待确认"
STATUS_DISPUTED = "待裁决"
STATUS_DECIDED = "已裁决"
STATUS_VOID = "已失效"


class PublishCredentialService(BaseModuleService):
    MODULE = "publish_credential"
    ENTITY = "分层种子发布凭据"
    OPERATION_TERM = "确认"
    LIST_FIELDS = [
        "凭据编号", "目标资产", "发布版本", "种子策略", "凭据状态",
        "采用草稿", "采用期限", "审批来源",
    ]
    REQUIRED_FIELDS = ["凭据编号", "目标资产", "发布版本"]
    STATUS_ORDER = [STATUS_PENDING, STATUS_DISPUTED, STATUS_DECIDED, STATUS_VOID]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS = []

    def find_credential(self, asset_id: str, version: str) -> dict[str, Any] | None:
        """按资产与发布版本定位唯一凭据。"""
        for row in store.rows(self.MODULE):
            if str(row.get("目标资产")) == asset_id and str(row.get("发布版本")) == version:
                return row
        return None

    def issue_credential(
        self,
        asset_id: str,
        version: str,
        seed_policy_code: str,
        domain: str = "",
    ) -> dict[str, Any]:
        """为一次发布写入唯一分层种子发布凭据（调用前需确认不存在同键凭据）。"""
        rows = store.rows(self.MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        credential = {
            "id": next_id,
            "status": STATUS_PENDING,
            "pending": True,
            "abnormal": False,
            "凭据编号": f"CRED-{next_id:04d}",
            "目标资产": asset_id,
            "发布版本": version,
            "所属域": domain,
            "种子策略": seed_policy_code,
            "凭据状态": STATUS_PENDING,
            "采用草稿": "",
            "采用期限": "",
            "审批来源": "",
            "处置理由": "",
            "创建时间": now,
            "最近更新": now,
        }
        rows.append(credential)
        return credential

    def mark_disputed(self, credential: dict[str, Any], reason: str) -> None:
        """多份有效草稿争抢激活时，把凭据置为待裁决，阻断一切派发。"""
        credential["status"] = STATUS_DISPUTED
        credential["pending"] = True
        credential["凭据状态"] = STATUS_DISPUTED
        credential["处置理由"] = reason
        credential["最近更新"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def mark_decided(
        self,
        credential: dict[str, Any],
        adopted_draft_code: str,
        adopted_term: str,
        approval_source: str,
        reason: str,
    ) -> None:
        """裁决完成：回写采用的草稿、期限数据与审批来源。"""
        credential["status"] = STATUS_DECIDED
        credential["pending"] = False
        credential["凭据状态"] = STATUS_DECIDED
        credential["采用草稿"] = adopted_draft_code
        credential["采用期限"] = adopted_term
        credential["审批来源"] = approval_source
        credential["处置理由"] = reason
        credential["最近更新"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def mark_void(self, credential: dict[str, Any], reason: str) -> None:
        """暂缓裁决：本版本不派发，凭据失效但处置理由留痕。"""
        credential["status"] = STATUS_VOID
        credential["pending"] = False
        credential["凭据状态"] = STATUS_VOID
        credential["处置理由"] = reason
        credential["最近更新"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def eligible_drafts(self, credential: dict[str, Any]) -> list[dict[str, Any]]:
        """从分层策略表核对该凭据下当前仍有激活资格的草稿。

        资格条件：同一目标资产、同一发布版本、关联本凭据且状态仍为「草稿」。
        已合并 / 已暂缓 / 已生效的历史草稿不再具备资格，也不会显示成可发起任务。
        """
        asset_id = str(credential.get("目标资产"))
        version = str(credential.get("发布版本"))
        cred_code = str(credential.get("凭据编号"))
        tier_rows = store.rows("tier_policy")
        return [
            row for row in tier_rows
            if str(row.get("目标资产")) == asset_id
            and str(row.get("发布版本")) == version
            and str(row.get("关联凭据")) == cred_code
            and row.get("status") == "草稿"
        ]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """凭据只能由资产发布流程按（资产, 版本）唯一签发，拒绝手工登记。"""
        return None, ["分层种子发布凭据由资产发布流程自动签发，不支持手工登记"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """凭据状态只允许由 tier_policy 裁决流程驱动，不接受手工通用动作。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        return None, "分层种子发布凭据由分层策略激活/裁决流程自动维护，不支持手工动作"
