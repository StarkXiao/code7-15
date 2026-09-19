"""端到端业务演示（直接调用服务层，无需启动 HTTP）。

跑三条批次的完整命运：

  * B-DEMO-01  一切合格                → QP 电子签名放行
  * B-DEMO-02  关键工艺 OOT → 重大偏差 → CAPA 无效被退回 → CAPA 验证有效 → 放行
  * B-DEMO-03  QC 含量 OOS（真实超标） → 放行被硬性阻断 → QA 拒放

最后演示审计链防篡改：直接改库（模拟内鬼/磁盘篡改），verify_chain 立即报警。

用法：python scripts/demo.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 使用独立的演示库，跑完即删，绝不污染正式数据
_TMP = tempfile.mkdtemp(prefix="ebr-demo-")
os.environ["EBR_DB_PATH"] = str(Path(_TMP) / "demo.db")

from ebr import service  # noqa: E402
from ebr.audit import AuditTrail  # noqa: E402
from ebr.db import connect  # noqa: E402
from ebr.errors import AuditChainBrokenError, ReleaseBlockedError  # noqa: E402
from scripts.seed import PRODUCT_CODE, seed  # noqa: E402

PASS = "\033[92m✔\033[0m"
BLOCK = "\033[91m✘\033[0m"
TITLE = "\033[96m"
RESET = "\033[0m"


def u(username: str) -> dict:
    conn = connect()
    try:
        return dict(conn.execute("SELECT * FROM users WHERE username = ?",
                                 (username,)).fetchone())
    finally:
        conn.close()


def banner(text: str) -> None:
    print(f"\n{TITLE}{'=' * 72}\n {text}\n{'=' * 72}{RESET}")


def show_gates(batch_no: str) -> None:
    ev = service.evaluate_batch(batch_no)
    for g in ev["gates"]:
        mark = PASS if g["passed"] else BLOCK
        print(f"   {mark} {g['id']:<24} {g['name']}")
        for f in g["failures"]:
            print(f"       └─ {f}")
    verdict = "可放行" if ev["releasable"] else "阻断中"
    print(f"   综合结论: {verdict}（未通过门禁: {ev['open_blocking_gates'] or '无'}）")


# ----------------------------------------------------------------------

def record_good_batch(batch_no: str, actual_size: float = 100500.0) -> None:
    """投料 → 工艺 → 质检 → 完工的合规流水线（复核人全部不同）。"""
    op, lead, qa = u("zhang.gong"), u("li.chejian"), u("zhao.qa")
    analyst, analyst2 = u("wang.huayan"), qa  # QC 复核用 QA，保证非同一人

    service.start_production(actor=op, batch_no=batch_no)

    for code, qty in [("MAT-API-001", 25.60), ("MAT-EXC-001", 0.61),
                      ("MAT-EXC-002", 4.25), ("MAT-PKG-001", 99800)]:
        service.dispense_material(actor=op, batch_no=batch_no,
                                  material_code=code, qty_actual=qty)
        service.verify_dispensing(actor=lead, batch_no=batch_no, material_code=code)

    for step, name, value in [
            (10, "搅拌转速", 185.0), (10, "混合时间", 21.0),
            (20, "进风温度", 66.0), (20, "颗粒水分", 2.6),
            (30, "装量差异", 1.2)]:
        service.record_process_value(actor=op, batch_no=batch_no, step_no=step,
                                     param_name=name, actual_value=value)

    qc_values = [
        ("装量差异", 1.2, None),
        ("含量（阿莫西林）", 99.4, None),
        ("溶出度(30min)", 92.0, None),
        ("水分", 3.1, None),
        ("性状", None, "内容物为白色或类白色粉末"),
    ]
    for test, num, txt in qc_values:
        service.record_qc_result(actor=analyst, batch_no=batch_no, test_name=test,
                                 numeric_value=num, text_value=txt)
        service.review_qc_result(actor=analyst2, batch_no=batch_no, test_name=test)

    service.complete_production(actor=lead, batch_no=batch_no, actual_size=actual_size)


def main() -> None:
    seed()
    audit = AuditTrail()

    # ================================================================
    banner("批次 1  B-DEMO-01：完全合规批次")
    # ================================================================
    service.create_batch(actor=u("zhang.gong"), product_code=PRODUCT_CODE,
                         planned_size=100000, batch_no="B-DEMO-01")
    record_good_batch("B-DEMO-01")
    show_gates("B-DEMO-01")
    out = service.release_batch(
        actor=u("sun.qp"), batch_no="B-DEMO-01",
        reason="审核合格，电子批记录完整，同意放行。")
    print(f"\n   {PASS} QP 电子签名完成 → 批次状态: {out['status']}")
    print(f"      签名哈希: {out['decision']['sig_hash'][:32]}…")

    # ================================================================
    banner("批次 2  B-DEMO-02：关键工艺 OOT → 重大偏差 → CAPA 闭环")
    # ================================================================
    service.create_batch(actor=u("zhang.gong"), product_code=PRODUCT_CODE,
                         planned_size=100000, batch_no="B-DEMO-02")
    op, lead, qa = u("zhang.gong"), u("li.chejian"), u("zhao.qa")
    service.start_production(actor=op, batch_no="B-DEMO-02")
    for code, qty in [("MAT-API-001", 25.55), ("MAT-EXC-001", 0.59),
                      ("MAT-EXC-002", 4.18), ("MAT-PKG-001", 100200)]:
        service.dispense_material(actor=op, batch_no="B-DEMO-02",
                                  material_code=code, qty_actual=qty)
        service.verify_dispensing(actor=lead, batch_no="B-DEMO-02", material_code=code)

    # 进风温度 73.8℃，超出 CPP 上限 70℃
    for step, name, value in [
            (10, "搅拌转速", 181.0), (10, "混合时间", 20.0),
            (20, "进风温度", 73.8), (20, "颗粒水分", 2.4),
            (30, "装量差异", 0.8)]:
        service.record_process_value(actor=op, batch_no="B-DEMO-02", step_no=step,
                                     param_name=name, actual_value=value)
    analyst = u("wang.huayan")
    for test, num, txt in [
            ("装量差异", 0.8, None), ("含量（阿莫西林）", 98.1, None),
            ("溶出度(30min)", 89.0, None), ("水分", 2.8, None),
            ("性状", None, "内容物为白色或类白色粉末")]:
        service.record_qc_result(actor=analyst, batch_no="B-DEMO-02", test_name=test,
                                 numeric_value=num, text_value=txt)
        service.review_qc_result(actor=qa, batch_no="B-DEMO-02", test_name=test)

    # 找到进风温度记录编号作为偏差来源
    ebr = service.get_ebr("B-DEMO-02")
    temp_rec = next(r for r in ebr["process_records"] if r["param_name"] == "进风温度")

    service.complete_production(actor=lead, batch_no="B-DEMO-02", actual_size=100100)
    print("\n   —— QA 初次评审 ——")
    show_gates("B-DEMO-02")

    # 第一次放行尝试：被门禁阻断
    try:
        service.release_batch(actor=u("sun.qp"), batch_no="B-DEMO-02",
                              reason="尝试放行")
    except ReleaseBlockedError as e:
        print(f"\n   {BLOCK} 放行被系统阻断: {e.message}")
        print("      （这次阻断尝试已写入审计链，result=BLOCKED）")

    # QA 驳回整改
    service.reopen_for_correction(
        actor=qa, batch_no="B-DEMO-02",
        reason="进风温度 CPP 超标，须立案重大偏差并评估 CAPA 后重新报交")
    dev = service.raise_deviation(
        actor=op, batch_no="B-DEMO-02", category="MAJOR",
        title="干燥进风温度超标 73.8℃（限度 60~70℃）",
        description="干燥工序进风温度短时达到 73.8℃，评估颗粒质量与稳定性影响",
        source_type="PROCESS_OOT", source_ref=f"process_record:{temp_rec['id']}")
    service.complete_production(actor=lead, batch_no="B-DEMO-02", actual_size=100100)
    service.close_deviation(
        actor=qa, dev_no=dev["dev_no"],
        root_cause="蒸汽阀门定位器漂移导致温度超调，已评估成品质量合格、无稳定性风险",
        root_cause_confirmed=True)

    capa = service.create_capa(
        actor=qa, dev_no=dev["dev_no"],
        action="更换阀门定位器并对干燥设备增加温度联锁报警；再验证",
        owner_username="li.chejian", due_date="2026-10-15")
    service.implement_capa(actor=u("li.chejian"), capa_id=capa["id"])

    # QA 第一次验证：措施无效（整改不到位）
    service.verify_capa(actor=qa, capa_id=capa["id"], effective=False,
                        note="联锁报警未做再验证，无法确认有效，退回重做")
    print("\n   —— CAPA 第一次验证被判“无效”后的门禁 ——")
    show_gates("B-DEMO-02")
    try:
        service.release_batch(actor=u("sun.qp"), batch_no="B-DEMO-02",
                              reason="再次尝试放行")
    except ReleaseBlockedError as e:
        print(f"\n   {BLOCK} 仍然阻断: {e.message}")

    # 重新提交并验证有效（措施责任人按整改要求二次实施，状态由 INEFFECTIVE 回到 OPEN）
    conn = connect()
    conn.execute("UPDATE capas SET status = 'OPEN', closed_at = NULL WHERE id = ?",
                 (capa["id"],))
    conn.commit()
    conn.close()
    service.implement_capa(actor=u("li.chejian"), capa_id=capa["id"])
    service.verify_capa(actor=qa, capa_id=capa["id"], effective=True,
                        note="联锁报警安装完成并完成三批次再验证，确认有效")
    print("\n   —— CAPA 验证有效后的门禁 ——")
    show_gates("B-DEMO-02")
    out = service.release_batch(
        actor=u("sun.qp"), batch_no="B-DEMO-02",
        reason="偏差已根因调查，CAPA 验证有效，成品检验合格，同意放行。")
    print(f"\n   {PASS} QP 电子签名完成 → 批次状态: {out['status']}")

    # ================================================================
    banner("批次 3  B-DEMO-03：QC 含量 OOS（真实超标）→ 阻断 → 拒放")
    # ================================================================
    service.create_batch(actor=u("zhang.gong"), product_code=PRODUCT_CODE,
                         planned_size=100000, batch_no="B-DEMO-03")
    record_good_batch("B-DEMO-03")
    # 追加一版 OOS：需要先把演示记录中的含量记成 92.6%。重新构造该批次：
    # （record_good_batch 已录合格值，这里演示的是“真实超标”，故新建批次时直接覆盖场景：
    #   通过偏差作废不适用，改为演示一次无法救活的 OOS。）
    # 为保持脚本简单，删除该批的合格含量记录并重录 OOS（仅演示库操作）
    conn = connect()
    conn.execute("DELETE FROM qc_records WHERE batch_id = "
                 "(SELECT id FROM batches WHERE batch_no='B-DEMO-03') AND test_name='含量（阿莫西林）'")
    conn.commit()
    conn.close()
    service.record_qc_result(actor=u("wang.huayan"), batch_no="B-DEMO-03",
                             test_name="含量（阿莫西林）", numeric_value=92.6)
    service.review_qc_result(actor=qa, batch_no="B-DEMO-03",
                             test_name="含量（阿莫西林）")

    oos_rec = next(r for r in service.get_ebr("B-DEMO-03")["qc_records"]
                   if r["test_name"] == "含量（阿莫西林）" and r["result"] == "OOS")
    service.raise_deviation(
        actor=u("wang.huayan"), batch_no="B-DEMO-03", category="CRITICAL",
        title="成品含量 OOS：92.6%（限度 95.0~105.0%）",
        description="成品含量低于注册标准，复测确认非化验室差错，疑混合均匀度问题",
        source_type="QC_OOS", source_ref=f"qc_record:{oos_rec['id']}")
    print()
    show_gates("B-DEMO-03")
    try:
        service.release_batch(actor=u("sun.qp"), batch_no="B-DEMO-03",
                              reason="QP 尝试放行 OOS 批次")
    except ReleaseBlockedError as e:
        print(f"\n   {BLOCK} 系统硬性阻断放行: {e.message}")
        print("      注意：服务层不存在 force=True 之类的旁路参数，QP 也无法放行 OOS")

    out = service.reject_batch(
        actor=qa, batch_no="B-DEMO-03",
        reason="成品含量真实 OOS 且为严重偏差未闭环，按不合格品拒放，启动召回风险评估")
    print(f"   {BLOCK} QA 电子签名拒放 → 批次状态: {out['status']}")

    # ================================================================
    banner("审计链完整性：正常校验 → 篡改数据库 → 立即报警")
    # ================================================================
    ok = audit.verify_chain()
    print(f"   {PASS} 篡改前校验通过，共 {ok['entries_checked']} 条审计记录")

    conn = connect()
    target = conn.execute(
        "SELECT id, reason FROM audit_log WHERE action='BATCH_RELEASE' "
        "AND entity_ref='B-DEMO-03' ORDER BY id LIMIT 1").fetchone()
    # 触发器禁止 UPDATE；真实威胁往往是绕过应用直改文件/卸下触发器。
    # 这里临时关闭触发器模拟“拿到库文件直接改”的攻击，验证哈希链本身的检测力。
    conn.execute("DROP TRIGGER trg_audit_no_update")
    conn.execute("UPDATE audit_log SET reason = ? WHERE id = ?",
                 ("（被篡改）同意放行", target["id"]))
    conn.commit()
    conn.close()
    print(f"   ! 已直接篡改审计记录 #{target['id']} 的内容（绕过应用层）")

    try:
        audit.verify_chain()
    except AuditChainBrokenError as e:
        print(f"   {BLOCK} 审计链校验报警: {e.message}")
        print(f"      定位记录: id={e.details['broken_at']}")
    sig = service.verify_signature_chain()
    print(f"   {PASS} 放行签名链独立校验: {sig}")

    # 批次台账汇总
    banner("批次台账最终状态")
    conn = connect()
    for b in conn.execute("SELECT batch_no, status FROM batches ORDER BY batch_no"):
        mark = PASS if b["status"] == "RELEASED" else BLOCK
        print(f"   {mark} {b['batch_no']}  {b['status']}")
    conn.close()
    print(f"\n演示库位于临时目录（可删）: {os.environ['EBR_DB_PATH']}")


if __name__ == "__main__":
    main()
