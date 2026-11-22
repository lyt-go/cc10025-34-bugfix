"""tier_policy 领域服务：分层策略管理，接收 asset_catalog 上游发布事件并生成冷存储与预热任务。

跨模块数据流：
    asset_catalog → tier_policy: 资产发布后按发布版本自动生成唯一分层策略种子
    publish_credential → tier_policy: 草稿激活前必须通过分层种子发布凭据核对资格
    tier_policy → cold_storage: 策略生效触发且仅触发一次冷存储迁移
    tier_policy → hot_prefetch: 高访问频次触发预热回迁
    tier_policy → retention_rule: 分层策略关联保留规则

重复发布治理：
    同一次发布（目标资产 + 发布版本）只有一份有效凭据。修复前残留的多份同版
    草稿在激活时统一回到凭据核对：两份有效草稿同时激活会触发冲突暂缓而不是
    重复派发；裁决时仅一份草稿被采用并产生唯一迁移，其余草稿标记为已合并/
    已暂缓，编辑依据与处置理由全部保留并写入审计。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def _service_for(module: str):
    """延迟导入服务定位器，避免 services 包初始化阶段的循环导入。"""
    from app.services import service_for
    return service_for(module)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")

# 分层草稿的终态标记：已合并（其编辑被采用）/ 已暂缓（未采用，编辑留痕）
DRAFT_MERGED = "已合并"
DRAFT_SHELVED = "已暂缓"


class TierPolicyService(BaseModuleService):
    MODULE = "tier_policy"
    ENTITY = "分层策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略编号", "目标资产", "发布版本", "当前层级", "迁移条件",
                   "目标层级", "冷却天数", "访问频次阈值", "策略版本"]
    REQUIRED_FIELDS = ["策略编号", "目标资产", "当前层级"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用", DRAFT_MERGED, DRAFT_SHELVED]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用策略": "已停用",
                    "合并草稿": DRAFT_MERGED, "暂缓草稿": DRAFT_SHELVED}
    NEGATIVE_ACTIONS = ["停用策略"]

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)
        self._lineage_dir = os.path.join(os.path.dirname(__file__), "..", "data", "lineage")
        os.makedirs(self._lineage_dir, exist_ok=True)

    # ---------- 激活入口 ----------
    def activate_policy(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """策略生效：先经发布凭据核对资格，再决定唯一派发或冲突暂缓。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        if entry.get("status") in ("已生效", DRAFT_MERGED, DRAFT_SHELVED, "已停用"):
            return None, (f"{self.ENTITY} {entry_id} 已处于{entry.get('status')}状态，"
                          "不再具备激活资格，不能重复发起迁移")

        credential_svc = _service_for("publish_credential")
        asset_id = str(entry.get("目标资产", ""))
        version = str(entry.get("发布版本", ""))
        credential = credential_svc.find_credential(asset_id, version)
        if credential is None:
            return None, (f"{self.ENTITY} {entry_id} 缺少发布版本 {version} 的"
                          "分层种子发布凭据，激活被拒绝")
        if credential.get("status") == "已裁决":
            return None, (f"发布版本 {version} 的凭据 {credential.get('凭据编号')} 已裁决，"
                          f"采用草稿 {credential.get('采用草稿')}，本草稿不能重复激活")
        if credential.get("status") == "已失效":
            return None, f"发布版本 {version} 的凭据已暂缓失效，本版本不再派发迁移"

        # 从凭据核对当前仍有资格的有效草稿（而不是信任调用方传入的单条记录）
        eligible = credential_svc.eligible_drafts(credential)
        if entry not in eligible:
            return None, (f"{self.ENTITY} {entry_id} 未通过凭据 {credential.get('凭据编号')} "
                          "的激活资格核对")

        if len(eligible) > 1:
            # 多份有效草稿争抢同一次发布：冲突暂缓，不派发任何迁移
            codes = "、".join(str(d.get("策略编号")) for d in eligible)
            reason = (f"同一发布版本 {version} 存在 {len(eligible)} 份有效草稿（{codes}）"
                      "同时申请激活，参数审批冲突，暂缓派发等待裁决")
            credential_svc.mark_disputed(credential, reason)
            for draft in eligible:
                draft["pending"] = True
            self._write_audit_entry(
                entity="分层策略",
                action="激活冲突暂缓",
                target_id=str(credential.get("凭据编号", "")),
                detail=reason,
            )
            return None, (f"激活被暂缓：{reason}。请先在凭据 {credential.get('凭据编号')} "
                          "上完成草稿裁决，再发起迁移")

        # 唯一有效草稿：正常采用并派发
        return self._adopt_draft(entry, credential, reason="单份有效草稿，激活资格核对通过")

    def merge_draft(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """裁决动作：采用指定草稿的期限数据与审批来源，完成本版本唯一派发。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        credential_svc = _service_for("publish_credential")
        asset_id = str(entry.get("目标资产", ""))
        version = str(entry.get("发布版本", ""))
        credential = credential_svc.find_credential(asset_id, version)
        if credential is None:
            return None, f"发布版本 {version} 的分层种子发布凭据不存在，无法裁决"
        if credential.get("status") not in ("待裁决", "待确认"):
            return None, (f"凭据 {credential.get('凭据编号')} 当前状态为 "
                          f"{credential.get('凭据状态')}，无需重复裁决")

        eligible = credential_svc.eligible_drafts(credential)
        if entry not in eligible:
            return None, (f"草稿 {entry.get('策略编号')} 已不具备激活资格"
                          "（已生效/已合并/已暂缓），不能作为裁决采用对象")

        other_codes = [str(d.get("策略编号")) for d in eligible if d is not entry]
        reason = (f"裁决采用草稿 {entry.get('策略编号')} 的期限参数；"
                  f"同版草稿 {('、'.join(other_codes) if other_codes else '无')} "
                  "的编辑依据保留并标记为已合并")
        adopted, msg = self._adopt_draft(entry, credential, reason=reason)
        return adopted, msg

    def shelve_draft(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """裁决动作：暂缓本版本派发，所有草稿留痕失效，凭据作废但审计可追溯。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        credential_svc = _service_for("publish_credential")
        asset_id = str(entry.get("目标资产", ""))
        version = str(entry.get("发布版本", ""))
        credential = credential_svc.find_credential(asset_id, version)
        if credential is None:
            return None, f"发布版本 {version} 的分层种子发布凭据不存在，无法裁决"
        if credential.get("status") in ("已裁决", "已失效"):
            return None, f"凭据 {credential.get('凭据编号')} 已完成处置，不能重复暂缓"

        eligible = credential_svc.eligible_drafts(credential)
        codes = "、".join(str(d.get("策略编号")) for d in eligible) or str(entry.get("策略编号"))
        reason = (f"人工裁决暂缓发布版本 {version} 的分层派发，涉及草稿 {codes}；"
                  "各草稿编辑依据保留，凭据标记失效")
        for draft in eligible:
            self._close_draft(draft, DRAFT_SHELVED, reason,
                              editor=str(draft.get("编辑人", "")))
        credential_svc.mark_void(credential, reason)
        self._write_audit_entry(
            entity="分层策略",
            action="裁决暂缓",
            target_id=str(credential.get("凭据编号", "")),
            detail=reason,
        )
        return entry, f"发布版本 {version} 已暂缓派发，未产生迁移任务"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "标记生效":
            return self.activate_policy(entry_id)
        if action == "合并草稿":
            return self.merge_draft(entry_id)
        if action == "暂缓草稿":
            return self.shelve_draft(entry_id)
        return super().run_action(entry_id, action)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记分层草稿：补全发布版本与凭据关联，值班人编辑依据一并留痕。

        同一目标资产 + 发布版本已存在发布凭据时，新草稿自动挂到该凭据下，
        后续激活统一走凭据资格核对；没有凭据则拒绝登记，避免无主草稿。
        """
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        asset_id = str(entry.get("目标资产", ""))
        version = str(values.get("发布版本") or "v1")
        entry["发布版本"] = version
        entry["编辑人"] = str(values.get("编辑人") or "值班人")
        entry["审批来源"] = str(values.get("审批来源")
                                  or f"值班人参数审批:{entry['编辑人']}")

        credential_svc = _service_for("publish_credential")
        credential = credential_svc.find_credential(asset_id, version)
        if credential is None:
            # 无发布凭据的草稿不允许存在：回滚本次登记
            store.rows(self.MODULE).remove(entry)
            return None, [f"目标资产 {asset_id} 发布版本 {version} 不存在分层种子发布凭据"]
        if credential.get("status") in ("已裁决", "已失效"):
            store.rows(self.MODULE).remove(entry)
            return None, [f"发布版本 {version} 的凭据已{credential.get('凭据状态')}，不能再登记草稿"]

        entry["关联凭据"] = str(credential.get("凭据编号", ""))
        self._write_audit_entry(
            entity="分层策略",
            action="草稿编辑",
            target_id=str(entry.get("策略编号", "")),
            detail=(f"值班人 {entry['编辑人']} 登记草稿 {entry.get('策略编号')}，"
                    f"资产 {asset_id} 版本 {version}，冷却天数 {entry.get('冷却天数')}，"
                    f"关联凭据 {entry['关联凭据']}"),
        )
        return entry, []

    # ---------- 裁决采用与唯一派发 ----------
    def _adopt_draft(
        self,
        entry: dict[str, Any],
        credential: dict[str, Any],
        reason: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """采用一份草稿：生效、回写凭据、唯一派发下游，并合并其余同版草稿。"""
        credential_svc = _service_for("publish_credential")
        current_tier = str(entry.get("当前层级", ""))
        target_tier = str(entry.get("目标层级", ""))
        asset_id = str(entry.get("目标资产", ""))
        version = str(entry.get("发布版本", ""))
        cooling_days = str(entry.get("冷却天数", ""))
        approval_source = str(entry.get("审批来源", "")) or "分层策略草稿激活"
        editor = str(entry.get("编辑人", ""))

        entry["status"] = "已生效"
        entry["pending"] = False

        # 同版本其余有效草稿：保留编辑依据，标记已合并，不再显示为可发起任务
        for draft in credential_svc.eligible_drafts(credential):
            if draft is entry:
                continue
            self._close_draft(
                draft,
                DRAFT_MERGED,
                reason=(f"发布版本 {version} 已采用草稿 {entry.get('策略编号')}，"
                        f"本草稿（冷却天数 {draft.get('冷却天数')}）编辑依据保留并合并"),
                editor=str(draft.get("编辑人", "")),
            )

        # 回写发布凭据：采用的期限数据与审批来源
        credential_svc.mark_decided(
            credential,
            adopted_draft_code=str(entry.get("策略编号", "")),
            adopted_term=cooling_days,
            approval_source=approval_source,
            reason=reason,
        )

        # 写入分层策略数据产物（含版本、期限与审批来源）
        self._write_policy_record(asset_id, entry, credential)

        # 根据分层方向触发下游动作（带凭据去重，保证只派发一次）
        if current_tier == "HOT" and target_tier == "COLD":
            self._trigger_cold_storage(asset_id, entry, credential)
        elif current_tier == "COLD" and target_tier == "HOT":
            self._trigger_hot_prefetch(asset_id, entry, credential)
        elif target_tier == "WARM":
            self._link_retention_rule(asset_id, entry)

        self._write_audit_entry(
            entity="分层策略",
            action="生效",
            target_id=str(entry.get("策略编号", "")),
            detail=(f"分层策略 {asset_id} 版本 {version} {current_tier}→{target_tier} 已生效，"
                    f"采用冷却天数 {cooling_days}，审批来源 {approval_source}；{reason}"),
        )
        self._update_lineage_tier(asset_id, target_tier)

        return entry, (f"{self.ENTITY} {entry.get('id')} 已生效并完成唯一派发，"
                       f"{current_tier}→{target_tier}，采用期限 {cooling_days} 天")

    def _close_draft(
        self,
        draft: dict[str, Any],
        status: str,
        reason: str,
        editor: str,
    ) -> None:
        """关闭未采用的草稿：状态终态化，编辑人/期限等依据原样保留。"""
        draft["status"] = status
        draft["pending"] = False
        draft["处置理由"] = reason
        draft["处置时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._write_audit_entry(
            entity="分层策略",
            action="草稿合并" if status == DRAFT_MERGED else "草稿暂缓",
            target_id=str(draft.get("策略编号", "")),
            detail=(f"草稿 {draft.get('策略编号')}（编辑人 {editor}，冷却天数 "
                    f"{draft.get('冷却天数')}）标记为{status}：{reason}"),
        )

    # ---------- 文件 I/O ----------
    def _write_policy_record(
        self,
        asset_id: str,
        entry: dict[str, Any],
        credential: dict[str, Any] | None = None,
    ) -> None:
        filename = f"tier_policy_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "asset_id": asset_id,
            "publish_version": str(entry.get("发布版本", "")),
            "credential_code": str((credential or {}).get("凭据编号", entry.get("关联凭据", ""))),
            "adopted_draft": str((credential or {}).get("采用草稿", entry.get("策略编号", ""))),
            "current_tier": str(entry.get("当前层级", "")),
            "target_tier": str(entry.get("目标层级", "")),
            "migration_condition": str(entry.get("迁移条件", "")),
            "cooling_days": str(entry.get("冷却天数", "")),
            "access_threshold": str(entry.get("访问频次阈值", "")),
            "approval_source": str((credential or {}).get("审批来源", entry.get("审批来源", ""))),
            "decision_reason": str((credential or {}).get("处置理由", "")),
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def read_policy_record(self, asset_id: str) -> dict[str, Any] | None:
        filename = f"tier_policy_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------- 跨模块副作用 ----------
    def _trigger_cold_storage(
        self,
        asset_id: str,
        policy: dict[str, Any],
        credential: dict[str, Any],
    ) -> None:
        """创建冷存储迁移任务；同一凭据只允许派发一次。"""
        cold_rows = store.rows("cold_storage")
        cred_code = str(credential.get("凭据编号", ""))
        for row in cold_rows:
            if str(row.get("关联凭据")) == cred_code:
                return
        next_id = max((int(r.get("id", 0)) for r in cold_rows), default=0) + 1
        task = {
            "id": next_id,
            "status": "待调度",
            "pending": True,
            "abnormal": False,
            "任务编号": f"COLD-{next_id:04d}",
            "源资产": asset_id,
            "目标存储": "S3-GLACIER",
            "压缩算法": "ZSTD",
            "迁移量": "估算中",
            "预计耗时": "估算中",
            "迁移状态": "待调度",
            "校验摘要": "",
            "发布版本": str(policy.get("发布版本", "")),
            "关联凭据": cred_code,
            "来源草稿": str(policy.get("策略编号", "")),
            "审批来源": str(credential.get("审批来源", policy.get("审批来源", ""))),
        }
        cold_rows.append(task)

    def _trigger_hot_prefetch(
        self,
        asset_id: str,
        policy: dict[str, Any],
        credential: dict[str, Any],
    ) -> None:
        """创建预热回迁任务；同一凭据只允许派发一次。"""
        hot_rows = store.rows("hot_prefetch")
        cred_code = str(credential.get("凭据编号", ""))
        for row in hot_rows:
            if str(row.get("关联凭据")) == cred_code:
                return
        next_id = max((int(r.get("id", 0)) for r in hot_rows), default=0) + 1
        task = {
            "id": next_id,
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "任务编号": f"HOT_-{next_id:04d}",
            "冷存位置": f"S3://glacier/{asset_id}",
            "目标分区": f"{asset_id}_hot",
            "回迁量": "估算中",
            "预计耗时": "估算中",
            "优先级": "高",
            "过期策略": "T+30",
            "任务状态": "排队中",
            "发布版本": str(policy.get("发布版本", "")),
            "关联凭据": cred_code,
            "来源草稿": str(policy.get("策略编号", "")),
        }
        hot_rows.append(task)

    def _link_retention_rule(self, asset_id: str, policy: dict[str, Any]) -> None:
        """为温层资产关联保留规则。"""
        retention_rows = store.rows("retention_rule")
        next_id = max((int(r.get("id", 0)) for r in retention_rows), default=0) + 1
        cooling = str(policy.get("冷却天数", "90"))
        rule = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "规则编号": f"RETE-{next_id:04d}",
            "目标资产": asset_id,
            "保留天数": cooling,
            "归档路径": f"archive://{asset_id}",
            "压缩策略": "ZSTD",
            "豁免条件": "",
            "执行频率": "monthly",
            "规则版本": "v1",
        }
        retention_rows.append(rule)

    def _update_lineage_tier(self, asset_id: str, target_tier: str) -> None:
        """更新血缘图谱中的分层信息。"""
        lineage_rows = store.rows("lineage_graph")
        for row in lineage_rows:
            if str(row.get("下游资产")) == asset_id:
                row["血缘状态"] = f"Tier:{target_tier}"
                break

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
