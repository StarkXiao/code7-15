# API 契约

Base URL: `http://localhost:8080`
鉴权：除 `POST /api/auth/login` 与 `GET /healthz` 外全部需要
`Authorization: Bearer <token>`。

错误响应统一格式：

```json
{ "error": "RELEASE_BLOCKED", "message": "...", "details": { "gates": [ ... ] } }
```

## 认证

### POST /api/auth/login
```json
{ "username": "sun.qp", "password": "qp123" }
```
→ `200 {"token": "...", "user": {"username","display_name","role"}}`
口令错误/账号停用 → `401 AUTH_ERROR`（失败也入审计链）。

## 受控主数据（ADMIN / QA）

| 方法/路径 | 说明 | 关键请求体字段 |
|---|---|---|
| POST /api/master/materials | 建物料 | code, name, spec, category(API/EXCIPIENT/PACKAGING), unit |
| POST /api/master/products | 建产品 | code, name, dosage_form, spec, batch_size, batch_size_unit, yield_lower_pct, yield_upper_pct |
| POST /api/master/bom-items | BOM 行 | product_code, material_code, qty_required, tolerance_pct, sequence_no |
| POST /api/master/process-parameters | 工艺参数标准 | product_code, step_no, step_name, param_name, target, lower_limit, upper_limit, unit, is_critical |
| POST /api/master/qc-specs | 质量标准 | product_code, test_name, test_type(NUMERIC/TEXT), lower_limit, upper_limit, expected_text, unit, is_critical |
| GET /api/materials · GET /api/products | 列表 | — |

数值型 QC 标准支持单侧限度（只给 lower 或 upper，如"溶出度 ≥80%"）。

## 批次执行

| 方法/路径 | 允许角色 | 说明 |
|---|---|---|
| POST /api/batches | OPERATOR/LEAD/QA | 建批 DRAFT，body: product_code, planned_size, batch_no? |
| POST /api/batches/{no}/start | OPERATOR/LEAD | 开工 → IN_PRODUCTION |
| POST /api/batches/{no}/dispensing | OPERATOR | 投料：material_code, qty_actual |
| POST /api/batches/{no}/dispensing/verify | OPERATOR/LEAD/QA | 第二人复核：material_code（不得本人） |
| POST /api/batches/{no}/process-records | OPERATOR/LEAD | step_no, param_name, actual_value |
| POST /api/batches/{no}/qc-results | ANALYST | test_name, numeric_value 或 text_value；自动判 PASS/OOS |
| POST /api/batches/{no}/qc-results/review | ANALYST/QA | test_name；第二人复核（不得本人） |
| POST /api/batches/{no}/complete | LEAD/QA | actual_size；→ PENDING_QA |
| POST /api/batches/{no}/reopen | QA | reason 必填；驳回整改 → IN_PRODUCTION |
| POST /api/batches/{no}/deviations | 生产/检验/QA/QP | category(MINOR/MAJOR/CRITICAL), title, description, source_type(QC_OOS/PROCESS_OOT/YIELD/OTHER), source_ref |

## 偏差与 CAPA

| 方法/路径 | 角色 | body |
|---|---|---|
| POST /api/deviations/{devNo}/close | QA | root_cause, root_cause_confirmed, is_lab_error, outcome(EFFECTIVE/REJECTED) |
| POST /api/capas | QA | dev_no, action, owner_username, due_date |
| POST /api/capas/{id}/implement | 责任人/QA | — |
| POST /api/capas/{id}/verify | QA | effective(bool), note（不得验证自己的措施） |

化验室差错：关闭 QC_OOS 偏差且 `is_lab_error=true, outcome=EFFECTIVE` 时，
`source_ref=qc_record:<id>` 指向的 OOS 记录自动作废，随后可重新检验。

## 放行

### POST /api/batches/{no}/evaluate
返回 10 道门禁明细：

```json
{
  "batch_no": "B-DEMO-02",
  "releasable": false,
  "open_blocking_gates": ["CAPA"],
  "gates": [
    {"id": "CAPA", "name": "重大/严重偏差 CAPA 有效性", "passed": false,
     "failures": ["偏差 DEV-... 的 CAPA 尚未由 QA 确认有效（当前：INEFFECTIVE）"],
     "evidence": {}}
  ]
}
```

### POST /api/batches/{no}/release —— 仅 QP，电子签名
body: `{"reason": "审核合格，同意放行"}`（必填）
- 全部门禁通过 → `200 {"status":"RELEASED", "decision": {...,"sig_hash":...}, "evaluation": {...}}`
- 任一门禁失败 → `409 RELEASE_BLOCKED`，批次不变，阻断事件入审计链；
  **不存在任何绕过参数，QP 也无法放行 OOS 批次**。

### POST /api/batches/{no}/reject —— QA/QP
body: `{"reason": "..."}`（必填）→ `200 {"status":"REJECTED", ...}`

## EBR 与审计

| 方法/路径 | 说明 |
|---|---|
| GET /api/batches/{no}/ebr | 电子批记录完整汇总（标准、投料、工艺、QC、偏差、CAPA、门禁、签名） |
| POST /api/audit | 按 entity_type/entity_ref 检索审计，可带 limit |
| POST /api/audit/verify | 重算全链哈希，返回 `{"ok": true, "entries_checked": n}`；篡改则 500 AUDIT_CHAIN_BROKEN |

## 状态码约定

| 码 | error | 场景 |
|---|---|---|
| 401 | AUTH_ERROR | 未登录/token 失效/口令错 |
| 403 | PERMISSION_DENIED | 角色无权、自配自核、自验 CAPA |
| 404 | NOT_FOUND | 资源不存在 |
| 409 | CONFLICT | 非法状态迁移、重复提交、终态改写 |
| 409 | RELEASE_BLOCKED | 放行门禁未通过（带 gates 明细） |
| 422 | VALIDATION_ERROR | 入参不合法 |
| 500 | AUDIT_CHAIN_BROKEN | 审计哈希链校验失败 |
