"""基线管理接口：维护基线定义与版本，并提供基线健康时间线、破线风险看板与运行结果入口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.baseline import BaselineService
from app.services.baseline_timeline import timeline_service

router = APIRouter(prefix="/api/baseline", tags=["基线管理"])

service = BaselineService()

LIST_FIELDS = ["基线编号", "基线名称", "承诺完成时间", "预警余量分钟", "破线级别", "关联作业列表", "工作日标记", "节假日偏移"]
STATUSES = ["草稿", "基线评审中", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按基线编号检索"),
    status: str | None = Query(default=None, description="草稿、基线评审中、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按基线编号与状态过滤基线管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 注意：固定路径必须声明在 /{entry_id} 之前，否则会被路径参数吞掉。
@router.get("/timeline/overview", response_model=dict)
def timeline_overview() -> dict:
    """时间线概览：按承诺完成时间展示各基线的健康状态与关键水位。"""
    return timeline_service.overview()


@router.get("/timeline/risk-board", response_model=dict)
def timeline_risk_board() -> dict:
    """破线风险看板：按风险级别分栏，并附最近的破线/预警节点。"""
    return timeline_service.risk_board()


@router.get("/timeline/execution", response_model=dict)
def timeline_execution(job: str | None = Query(default=None, description="按作业名称过滤执行视图")) -> dict:
    """供作业执行组件读取：只给执行期需要的水位、缺失分区、预计完成时间。"""
    return timeline_service.execution_view(job=job)


@router.post("/timeline/recompute", response_model=dict)
def timeline_recompute(payload: EntryPayload) -> dict:
    """手动重算时间线；重复计算不会重复写入同一关键节点。"""
    baseline_nos = payload.values.get("基线编号")
    if isinstance(baseline_nos, str):
        baseline_nos = [item.strip() for item in baseline_nos.split(",") if item.strip()]
    result = timeline_service.recompute(
        baseline_nos=baseline_nos if isinstance(baseline_nos, list) else None,
        trigger="手动重算",
    )
    return {
        "ok": True,
        "message": f"时间线已重算，新增 {result['added_nodes']} 个节点",
        **result,
    }


@router.post("/timeline/run-results", response_model=dict)
def timeline_submit_run_result(payload: EntryPayload) -> dict:
    """提交作业运行分区结果：同一作业可能同时更新多条基线的时间线。"""
    return timeline_service.submit_run_result(payload.values)


@router.get("/timeline/version-conflicts", response_model=dict)
def timeline_version_conflicts() -> dict:
    """列出同一作业被多条基线/多个版本引用的情况及其预警时间差异。"""
    rows = service.version_conflicts()
    return {"ok": True, "total": len(rows), "items": rows}


@router.get("/{entry_id}/timeline", response_model=dict)
def get_timeline(entry_id: int) -> dict:
    """按单条基线读取完整时间线与版本归属，节点按时间排列。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"基线定义 {entry_id} 不存在或已归档")
    detail = timeline_service.get_baseline(str(entry.get("基线编号")))
    if detail is None:
        raise HTTPException(status_code=404, detail=f"基线定义 {entry_id} 尚未生成时间线")
    return detail


@router.get("/{entry_id}/versions", response_model=dict)
def list_versions(entry_id: int) -> dict:
    """读取基线版本链；历史运行结果按到达时间归属，不随新版本改写。"""
    versions = service.list_versions(entry_id)
    if versions is None:
        raise HTTPException(status_code=404, detail=f"基线定义 {entry_id} 不存在或已归档")
    return {"ok": True, "total": len(versions), "items": versions}


@router.post("/{entry_id}/versions", response_model=dict)
def create_version(entry_id: int, payload: EntryPayload) -> dict:
    """修改基线字段并生成新版本；未变化时不会凭空产生版本。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return {"ok": False, "message": message, "entry": None}
    return {"ok": True, "message": message, "entry": entry}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条基线定义明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"基线定义 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条基线定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或编号冲突：{'、'.join(missing)}")
    return ActionResult(ok=True, message="基线定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条基线定义执行提交基线评审、启用基线、停用基线；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出基线管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "baseline", "total": total, "items": items}
