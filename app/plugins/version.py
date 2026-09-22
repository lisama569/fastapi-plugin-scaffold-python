"""极简 semver 兼容判定（不引入第三方依赖）。

支持：精确版本 "1.2.3"；区间 "^1.2.0"（>=1.2.0 且 <2.0.0，主版本为 0 时
要求次版本也相同，即 ^0.2.1 表示 >=0.2.1,<0.3.0）。
"""

from __future__ import annotations


def parse_version(text: str) -> tuple[int, int, int]:
    """解析 x.y.z 形式版本号，非法时抛 ValueError。"""
    parts = text.strip().split(".")
    if len(parts) != 3:
        raise ValueError(f"版本号必须是 x.y.z 形式: {text!r}")
    try:
        nums = tuple(int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"版本号包含非数字段: {text!r}") from exc
    return nums  # type: ignore[return-value]


def satisfies(required: str, actual: str) -> bool:
    """判断 actual 是否满足 required 区间。

    * "1.2.3"：必须完全相等；
    * "^1.2.0"：同主版本且不低于；主版本为 0 时退化为同次版本。
    """
    req = required.strip()
    if req.startswith("^"):
        low = parse_version(req[1:])
        ver = parse_version(actual)
        if low[0] > 0:
            return ver[0] == low[0] and ver >= low
        if low[1] > 0:
            return ver[0] == 0 and ver[1] == low[1] and ver >= low
        return ver == low
    return parse_version(actual) == parse_version(req)
