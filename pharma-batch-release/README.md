# 药品批放行管理系统（EBR / Batch Release）

构建**电子批记录**，串联投料、工艺与质检数据；异常批次由门禁规则引擎**硬性阻断放行**，
全部行为（含越权与阻断尝试）留**哈希链式审计链**。

- Python 3.11 标准库 + SQLite，**零第三方依赖**，克隆即用
- 符合 GMP / 附录《确认与验证》思路的状态机、双人复核、职责分离、电子签名
- 10 道放行门禁覆盖：BOM 完整性、物料平衡公差、双人复核、CPP、QC 完成度、
  OOS、收率、偏差、CAPA 有效性、职责分离
- SHA-256 哈希链 + 数据库触发器双重防篡改，可随时独立校验

## 快速开始

```bash
cd pharma-batch-release

python3 scripts/seed.py                 # 初始化库 + 阿莫西林胶囊主数据 + 6 个角色账号
python3 -m unittest discover -s tests   # 26 个测试
python3 scripts/demo.py                 # 三批次命运端到端演示（含篡改检测）

EBR_PORT=8080 python3 -m ebr.api.server # 启动 HTTP API
```

### 演示账号（seed 生成，仅演示用）

| 账号 | 口令 | 角色 |
|---|---|---|
| `zhang.gong` | operator123 | 操作员 |
| `li.chejian` | lead123 | 生产负责人 |
| `wang.huayan` | analyst123 | 化验员 |
| `zhao.qa` | qa123 | QA |
| `sun.qp` | qp123 | **受权放行人 QP**（唯一放行签名人） |
| `admin` | admin123 | 管理员（主数据） |

## 一分钟看懂系统在做什么

```
建批 DRAFT → 开工 → 投料(称配+第二人复核) → 记录CPP工艺参数
        → QC 检验(化验+第二人复核) → 完工报交 PENDING_QA
        → QA 评审：有异常走偏差/CAPA 闭环，或驳回整改
        → QP 电子签名：10 道门禁全过才 RELEASED；任一不过硬性阻断
                       真实 OOS 等不可放行批次 → QA/QP REJECTED
```

## curl 示例

```bash
# 登录
TOKEN=$(curl -s -X POST localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"sun.qp","password":"qp123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 查看批次门禁
curl -s -X POST localhost:8080/api/batches/B-DEMO-02/evaluate \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# QP 电子签名放行（门禁不过返回 409 RELEASE_BLOCKED，无任何绕过参数）
curl -s -X POST localhost:8080/api/batches/B-DEMO-02/release \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"reason":"电子批记录审核合格，同意放行"}'

# 校验审计哈希链
curl -s -X POST localhost:8080/api/audit/verify -H "Authorization: Bearer $TOKEN"
```

完整接口见 [`docs/api.md`](docs/api.md)，设计细节见 [`docs/design.md`](docs/design.md)。

## 演示脚本覆盖的三条批次命运

| 批号 | 情形 | 结局 |
|---|---|---|
| B-DEMO-01 | 投料/工艺/质检全部合规 | 门禁全过 → QP 电子签名 **RELEASED** |
| B-DEMO-02 | 干燥进风温度 CPP 超标(OOT) → 两次放行被阻断 → MAJOR 偏差 + CAPA 首次验证无效退回 → 重新实施验证有效 | **RELEASED** |
| B-DEMO-03 | 成品含量 92.6% 真实 OOS + CRITICAL 偏差未闭环 | 放行被**硬性阻断** → QA 电子签名 **REJECTED** |

脚本最后直接篡改数据库（模拟绕过应用改库文件），`verify_chain()` 立刻在被改记录处报警。

## 目录

```
pharma-batch-release/
├── ebr/            核心包（db / auth / audit / masterdata / rules / service / api）
├── scripts/        seed.py 主数据种子，demo.py 端到端演示
├── tests/          26 个 unittest（状态机/门禁/偏差CAPA/审计链/HTTP）
└── docs/           design.md 设计说明，api.md API 契约
```

## 几条关键的实现取舍

- **阻断逻辑放在纯函数规则引擎**（`rules.py`），与写入和 HTTP 完全解耦，
  放行前内联评估；服务层没有也不可能提供 force 开关。
- **业务 SQL 与审计 INSERT 同一事务**，要么一起成功要么一起回滚。
- **状态迁移用条件 UPDATE 抢占**（`... WHERE status=?`），不靠先查后改，
  并发下不会出现两次放行。
- **标准随记录快照**：工艺/QC 记录写入时冗余当时限度，主数据修订不改写历史。
- **审计只增不删**：触发器拦 UPDATE/DELETE，SHA-256 链再拦物理篡改，双保险。
