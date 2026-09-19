#!/usr/bin/env python3
"""端到端演示：直接驱动服务层，构造三个批次并验证阻断/放行/审计链。

用法:
    python scripts/demo.py              # 重建 data/demo.db 并跑全部剧情
    python scripts/demo.py --keep-db   # 复用已有库

剧情：
  A 正常批：投料合规 → 工艺合格 → QC 全合格 → QA 电子签名放行
  B 异常批：API 超量 4.6% + 含量 OOS（critical）→ 自动偏差 → 规则引擎阻断 → QA 拒绝
  C 纠偏批：混合时间 OOS（major）→ QA 调查接受 + CAPA → 重新放行成功
  D 篡改演示：直接改库（绕过触发器的可行方式）→ 哈希校验立即发现
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services
from app.database import connect, init_db

OP1 = {"id": 1, "username": "op01", "display_name": "王操作", "role": "operator"}
OP2 = {"id": 2, "username": "op02", "display_name": "李操作", "role": "operator"}
PL = {"id": 3, "username": "pl01", "display_name": "赵班长", "role": "production_lead"}
QC1 = {"id": 4, "username": "qc01", "display_name": "孙检验", "role": "qc"}
QC2 = {"id": 5, "username": "qc02", "display_name": "周复核", "role": "qc"}
QA = {"id": 6, "username": "qa01", "display_name": "钱质保", "role": "qa"}

# (物料编码, 实际量)
WEIGH_GOOD = [("M-API-001", 10.50), ("M-EXC-101", 5.20),
              ("M-EXC-102", 0.26), ("M-PKG-201", 12.00)]
# (步骤号, 参数名, 值)
PARAMS_GOOD = [
    (20, "混合时间", 25.0), (20, "混合转速", 15.0),
    (30, "平均片重", 0.186), (30, "片重差异", 1.8), (30, "硬度", 75.0),
    (40, "密封性", True),
]
QC_GOOD = [
    ("性状", True), ("维生素C含量", 99.2), ("溶出度", 92.0),
    ("片重差异", 2.1), ("水分", 3.2), ("微生物限度", True),
]


def line(t=""):
    print(t)


def run_weighing(db, bid, weigh_list):
    services.start_weighing(db, bid, OP1)
    for code, qty in weigh_list:
        r = services.record_weighing(db, bid, code, qty, OP1)
        rec_id = r["record"]["id"]
        services.check_weighing(db, bid, rec_id, OP2)
        flag = f"⚠ 自动偏差#{r['auto_deviation_id']}" if r["auto_deviation_id"] else "✓"
        print(f"   投料 {code} {qty} (偏差 {r['deviation_pct']}%) 双人复核 {flag}")


def run_steps(db, bid, params):
    for step_no in (10, 20, 30, 40):
        services.start_step(db, bid, step_no, OP1)
        for sn, name, val in params:
            if sn == step_no:
                r = services.record_param(db, bid, sn, name, val, OP1)
                flag = f"⚠ 自动偏差#{r['auto_deviation_id']}" if r["auto_deviation_id"] else "✓"
                print(f"   工艺 S{sn} {name}={val} {flag}")
        services.finish_step(db, bid, step_no, PL)


def run_qc(db, bid, qc_list):
    services.start_qc(db, bid, QC1)
    for name, val in qc_list:
        r = services.record_qc(db, bid, name, val, QC1)
        rec_id = r["record"]["id"]
        services.check_qc(db, bid, rec_id, QC2)
        if r["record"]["result_conforms"]:
            print(f"   QC {name}={val} 符合 双人复核 ✓")
        else:
            print(f"   QC {name}={val} OOS 不符 ⚠ 自动偏差#{r['auto_deviation_id']}")


def header(title):
    line("\n" + "=" * 72)
    line(title)
    line("=" * 72)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "demo.db"))
    ap.add_argument("--keep-db", action="store_true")
    args = ap.parse_args()
    db = os.path.abspath(args.db)

    if not args.keep_db and os.path.exists(db):
        for ext in ("", "-wal", "-shm"):
            p = db + ext
            if os.path.exists(p):
                os.remove(p)
    init_db(db)
    services.seed(db)

    # ---------------- 批次 A：正常放行 ----------------
    header("批次 A —— 正常批（期望：电子签名放行成功）")
    a = services.create_batch(db, PL, batch_no="VC260919-A")["id"]
    run_weighing(db, a, WEIGH_GOOD)
    run_steps(db, a, PARAMS_GOOD)
    services.finish_production(db, a, 99.2, PL)
    run_qc(db, a, QC_GOOD)
    services.submit_for_review(db, a, QC2)
    review = services.review_status(db, a)
    print(f"   规则引擎：阻断项 {len(review['blockers'])} 项 → can_release={review['can_release']}")
    services.release_batch(db, a, "批记录完整，检验全部合格，同意放行。", "Qa@12345", QA)
    print("   QA 电子签名放行 ✓ 状态 =", services.review_status(db, a)["batch"]["status"])

    # ---------------- 批次 B：异常阻断 + 拒绝 ----------------
    header("批次 B —— 异常批（期望：自动偏差 → 引擎阻断 → 拒绝放行）")
    b = services.create_batch(db, PL, batch_no="VC260919-B")["id"]
    bad_weigh = [("M-API-001", 10.98)] + WEIGH_GOOD[1:]   # 10.50 -> 10.98 = +4.57%
    run_weighing(db, b, bad_weigh)
    run_steps(db, b, PARAMS_GOOD)
    services.finish_production(db, b, 98.6, PL)
    bad_qc = QC_GOOD.copy()
    bad_qc[1] = ("维生素C含量", 91.5)   # 低于 95% 下限 → critical OOS
    run_qc(db, b, bad_qc)
    services.submit_for_review(db, b, QC2)
    review = services.review_status(db, b)
    print(f"   规则引擎：{len(review['blockers'])} 项阻断 "
          f"(critical={review['blocker_counts']['critical']}, "
          f"major={review['blocker_counts']['major']})")
    for blk in review["blockers"]:
        print(f"     [{blk['severity'].upper():8s}] {blk['code']}: {blk['message']}")

    print("\n   >> QA 尝试放行（即使签名正确，也应被系统硬阻断）：")
    try:
        services.release_batch(db, b, "强行放行试试", "Qa@12345", QA)
    except Exception as e:
        print(f"      系统返回 409: {e}")

    services.reject_batch(db, b, "API 投料超量且含量 OOS，偏差未关闭，拒绝放行。", QA)
    print("   QA 拒绝放行 ✓ 状态 =", services.review_status(db, b)["batch"]["status"])

    # ---------------- 批次 C：纠偏后放行 ----------------
    header("批次 C —— 纠偏批（期望：偏差调查接受 + CAPA 后放行成功）")
    c = services.create_batch(db, PL, batch_no="VC260919-C")["id"]
    run_weighing(db, c, WEIGH_GOOD)
    params_c = [(s, n, 18.0 if n == "混合时间" else v) for s, n, v in PARAMS_GOOD]
    run_steps(db, c, params_c)            # 混合时间 18min < 20min 下限 → major 偏差
    services.finish_production(db, c, 99.5, PL)
    run_qc(db, c, QC_GOOD)
    devs = services.list_deviations(db, c)
    dev = next(d for d in devs if d["source"] == "auto_process")
    print(f"   自动偏差 #{dev['id']}（{dev['severity']}）：{dev['description']}")

    services.submit_for_review(db, c, QC2)
    review = services.review_status(db, c)
    print(f"   提交 QA 后阻断项：{len(review['blockers'])} 项 → 不能放行")

    print("   >> QA 启动偏差调查并关闭（accepted + CAPA）：")
    services.close_deviation(
        db, dev["id"], "accepted",
        "调查确认为计时器设定错误；已延长混合至25分钟重新混合并复测含量均匀度合格；"
        "CAPA：对混合岗计时器加装双人确认并重新培训，纳入月度点检。", QA)
    review = services.review_status(db, c)
    print(f"   关闭后阻断项：{len(review['blockers'])} 项 → can_release={review['can_release']}")
    services.release_batch(db, c, "偏差已按 CAPA 关闭，复测合格，同意放行。", "Qa@12345", QA)
    print("   QA 电子签名放行 ✓ 状态 =", services.review_status(db, c)["batch"]["status"])

    # ---------------- 审计链 ----------------
    header("审计链验证")
    chain = services.verify_chain(db)
    print(f"   哈希链: ok={chain['ok']} 条目数={chain['entries']}")
    print(f"   链尾哈希: {chain['tail_hash']}")
    integrity_c = services.full_integrity_report(db, c)
    print(f"   批次C 记录复算: ok={integrity_c['records']['ok']} "
          f"校验记录数={integrity_c['records']['records_checked']}")
    print(f"   批次C 状态指纹: {integrity_c['records']['state_hash']}")

    # ---------------- 篡改检测 ----------------
    header("篡改检测演示（模拟有人拿到库文件直接改数）")
    print("   >> 尝试在批次冻结后 UPDATE 业务记录（SQLite 触发器拦截）：")
    raw = connect(db)
    try:
        raw.execute("UPDATE qc_results SET numeric_value=99.0 WHERE batch_id=? AND test_name='维生素C含量'",
                    (b,))
        print("      未拦截（异常！）")
    except sqlite3.IntegrityError as e:
        print(f"      触发器拒绝: {e}")
    try:
        raw.execute("UPDATE audit_log SET reason='hacked' WHERE id=1")
        print("      未拦截（异常！）")
    except sqlite3.IntegrityError as e:
        print(f"      触发器拒绝: {e}")
    finally:
        raw.close()

    print("\n   >> 模拟攻击者绕过应用、临时拆除触发器后篡改历史记录：")
    raw = sqlite3.connect(db)
    try:
        raw.execute("DROP TRIGGER IF EXISTS trg_freeze_qc_upd")
        raw.execute("UPDATE qc_results SET numeric_value=99.0, record_hash=record_hash "
                    "WHERE batch_id=? AND test_name='维生素C含量'", (b,))
        raw.commit()
    finally:
        raw.close()

    rep = services.full_integrity_report(db, b)
    print(f"   完整性校验结果: ok={rep['ok']}")
    for mm in rep["records"]["mismatches"]:
        print(f"      ✗ {mm['entity']}#{mm['id']}: {mm['reason']}")
    print("   >> 即使攻击者只改值不重算哈希（更常见），record_hash 复算立刻暴露；")
    print("      若同时重算了记录哈希，批次级 state_hash 与审计链中的历史 after_data 快照仍会暴露。")

    header("演示完成")
    print(f"   数据库: {db}")
    print("   启动界面查看: python run.py --db " + os.path.relpath(db))


if __name__ == "__main__":
    main()
