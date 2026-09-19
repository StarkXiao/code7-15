# 设计说明：药品批放行管理系统（EBR）

## 1. 目标与范围

把一批药从**投料 → 工艺执行 → 质量检验 → QA 评审 → QP 放行**的全过程电子化：

- 以**电子批记录（EBR）**串联 BOM、工艺参数标准、质量标准与批次执行数据；
- 异常（投料超差、关键工艺参数超标 OOT、检验超标 OOS、收率超限、偏差未闭环、
  CAPA 未验证有效等）由规则引擎**硬性阻断放行**；
- 所有操作（包括越权拒绝、放行被阻断）写入**哈希链式审计追踪**，可独立校验、防篡改；
- 放行/拒放需受权放行人(QP) 或 QA **电子签名**（签名含义声明 + 意见必填）。

实现刻意只依赖 Python 3.11 标准库 + SQLite，零第三方安装。

## 2. 角色与职责分离（SoD）

| 角色 | 职责 |
|---|---|
| `OPERATOR` | 建批、开工、称配投料、记录工艺参数（只能操作，不能复核自己的记录） |
| `PRODUCTION_LEAD` | 投料复核、完工报交、工艺记录、担任 CAPA 责任人 |
| `ANALYST` | QC 取样检验（不能复核自己的检验结果） |
| `QA` | 投料/检验复核、驳回整改、偏差立案与关闭、CAPA 制定与有效性验证、拒放 |
| `QP` | 受权放行人：全系统**唯一**能执行放行电子签名的角色；也可拒放 |
| `ADMIN` | 受控主数据维护、账号（不参与业务放行） |

数据库层与服务层双重保证：称量人≠复核人、检验人≠复核人、CAPA 责任人≠验证人。

## 3. 批次状态机

```
DRAFT ──开工(OPERATOR/LEAD)──▶ IN_PRODUCTION
                                  │  完工报交(LEAD/QA)，登记实际产量
                                  ▼
                              PENDING_QA ──放行(QP, 电子签名)──▶ RELEASED（终态）
                                  │  └──拒放(QA/QP, 电子签名)──▶ REJECTED（终态）
                                  └──驳回整改(QA, 必填原因)──▶ IN_PRODUCTION
```

- 每次迁移使用**带条件的 UPDATE**（`WHERE status=?`），并发下不会重复迁移；
- 终态 `RELEASED/REJECTED` 不可再写入任何批记录；
- QA 驳回整改必须填写原因，原因随审计永久保存。

## 4. 放行门禁（10 道，全部通过才放行）

| # | 门禁 | 阻断条件 |
|---|---|---|
| 1 | `BOM_COMPLETENESS` | BOM 中存在未投料物料 |
| 2 | `DISPENSING_TOLERANCE` | 实投量超出"理论量±公差%"（物料平衡） |
| 3 | `DISPENSING_VERIFICATION` | 投料记录缺第二人复核 |
| 4 | `PROCESS_PARAMETERS` | CPP 未记录/实测超限(OOT)；超标须有 **CLOSED_EFFECTIVE** 偏差覆盖 |
| 5 | `QC_TESTS_COMPLETE` | 质量标准项目缺生效检验记录或缺复核 |
| 6 | `QC_RESULTS_CONFORM` | 存在生效中的 **OOS**；化验室差错须偏差判定作废后重测合格 |
| 7 | `YIELD` | 成品收率超出注册工艺范围；超标须有有效偏差 |
| 8 | `DEVIATIONS` | 偏差仍 OPEN，或关闭申请被驳回(CLOSED_REJECTED) |
| 9 | `CAPA` | MAJOR/CRITICAL 偏差没有 CAPA，或 CAPA 未被 QA 验证为 EFFECTIVE |
| 10 | `SEGREGATION_OF_DUTIES` | 自配自核/自检自核、缺开完工时间等记录完整性问题 |

关键设计原则：

- **阻断不可绕过**。`service.release_batch()` 方法签名上没有任何 `force/override`
  参数；门禁评估在状态迁移之前执行，失败即抛 `RELEASE_BLOCKED`，批次保持
  `PENDING_QA`。
- **异常的合规出口是偏差流程，而不是放行**。OOT/收率超标可用"已验证有效的偏差"
  覆盖；真实 OOS **任何情况都不能放行**，只能拒放（偏差无法覆盖 QC_RESULTS_CONFORM）。
- 化验室差错 OOS 的唯一处理路径：立偏差 → QA 判定 `is_lab_error` 关闭 →
  原 OOS 记录作废（保留，不删除）→ 重新取样检验 → 双人复核合格。
- 放行时刻，10 道门禁的完整快照（JSON）随电子签名一起冻结。

## 5. 偏差与 CAPA 生命周期

```
偏差 OPEN ──QA 关闭──▶ CLOSED_EFFECTIVE      （根因确认；可附带化验室差错作废）
                    └▶ CLOSED_REJECTED       （调查不充分/结论不成立，继续阻断）

CAPA OPEN ──责任人实施──▶ CLOSED_PENDING ──QA 验证──▶ EFFECTIVE（门禁认可）
                                              └────▶ INEFFECTIVE（继续阻断，须重做）
```

- MINOR 偏差有效关闭即可；MAJOR/CRITICAL 必须挂 CAPA 且验证 EFFECTIVE；
- CAPA 验证人不得是责任人本人。

## 6. 审计链（防篡改的核心）

每条审计记录保存：

```
entry_hash = SHA256( prev_hash | ts | actor | action | entity | result | reason | details )
```

- 首条的 `prev_hash` 为 64 个 0（创世哈希）；每条指向前一条，形成单链；
- `audit_log` 表挂有 `BEFORE UPDATE/DELETE` 触发器，数据库层拒绝改删；
- 即便攻击者绕过应用、卸掉触发器直接改库文件，`verify_chain()` 逐条重算哈希
  即可在被改记录处定位断裂（演示脚本实测）；
- 业务写与审计写在**同一事务**提交，不存在"做了没记"的窗口；
- 结果分四类：`SUCCESS / DENIED（越权）/ BLOCKED（门禁阻断）/ FAILED（超标等）`，
  登录失败、自配自核被拒、放行尝试被拦全部留痕；
- 放行签名记录 (`release_decisions`) 另有独立的 `sig_hash` 链，双链互证。

## 7. 数据模型

```
users
products ──┬─ product_bom_items ── materials
           ├─ process_parameters       （CPP 工艺标准：步骤/参数/限度/是否关键）
           └─ qc_specs                 （CQA 质量标准：数值型双侧/单侧限度或文本型）
batches ──┬─ dispensing_records        （称配 + 双人复核）
          ├─ process_records           （参数实际值 + 记录人，含当时标准快照字段）
          ├─ qc_records                （检验值/结论 PASS|OOS/作废/复核）
          ├─ deviations ── capas
          └─ release_decisions         （电子签名 + 门禁快照 + 哈希链）
audit_log                                （全局哈希链，只增不删）
```

工艺与 QC 记录在写入时把当时的限度/单位/关键标志**冗余快照**进记录行，
之后主数据修订不会改变历史批次的判定依据。

## 8. 代码地图

```
ebr/
  config.py        角色、状态机迁移表、PBKDF2 参数
  errors.py        业务异常（ReleaseBlockedError 等）
  db.py            SQLite 连接、全部建表 DDL 与防篡改触发器
  auth.py          PBKDF2 口令派生、会话 token、登录审计
  audit.py         哈希链式审计追踪（record/list/verify_chain）
  masterdata.py    物料/产品/BOM/CPP 标准/CQA 标准（受控文件，ADMIN/QA）
  rules.py         纯函数规则引擎：build_bundle + 10 道门禁
  service.py       状态机、EBR 全部写操作、偏差/CAPA、电子签名放行
  api/server.py    零依赖 HTTP API（标准库 http.server）
scripts/
  seed.py          演示主数据 + 6 个角色账号
  demo.py          三批次命运端到端演示（含篡改检测）
tests/             26 个 unittest 用例
```

## 9. 与生产系统的差距（刻意的演示取舍）

- 会话存内存（重启失效），生产应换 Redis/DB 并设过期；
- 电子签名用"已认证会话 + 必填签名含义/意见"表达 21 CFR Part 11 的
  签名组成，生产环境应在签名时**再次输入口令**做再认证；
- 无前端 UI，仅提供 REST API；
- 单节点 SQLite；门禁引擎是纯函数，迁到 PostgreSQL 等只需替换 `db.py`。
