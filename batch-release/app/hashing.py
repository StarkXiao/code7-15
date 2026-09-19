"""规范 JSON 序列化与记录哈希。

GMP 电子记录要求哈希可复算、可验证，因此：
- 统一用 UTF-8、键名字典序、分隔符固定，不允许任何端自己换序列化方式；
- 数值先归一化（数字保留 6 位有效小数，消除 100.0 vs 100.000 差异）。
"""
import hashlib
import json
from typing import Any

NUM_DIGITS = 6


def canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def norm_num(v: Any) -> Any:
    """把数字归一成稳定字符串形式；None / 布尔原样保留。"""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return f"{float(v):.{NUM_DIGITS}f}"
    return v


def norm_dict(d: dict) -> dict:
    return {k: norm_num(v) for k, v in d.items()}


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_hash(fields: dict) -> str:
    """单条业务记录指纹：sha256(canonical(归一化字段))。"""
    return sha256_hex(canon(norm_dict(fields)))


def chain_hash(prev_hash: str, payload: dict) -> str:
    """审计链下一条哈希：sha256(prev_hash | canonical(payload))。"""
    return sha256_hex(prev_hash + canon(payload))
