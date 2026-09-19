# 药品批放行管理系统（Batch Release Management）

构建**电子批记录（EBR, Electronic Batch Record）**，把**投料称量、工艺执行、QC 检验**三类数据
串联到同一张批记录上；放行前由规则引擎逐核对，异常批次被**系统硬阻断**；
全过程（包括被拦下的放行尝试）写入 **SHA-256 哈希链审计追踪**，记录冻结由数据库触发器强制。

> 仅依赖 Python 3.11 标准库（`http.server` + `sqlite3`），前端为原生 JS，零安装、零构建。
> 业务品种（维生素 C 片）与限度为教学示例，非真实注册标准。

## 快速开始

```bash
cd batch-release
python3 run.py                      # 初始化库 + 种子数据，监听 :8000
# 浏览器打开 http://localhost:8000
```

预置账号（生产环境请改密）：

| 账号 | 角色 | 口令 | 职责 |
|---|---|---|---|
| `op01` / `op02` | 操作工 | `Op@12345` | 称量、工艺参数录入（两人互为复核） |
| `pl01` | 生产班长 | `Pl@12345` | 结束步骤、填报收率、结束生产 |
| `qc01` / `qc02` | QC 检验员 | `Qc@12345` | 检验录入、第二人复核、提交 QA |
| `qa01` | QA 质保 | `Qa@12345` | 偏差调查/CAPA、退回补检、电子签名放行/拒绝 |
| `admin` | 管理员 | `Ad@12345` | 全权限 |

跑剧情脚本（自动构造正常批 / 异常阻断批 / 纠偏放行批 / 篡改检测）：

```bash
python3 scripts/demo.py
```

测试：

```bash
python3 -m unittest discover -s tests -v     # 18 个测试（服务层规则 + 真实 HTTP 端到端）
```

## 业务闭环

```
创建批次 → 配料称量(双人复核) → 工艺步骤/参数(超限自动开偏差)
       → 班长填报收率结束生产 → QC 取样检验(双人复核，OOS 自动开偏差)
       → 提交 QA（EBR 整体冻结）
       → 规则引擎核对 ──有阻断──► QA 偏差调查：接受+CAPA / 判废 / 退回补检
                └──无阻断──► QA 重输口令电子签名 → 放行（终态）
```

### 规则引擎一票否决（`services.evaluate_blockers`）

| 规则码 | 检查内容 | 等级 |
|---|---|---|
| `WEIGH_MISSING` | BOM 每种物料都有称量记录 | critical |
| `WEIGH_NOT_CHECKED` | 投料必须第二人复核（复核人 ≠ 称量人） | major |
| `WEIGH_OUT_OF_TOLERANCE` | 实际投料在 BOM 公差内（默认 ±3%，硬脂酸镁/包材 ±5%，超 5% 升 critical） | major/critical |
| `STEP_INCOMPLETE` / `PARAM_MISSING` | 工艺步骤全部开始并结束、参数全部记录 | critical/major |
| `PARAM_OOS` | 参数在规定限度内（如密封性定性不合格 → critical） | major/critical |
| `YIELD_OOS` | 实际收率在放行窗口 97%~101% | major |
| `QC_MISSING` / `QC_NOT_CHECKED` | 全部质量标准项已检且双人复核 | critical/major |
| `QC_OOS` | 检验结果符合标准（关键项如含量/溶出度/微生物 → critical） | 按质量标准风险 |
| `DEVIATION_OPEN` | 无未关闭偏差（自动 + 人工） | 按偏差等级 |
| `DEVIATION_REJECTED` | QA 判废/退回的批次**永久不得放行** | critical |

数据不符合项与偏差档案联动：产生即自动开偏差（带去重键）；
QA 关闭为 **accepted（含 CAPA 摘要）→ 阻断解除**；关闭为 **rejected → 永久阻断**。
未关闭一律阻断。QA 放行时即使口令正确、即使是管理员，也无法越过引擎。

### 电子签名（21 CFR Part 11 思路）

放行/拒绝均需 QA 重新输入口令二次认证，签名记录为
`用户名#姓名#UTC时间戳`，与放行意见一起进审计链。

## 审计链与防篡改（四道防线）

1. **审计日志只增**：`trg_audit_no_update/delete` 触发器直接拒绝任何 UPDATE/DELETE，
   DBA 直连改库也会被 SQLite 拒绝。
2. **EBR 冻结**：批次进入 `pending_review/released/rejected` 后，
   投料/工艺/QC 记录的 INSERT/UPDATE/DELETE 全部被触发器拒绝。
3. **记录指纹**：每条业务记录保存
   `sha256(canonical(关键字段))`，「完整性校验」页签可逐条复算；
   另对整批记录串联出批次状态指纹。
4. **哈希链**：审计条目 `entry_hash = sha256(prev_hash | canonical(本条内容))`，
   任何插入/删除/改写都会在重放时断链。即使攻击者 DROP 触发器再改库，
   记录指纹复算立即暴露（`scripts/demo.py` 末尾有现场演示）。

特别地：**被阻断的放行尝试在独立事务中先落审计**，再返回 409——
否则异常回滚会抹掉"谁试图强行放行不合格批次"这一关键证据。

## API 摘要

```
POST /api/auth/login | POST /api/auth/logout | GET /api/me
GET  /api/catalog                      产品/BOM/工艺/质量标准
GET  /api/batches | POST /api/batches
GET  /api/batches/{id}                 聚合 EBR（投料+工艺+QC+偏差）
POST /api/batches/{id}/start-weighing
POST /api/batches/{id}/weighing       POST /api/batches/{id}/weighing/{wid}/check
POST /api/batches/{id}/steps/start|finish
POST /api/batches/{id}/params
POST /api/batches/{id}/finish-production
POST /api/batches/{id}/qc/start | POST /api/batches/{id}/qc | .../qc/{qid}/check
POST /api/batches/{id}/submit                          提交 QA（冻结）
GET  /api/batches/{id}/review                          规则引擎核对结果
POST /api/batches/{id}/release                         QA 电子签名放行
POST /api/batches/{id}/reject                          QA 拒绝放行
POST /api/batches/{id}/return-retest                   QA 退回补检（解冻，留痕）
GET  /api/batches/{id}/deviations | POST ...（人工偏差）
POST /api/deviations/{id}/close                        QA 处置 accepted/rejected + CAPA
GET  /api/deviations
GET  /api/audit | GET /api/integrity | GET /api/batches/{id}/integrity
```

## 目录

```
batch-release/
├── run.py                 启动入口
├── app/
│   ├── schema.sql         表结构 + 冻结/只增触发器
│   ├── master_data.py     品种/BOM/工艺/QC 标准/账号
│   ├── database.py        连接、事务、审计链追加
│   ├── hashing.py         规范化序列化 / SHA-256 记录指纹与链哈希
│   ├── auth.py            PBKDF2 口令、会话令牌、RBAC
│   ├── services.py        EBR、规则引擎、偏差/CAPA、放行、完整性校验
│   ├── server.py          标准库 HTTP REST + 静态托管
│   └── static/            原生 JS 前端（EBR 工作台/偏差台/审计/完整性）
├── scripts/demo.py        三批次剧情 + 篡改检测演示
└── tests/test_system.py   18 个测试
```

## 实现取舍说明

- **EBR 记录只许追加、不许覆盖**：错了走"退回补检 + 重录 + 审计"而不是 UPDATE 原记录，
  这与纸质批记录杠改留痕的监管预期一致。
- **自动偏差带去重键**（`weigh:{批次}:{物料}` 等）：同一不符合项只产生一条偏差，
  重复提交不会刷屏，且规则引擎靠它找到调查结论。
- **SQLite 触发器作为最后一道闸**：应用层校验可能被绕过，触发器不会；
  但触发器本身可被 DROP，所以仍有记录指纹 + 哈希链兜底——纵深防御，不把安全押在单层。
- **收率、双人复核、关键步骤完成度都在"提交 QA"和"放行"两处重复校验**：
  前者给操作员友好的完整性提示，后者是不可绕过的最终闸门。
