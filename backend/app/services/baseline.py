"""基线管理业务规则：状态流转、字段校验、版本承接与筛选口径都收在这里。

基线定义的承诺完成时间、预警余量等字段一旦被修改，会生成一个新版本并承接上一版，
时间线按运行结果的到达时间选择归属版本，因此历史节点不会被新版本改写。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services.baseline_timeline import timeline_service
from app.services.timeutil import fmt_dt, parse_dt, parse_int, parse_jobs
from app.store import store

MODULE = "baseline"
VERSION_MODULE = "_baseline_version"
REQUIRED_FIELDS = ["基线编号", "基线名称", "承诺完成时间"]
STATUS_ORDER = ["草稿", "基线评审中", "已启用", "已停用"]
ACTION_RULES = {"提交基线评审": "基线评审中", "启用基线": "已启用", "停用基线": "已停用"}
NEGATIVE_ACTIONS = ["停用基线"]
VERSIONED_FIELDS = ["承诺完成时间", "预警余量分钟", "破线级别", "关联作业列表", "工作日标记", "节假日偏移"]


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class BaselineService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("基线编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.transaction() as tx_id:
            rows = store.rows(MODULE)
            baseline_no = str(values.get("基线编号")).strip()
            if any(str(row.get("基线编号")) == baseline_no for row in rows):
                return None, [f"基线编号 {baseline_no} 已存在"]
            entry = {"id": _next_id(rows)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in VERSIONED_FIELDS:
                if field not in entry:
                    entry[field] = values.get(field)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["当前版本"] = 1
            entry["最近时间线交易号"] = tx_id
            rows.append(entry)
            self._create_version(entry, values, tx_id=tx_id, note="基线登记初版")
            timeline_service.invalidate()
            return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"基线定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于基线管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with store.transaction():
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
        timeline_service.invalidate()
        return entry, f"基线定义已{action}"

    # ------------------------------------------------------------------ 版本
    def _create_version(
        self,
        baseline: dict[str, Any],
        values: dict[str, Any],
        *,
        tx_id: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        rows = store.rows(VERSION_MODULE)
        baseline_no = str(baseline.get("基线编号", ""))
        existing = [row for row in rows if row.get("基线编号") == baseline_no]
        number = max((parse_int(row.get("版本号"), 0) or 0 for row in existing), default=0) + 1
        parent = max((parse_int(row.get("版本号"), 0) or 0 for row in existing), default=0) or None
        version = {"id": _next_id(rows), "基线编号": baseline_no, "版本号": number,
                   "生效时间": fmt_dt(values.get("生效时间") or datetime.now()),
                   "承接版本": parent, "变更说明": note or values.get("变更说明") or "",
                   "交易号": tx_id}
        for field in VERSIONED_FIELDS:
            version[field] = values.get(field, baseline.get(field))
        rows.append(version)
        baseline["当前版本"] = number
        return version

    def list_versions(self, entry_id: int) -> list[dict[str, Any]] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        baseline_no = str(entry.get("基线编号", ""))
        rows = [row for row in store.rows(VERSION_MODULE) if row.get("基线编号") == baseline_no]
        return sorted(rows, key=lambda row: parse_int(row.get("版本号"), 0) or 0)

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改基线字段：字段确实变化时生成新版本，历史运行结果仍挂在原版本上。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"基线定义 {entry_id} 不存在或已归档"
        changes = {
            field: values.get(field)
            for field in VERSIONED_FIELDS
            if field in values and str(values.get(field) or "") != str(entry.get(field) or "")
        }
        if not changes:
            return entry, "基线定义没有字段变化，未生成新版本"
        with store.transaction() as tx_id:
            entry.update(changes)
            version = self._create_version(
                entry,
                values,
                tx_id=tx_id,
                note=f"字段变更：{'、'.join(changes)}",
            )
        timeline_service.invalidate()
        return entry, f"基线定义已更新并生成版本 v{version.get('版本号')}，历史时间线节点保持不变"

    def version_conflicts(self) -> list[dict[str, Any]]:
        """列出同一运行结果可能归属多个版本、从而改变预警时间的情况，便于核对。"""
        conflicts: dict[str, list[dict[str, Any]]] = {}
        for version in store.rows(VERSION_MODULE):
            for job in parse_jobs(version.get("关联作业列表")):
                conflicts.setdefault(job, []).append(version)
        rows: list[dict[str, Any]] = []
        for job, versions in conflicts.items():
            if len(versions) < 2:
                continue
            rows.append({
                "作业名称": job,
                "候选版本": [
                    {
                        "基线编号": version.get("基线编号"),
                        "版本号": version.get("版本号"),
                        "承诺完成时间": fmt_dt(version.get("承诺完成时间")),
                        "预警余量分钟": parse_int(version.get("预警余量分钟"), 0),
                        "预警时间": fmt_dt(
                            (parse_dt(version.get("承诺完成时间")) or datetime.now())
                            - timedelta(minutes=parse_int(version.get("预警余量分钟"), 0) or 0)
                        ),
                        "生效时间": version.get("生效时间"),
                    }
                    for version in sorted(versions, key=lambda row: parse_int(row.get("版本号"), 0) or 0)
                ],
            })
        return rows
