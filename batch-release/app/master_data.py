"""主数据：产品、物料、BOM、工艺步骤/参数标准、QC 质量标准。

以“维生素 C 片 100mg”为示范品种（工艺与限度为教学示例，非真实注册标准）。
批量：100,000 片。
"""

PRODUCT = {
    "code": "P-VC-100",
    "name": "维生素C片",
    "dosage_form": "片剂",
    "strength": "100mg",
    "batch_size": 100_000,
}

# (物料编码, 名称, 规格, 类别, 理论投料量, 单位, 允许偏差%)
BOM = [
    ("M-API-001", "维生素C原料药", "符合CP标准", "raw",       10.50, "kg", 3.0),
    ("M-EXC-101", "微晶纤维素",   "PH-102",   "raw",        5.20, "kg", 3.0),
    ("M-EXC-102", "硬脂酸镁",     "药用级",    "raw",        0.26, "kg", 5.0),
    ("M-PKG-201", "铝塑泡罩包装", "PVC/PVDC", "packaging", 12.00, "kg", 5.0),
]

# 工艺步骤：步骤号, 名称
PROCESS_STEPS = [
    (10, "原辅料配料与称量"),
    (20, "总混"),
    (30, "压片"),
    (40, "铝塑包装"),
]

# 工艺参数：步骤号 -> [(参数名, 下限, 上限, 单位, 是否定性)]
PROCESS_PARAMS = {
    20: [
        ("混合时间", 20.0, 30.0, "min", False),
        ("混合转速", 12.0, 18.0, "rpm", False),
    ],
    30: [
        ("平均片重", 0.182, 0.190, "g", False),
        ("片重差异", None, 3.0, "%", False),
        ("硬度", 50.0, 100.0, "N", False),
    ],
    40: [
        ("密封性", None, None, None, True),
    ],
}

# QC 项目：(检验名, 类型, 下限, 上限, 单位, 风险等级)
QC_SPECS = [
    ("性状",          "pass_fail", None, None, None, "major"),
    ("维生素C含量",    "numeric",   95.0, 105.0, "%", "critical"),
    ("溶出度",        "numeric",   80.0, None,  "%", "critical"),
    ("片重差异",       "numeric",   None, 5.0,   "%", "major"),
    ("水分",          "numeric",   None, 5.0,   "%", "major"),
    ("微生物限度",     "pass_fail", None, None, None, "critical"),
]

# 放行收率窗口
YIELD_LOWER = 97.0
YIELD_UPPER = 101.0

# 系统账号（演示用，生产必须改密）：用户名, 姓名, 角色, 初始密码
USERS = [
    ("op01",   "王操作", "operator",        "Op@12345"),
    ("op02",   "李操作", "operator",        "Op@12345"),
    ("pl01",   "赵班长", "production_lead", "Pl@12345"),
    ("qc01",   "孙检验", "qc",              "Qc@12345"),
    ("qc02",   "周复核", "qc",              "Qc@12345"),
    ("qa01",   "钱质保", "qa",              "Qa@12345"),
    ("admin",  "系统管理员", "admin",        "Ad@12345"),
]
