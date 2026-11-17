"""质量审计接口：时间线 + 记录版本化迁移、冲突确认与统一读取。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.audit_migration import (
    DEFAULT_POLICY,
    POLICY_ARCHIVE,
    POLICY_MANUAL,
    audit_migration_service,
)
from app.services.quality_check import QualityCheckService

router = APIRouter(prefix="/api/quality_check", tags=["质量审计"])
service = QualityCheckService()
LIST_FIELDS = ["记录编号", "关联批次", "检查项", "阈值", "实际值", "判定结果", "审计时间", "处理建议"]
STATUSES = ["待审计", "已通过", "需修复", "已忽略"]


class MigrationPayload(BaseModel):
    conflict_policy: str = DEFAULT_POLICY


class ConflictResolvePayload(BaseModel):
    resolution: str  # use_incoming / keep_current


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def list_entries() -> dict[str, Any]:
    items = service.get_timeline()
    return {"items": items}


# ---------- 版本化迁移 ----------

@router.get("/migration/status")
def migration_status() -> dict[str, Any]:
    """迁移来源同步状态：旧数组文件、新版信封文件、已迁移/待迁移来源。"""
    return audit_migration_service.migration_status()


@router.post("/migration/run")
def run_migration(payload: MigrationPayload) -> dict[str, Any]:
    """批量处理全部旧文件与新版信封，逐条核对、整组一次提交。

    幂等：重复导入不产生重复审计版本。
    """
    if payload.conflict_policy not in (POLICY_ARCHIVE, POLICY_MANUAL):
        raise HTTPException(status_code=400,
                            detail="conflict_policy 只能是 archive 或 manual")
    try:
        result = audit_migration_service.migrate(
            conflict_policy=payload.conflict_policy, trigger="manual"
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"迁移失败已整组回滚：{exc}")
    # 迁移完成后返回质量审计详情快照（前端据此刷新）
    result["audit_snapshot"] = audit_migration_service.read_all()
    return result


@router.get("/versions")
def list_versions() -> dict[str, Any]:
    """质量审计读取服务：同时读取未迁移记录与新版数据集。"""
    return audit_migration_service.read_all()


@router.get("/records/{record_no}/detail")
def record_detail(record_no: str) -> dict[str, Any]:
    """质量审计详情：当前版本 + 全部历史版本 + 冲突状态。"""
    detail = audit_migration_service.read_detail(record_no)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"审计记录 {record_no} 不存在")
    return detail


@router.get("/conflicts")
def list_conflicts() -> dict[str, Any]:
    """冲突清单：新旧值不一致、等待人工确认的条目。"""
    return {"items": audit_migration_service.list_conflicts()}


@router.post("/conflicts/{envelope_id}/resolve")
def resolve_conflict(envelope_id: int, payload: ConflictResolvePayload) -> ActionResult:
    res = audit_migration_service.resolve_conflict(envelope_id, payload.resolution)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    return ActionResult(ok=True,
                        message=f"冲突已按「{payload.resolution}」确认，历史版本已保留",
                        entry=res.get("envelope"))


# ---------- 导出（落库闸门） ----------

@router.get("/export")
def export_entries() -> StreamingResponse:
    """导出新版质量审计记录数据集；记录落库后才能导出文件供下载。"""
    try:
        items = audit_migration_service.export_rows()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = [
        "id", "record_no", "version", "is_current", "lifecycle",
        "batch_id", "batch_no", "check_item", "verdict",
        "source_type", "source_file", "audited_at", "migrated_at",
    ]
    writer.writerow(all_keys)
    for item in items:
        source = item.get("source") or {}
        writer.writerow([
            csv_cell(item.get("id", "")),
            csv_cell(item.get("record_no", "")),
            csv_cell(item.get("version", "")),
            csv_cell(item.get("is_current", "")),
            csv_cell(item.get("lifecycle", "")),
            csv_cell(item.get("batch_id", "")),
            csv_cell(item.get("batch_no", "")),
            csv_cell(item.get("check_item", "")),
            csv_cell(item.get("verdict", "")),
            csv_cell(source.get("type", "")),
            csv_cell(source.get("file", "")),
            csv_cell(item.get("audited_at", "")),
            csv_cell(item.get("migrated_at", "")),
        ])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=quality_check.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"审计记录 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="审计记录已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
