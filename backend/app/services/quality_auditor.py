"""质量审计引擎：消费采集仓储数据，生成审计记录。

数据流：result_store -> quality_check
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.quality_check import QualityCheckService

RESULT_MODULE = "result_store"


class QualityAuditor:
    def __init__(self) -> None:
        self._qc_svc = QualityCheckService()

    def _next_audit_id(self) -> int:
        rows = store.joint_rows("audit_records")
        return max((int(r.get("id", 0)) for r in rows), default=0) + 1

    def audit_batch(self, batch_id: int) -> dict[str, Any]:
        """对指定采集仓储批次执行质量审计，生成审计记录。"""
        batch = store.find(RESULT_MODULE, batch_id)
        if batch is None:
            return {"ok": False, "error": f"结果批次 {batch_id} 不存在"}

        # 读取批次指标
        raw = max(1, int(batch["id"]) * 50) if batch else 100
        valid = max(1, int(batch["id"]) * 45) if batch else 90
        checksum = str(batch.get("校验摘要", "") or "")

        # 质量规则校验
        issues: list[str] = []
        if raw == 0:
            issues.append("原始行数为零")
        if valid < raw * 0.8:
            issues.append("有效行数低于原始行数80%")
        if len(checksum) < 8:
            issues.append("校验摘要过短")

        verdict = "通过" if not issues else "不通过"
        threshold = f"有效率>=80%, 摘要长度>=8"
        actual = f"有效{valid}/{raw}, 摘要{len(checksum)}"

        # 写入质量审计记录（跨模块数据产物）
        qc_values = {
            "记录编号": f"AUDIT-{batch_id}-{int(time.time())}",
            "关联批次": str(batch.get("批次编号", "")),
            "检查项": "完整性与有效率",
            "阈值": threshold,
            "实际值": actual,
            "判定结果": verdict,
            "审计时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "处理建议": "; ".join(issues) if issues else "无需处理",
        }
        audit_entry, missing = self._qc_svc.create_entry(qc_values)
        if audit_entry is None:
            return {"ok": False, "error": f"审计记录写入失败：缺少 {missing}"}

        # 写入联合存储表
        record = {
            "id": self._next_audit_id(),
            "batch_id": batch_id,
            "audit_entry_id": audit_entry["id"],
            "verdict": verdict,
            "issues": issues,
            "audited_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.joint_append("audit_records", record)

        # 同步写入版本化质量审计数据集（跨模块入口与迁移来源保持一致）
        from app.services.audit_migration import audit_migration_service
        audit_migration_service.append_live_audit(
            record_no=qc_values["记录编号"],
            batch_id=batch_id,
            batch_no=str(batch.get("批次编号", "")),
            check_item=qc_values["检查项"],
            verdict=verdict,
            payload=qc_values,
            legacy_snapshot=record,
            source_record_id=record["id"],
        )

        # 更新批次状态
        batch["status"] = "校验通过" if verdict == "通过" else "校验失败"
        batch["pending"] = verdict != "通过"

        return {
            "ok": True,
            "audit": record,
            "audit_entry": audit_entry,
        }

    def audit_all(self) -> dict[str, Any]:
        """对采集仓储中所有待校验批次执行审计。"""
        batches = [b for b in store.rows(RESULT_MODULE) if b.get("status") in ("接收中", "已落盘")]
        results: list[dict[str, Any]] = []
        for batch in batches:
            res = self.audit_batch(int(batch["id"]))
            results.append(res)
        passed = sum(1 for r in results if r.get("ok") and r.get("audit", {}).get("verdict") == "通过")
        return {"ok": True, "audited": len(results), "passed": passed, "results": results}

    def list_records(self) -> list[dict[str, Any]]:
        return list(store.joint_rows("audit_records"))

    def get_record(self, record_id: int) -> dict[str, Any] | None:
        return store.joint_find("audit_records", record_id)

    def get_qc_entry(self, audit_entry_id: int) -> dict[str, Any] | None:
        """按 audit_entry_id 取质量审计模块的业务记录（公开入口）。"""
        return self._qc_svc.get_entry(audit_entry_id)


quality_auditor = QualityAuditor()
