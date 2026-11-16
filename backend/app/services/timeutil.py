"""时间与列表字段的解析工具：时间线计算里到处都要用，单独收一处。"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_dt(value: object) -> datetime | None:
    """把日期时间字段解析成 naive datetime；解析不了返回 None，不抛异常。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("/", "-")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def fmt_dt(value: object) -> str:
    """统一按分钟精度输出，保证节点排序和展示口径一致。"""
    parsed = parse_dt(value)
    if parsed is None:
        return ""
    return parsed.strftime("%Y-%m-%d %H:%M")


def parse_int(value: object, default: int | None = None) -> int | None:
    """容忍 "60 分钟" 这类带单位的写法，只取前面的整数。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return default
    digits = ""
    for char in text:
        if char.isdigit() or (char == "-" and not digits):
            digits += char
        elif digits:
            break
    try:
        return int(digits)
    except ValueError:
        return default


def parse_jobs(value: object) -> list[str]:
    """关联作业列表既可能是逗号分隔文本，也可能是数组，统一成去重列表。"""
    if isinstance(value, (list, tuple, set)):
        raw = [str(item) for item in value]
    else:
        text = str(value or "")
        for separator in ("；", ";", "，", "、", "|"):
            text = text.replace(separator, ",")
        raw = text.split(",")
    jobs: list[str] = []
    for item in raw:
        name = item.strip()
        if name and name not in jobs:
            jobs.append(name)
    return jobs
