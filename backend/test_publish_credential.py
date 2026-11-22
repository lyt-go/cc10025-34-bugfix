"""分层种子发布凭据与资产重复发布治理的端到端集成测试。

场景（对应缺陷单）：
1. 已发布资产在编目入口被再次确认（含丢失回执后的重发），验证同版本种子去重；
2. 预置两份修复前残留的、已被值班人分别编辑过的同版草稿（7 天 / 14 天）；
3. 同时提交两个草稿号激活：冲突暂缓、不产生重复迁移，凭据进入待裁决；
4. 裁决采用其中一份草稿：唯一可派发迁移写入期限数据与审批来源，另一份留痕合并；
5. 老草稿不能再显示成可发起任务，审计可追溯两个编辑来源与处置理由；
6. 资产提交真正的新版本后，建立独立凭据、独立草稿与独立分层结果。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.store import store
from app.services import service_for

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

passed = 0
failed = 0


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
    else:
        print(f"  {RED}FAIL{RESET} {desc} {detail}")
        failed += 1
    return condition


def _next_id(module):
    return max((int(r.get("id", 0)) for r in store.rows(module)), default=0) + 1


def _make_legacy_draft(asset_id, version, credential_code, cooling_days, editor):
    """模拟修复前重复发布残留、且已被值班人编辑过的同版分层草稿。"""
    tier_rows = store.rows("tier_policy")
    next_id = _next_id("tier_policy")
    draft = {
        "id": next_id,
        "status": "草稿",
        "pending": True,
        "abnormal": False,
        "策略编号": f"TIER-{next_id:04d}",
        "目标资产": asset_id,
        "当前层级": "HOT",
        "迁移条件": "访问频次<5次/天",
        "目标层级": "COLD",
        "冷却天数": str(cooling_days),
        "访问频次阈值": "5",
        "策略版本": "v1",
        "发布版本": version,
        "关联凭据": credential_code,
        "编辑人": editor,
        "审批来源": f"值班人参数审批:{editor}",
    }
    tier_rows.append(draft)
    return draft


def main():
    print("=" * 64)
    print("重复发布种子去重 / 发布凭据 / 双草稿冲突裁决 集成测试")
    print("=" * 64)

    asset_svc = service_for("asset_catalog")
    tier_svc = service_for("tier_policy")
    cred_svc = service_for("publish_credential")

    # ---------- 阶段1：首次发布（ASSE-0003 种子状态为已发布） ----------
    print("\n=== 阶段1: 资产首次发布，写入唯一凭据与唯一种子 ===")
    entry, msg = asset_svc.publish_asset(3)
    check("首次发布成功", entry is not None and entry["status"] == "已发布", msg)

    v1_cred = cred_svc.find_credential("ASSE-0003", "v1")
    check("v1 发布凭据已建立", v1_cred is not None)
    check("凭据初始状态为待确认", v1_cred and v1_cred["status"] == "待确认")

    v1_drafts = [r for r in store.rows("tier_policy")
                 if r.get("目标资产") == "ASSE-0003" and r.get("发布版本") == "v1"]
    check("v1 只生成一份分层种子草稿", len(v1_drafts) == 1, f"实际 {len(v1_drafts)} 份")
    seed_draft = v1_drafts[0]
    check("种子草稿已回填关联凭据",
          seed_draft.get("关联凭据") == v1_cred.get("凭据编号"))

    # ---------- 阶段2：同版本重复确认 + 丢失回执重发（幂等拦截） ----------
    print("\n=== 阶段2: 同版本再次确认发布并重发，验证种子去重 ===")
    entry2, msg2 = asset_svc.publish_asset(3)
    check("重复确认发布被幂等接受", entry2 is not None, msg2)
    entry3, msg3 = asset_svc.publish_asset(3)
    check("丢失回执后的重发被幂等接受", entry3 is not None, msg3)

    creds_v1 = [r for r in store.rows("publish_credential")
                if r.get("目标资产") == "ASSE-0003" and r.get("发布版本") == "v1"]
    check("同版本始终只有一份发布凭据", len(creds_v1) == 1, f"实际 {len(creds_v1)} 份")
    v1_drafts = [r for r in store.rows("tier_policy")
                 if r.get("目标资产") == "ASSE-0003" and r.get("发布版本") == "v1"
                 and r.get("status") == "草稿"]
    check("重发未产生第二份分层草稿", len(v1_drafts) == 1, f"实际 {len(v1_drafts)} 份")

    idem_audits = [r for r in store.rows("audit_log")
                   if r.get("操作类型") == "发布重发(幂等拦截)"]
    check("幂等拦截写入审计留痕", len(idem_audits) >= 2, f"实际 {len(idem_audits)} 条")

    # ---------- 阶段3：预置两份修复前残留的已编辑同版草稿 ----------
    print("\n=== 阶段3: 预置两份已编辑的同版历史草稿（7天 / 14天） ===")
    # 原系统未编辑种子先被值班人的编辑草稿取代（保留留痕），只留两份有效草稿，
    # 精确还原缺陷单中“两个有效草稿残留”的现场。
    seed_draft["status"] = "已合并"
    seed_draft["pending"] = False
    seed_draft["处置理由"] = "修复前重复发布残留：原始种子已被值班人编辑草稿取代，依据留痕"
    draft_7 = _make_legacy_draft(
        "ASSE-0003", "v1", v1_cred["凭据编号"], 7, "值班人甲")
    draft_14 = _make_legacy_draft(
        "ASSE-0003", "v1", v1_cred["凭据编号"], 14, "值班人乙")
    eligible = cred_svc.eligible_drafts(v1_cred)
    check("凭据核对出恰好两份有资格的同版草稿", len(eligible) == 2,
          f"实际 {len(eligible)} 份")
    check("7天草稿编辑依据保留", draft_7["冷却天数"] == "7" and draft_7["编辑人"] == "值班人甲")
    check("14天草稿编辑依据保留", draft_14["冷却天数"] == "14" and draft_14["编辑人"] == "值班人乙")

    # ---------- 阶段4：两个草稿号同时激活，冲突暂缓、零派发 ----------
    print("\n=== 阶段4: 同时提交两个草稿号激活，验证冲突暂缓不重复派发 ===")
    cold_before = [r for r in store.rows("cold_storage")
                   if r.get("源资产") == "ASSE-0003"]
    blocked1, bmsg1 = tier_svc.activate_policy(draft_7["id"])
    check("第一份草稿激活被暂缓拦截", blocked1 is None, bmsg1)
    blocked2, bmsg2 = tier_svc.activate_policy(draft_14["id"])
    check("第二份草稿激活同样被拦截", blocked2 is None, bmsg2)
    # 重发原发布也不能绕过裁决
    blocked3, bmsg3 = asset_svc.publish_asset(3)
    check("冲突期间重发发布仍走幂等不新播种", blocked3 is not None and len(
        [r for r in store.rows("publish_credential")
         if r.get("目标资产") == "ASSE-0003" and r.get("发布版本") == "v1"]) == 1, bmsg3)

    check("凭据进入待裁决状态", v1_cred["status"] == "待裁决",
          f"实际 {v1_cred['status']}")
    cold_after = [r for r in store.rows("cold_storage")
                  if r.get("源资产") == "ASSE-0003"]
    check("冲突暂缓未派发任何冷存迁移", len(cold_after) == len(cold_before),
          f"拦截前 {len(cold_before)} / 拦截后 {len(cold_after)}")
    check("两份草稿仍是草稿状态（编辑未丢）",
          draft_7["status"] == "草稿" and draft_14["status"] == "草稿")

    dispute_audits = [r for r in store.rows("audit_log")
                      if r.get("操作类型") == "激活冲突暂缓"]
    check("冲突暂缓写入审计", len(dispute_audits) >= 1)

    # ---------- 阶段5：裁决采用 7 天草稿，唯一派发 ----------
    print("\n=== 阶段5: 裁决采用7天草稿，验证唯一派发与期限/审批来源写入 ===")
    adopted, amsg = tier_svc.merge_draft(draft_7["id"])
    check("裁决采用成功", adopted is not None and adopted["status"] == "已生效", amsg)

    check("凭据回写为已裁决", v1_cred["status"] == "已裁决")
    check("凭据记录采用草稿号", v1_cred["采用草稿"] == draft_7["策略编号"])
    check("凭据记录采用期限7天", v1_cred["采用期限"] == "7")
    check("凭据记录审批来源", "值班人甲" in str(v1_cred["审批来源"]))

    # 14 天草稿与其它历史草稿：已合并、保留编辑依据、不再可发起
    check("14天草稿标记已合并且编辑依据保留",
          draft_14["status"] == "已合并"
          and draft_14["冷却天数"] == "14"
          and draft_14["编辑人"] == "值班人乙"
          and bool(draft_14.get("处置理由")))
    check("原系统种子草稿维持已合并留痕", seed_draft["status"] == "已合并")
    check("被采用草稿不再出现在有效资格列表",
          cred_svc.eligible_drafts(v1_cred) == [])

    cold_tasks = [r for r in store.rows("cold_storage")
                  if r.get("关联凭据") == v1_cred["凭据编号"]]
    check("本次发布只派发一份冷存任务", len(cold_tasks) == 1, f"实际 {len(cold_tasks)} 份")
    task = cold_tasks[0] if cold_tasks else {}
    check("迁移任务带发布版本", task.get("发布版本") == "v1")
    check("迁移任务带来源草稿", task.get("来源草稿") == draft_7["策略编号"])
    check("迁移任务带审批来源", "值班人甲" in str(task.get("审批来源", "")))

    # ---------- 阶段6：老草稿不能再激活/再派发 ----------
    print("\n=== 阶段6: 老草稿重复激活与重复派发防护 ===")
    again1, amsg1 = tier_svc.activate_policy(draft_7["id"])
    check("已采用草稿再次激活被拒绝", again1 is None, amsg1)
    again2, amsg2 = tier_svc.activate_policy(draft_14["id"])
    check("已合并草稿再次激活被拒绝", again2 is None, amsg2)
    again3, amsg3 = tier_svc.merge_draft(draft_14["id"])
    check("对已合并草稿重复裁决被拒绝", again3 is None, amsg3)

    cold_tasks2 = [r for r in store.rows("cold_storage")
                   if r.get("关联凭据") == v1_cred["凭据编号"]]
    check("没有产生第二份迁移任务", len(cold_tasks2) == 1)

    # 分层策略数据产物写入采用期限与审批来源
    record = tier_svc.read_policy_record("ASSE-0003")
    check("分层产物记录采用期限7天", record and record.get("cooling_days") == "7")
    check("分层产物记录审批来源", record and "值班人甲" in str(record.get("approval_source")))
    check("分层产物记录发布版本与凭据",
          record and record.get("publish_version") == "v1"
          and record.get("credential_code") == v1_cred["凭据编号"])

    # 审计可追溯两个编辑来源与合并/暂缓处置理由
    merge_audits = [r for r in store.rows("audit_log")
                    if r.get("操作类型") == "草稿合并"]
    check("审计保留草稿合并处置记录", len(merge_audits) >= 1)
    audit_text = " | ".join(str(r.get("审计摘要", "")) for r in store.rows("audit_log"))
    check("审计可追溯值班人甲(7天)编辑来源", "值班人甲" in audit_text and "7" in audit_text)
    check("审计可追溯值班人乙(14天)编辑来源", "值班人乙" in audit_text and "14" in audit_text)

    # ---------- 阶段7：暂缓裁决路径（另一资产验证处置理由留痕） ----------
    print("\n=== 阶段7: 暂缓裁决路径验证 ===")
    # 直接构造一份带凭据的冲突草稿场景
    cred_b = cred_svc.issue_credential("ASSE-TEST-B", "v1", "TIER-B-SEED", "测试域")
    b_draft = _make_legacy_draft("ASSE-TEST-B", "v1", cred_b["凭据编号"], 30, "值班人丙")
    shelved, smsg = tier_svc.shelve_draft(b_draft["id"])
    check("暂缓裁决成功", shelved is not None, smsg)
    check("凭据标记已失效", cred_b["status"] == "已失效")
    check("草稿标记已暂缓且理由留痕",
          b_draft["status"] == "已暂缓" and bool(b_draft.get("处置理由")))
    b_cold = [r for r in store.rows("cold_storage")
              if r.get("关联凭据") == cred_b["凭据编号"]]
    check("暂缓版本未派发迁移", len(b_cold) == 0)
    b_retry, _ = tier_svc.activate_policy(b_draft["id"])
    check("已暂缓草稿不能再激活", b_retry is None)

    # ---------- 阶段8：真正的新版本独立确认流程 ----------
    print("\n=== 阶段8: 资产提交真正新版本，建立独立分层结果 ===")
    nv, nvmsg = asset_svc.submit_new_version(3)
    check("新版本提交成功并回到待审核",
          nv is not None and nv["status"] == "待审核" and nv["发布版本"] == "v2", nvmsg)

    # 新版本发布前不应有 v2 凭据/草稿
    check("发布前不存在v2凭据", cred_svc.find_credential("ASSE-0003", "v2") is None)
    pub2, pmsg = asset_svc.publish_asset(3)
    check("v2 发布成功", pub2 is not None and pub2["status"] == "已发布", pmsg)

    v2_cred = cred_svc.find_credential("ASSE-0003", "v2")
    check("v2 建立独立发布凭据", v2_cred is not None
          and v2_cred["凭据编号"] != v1_cred["凭据编号"])
    v2_drafts = [r for r in store.rows("tier_policy")
                 if r.get("目标资产") == "ASSE-0003" and r.get("发布版本") == "v2"]
    check("v2 生成独立分层草稿", len(v2_drafts) == 1
          and v2_drafts[0]["关联凭据"] == v2_cred["凭据编号"])
    check("v1 凭据裁决结果不受新版本影响", v1_cred["status"] == "已裁决"
          and v1_cred["采用草稿"] == draft_7["策略编号"])

    v2_adopted, v2msg = tier_svc.activate_policy(v2_drafts[0]["id"])
    check("v2 草稿独立激活成功", v2_adopted is not None and v2_adopted["status"] == "已生效", v2msg)
    v2_tasks = [r for r in store.rows("cold_storage")
                if r.get("关联凭据") == v2_cred["凭据编号"]]
    check("v2 派发独立迁移任务", len(v2_tasks) == 1 and v2_tasks[0]["发布版本"] == "v2")
    check("v1 迁移任务保持唯一不被重复",
          len([r for r in store.rows("cold_storage")
               if r.get("关联凭据") == v1_cred["凭据编号"]]) == 1)
    check("v2 凭据独立裁决", v2_cred["status"] == "已裁决"
          and v2_cred["采用草稿"] == v2_drafts[0]["策略编号"])

    print(f"\n{'=' * 64}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 64)
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
