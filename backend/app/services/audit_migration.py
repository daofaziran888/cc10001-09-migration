"""质量审计记录版本化迁移服务。

背景：质量审计记录历史上以 JSON 数组保存（``data/audit_records.json`` 等），
字段没有稳定版本；新版记录以「信封」结构写入（``data/audit_envelopes_*.json``），
新旧读取入口对同一批次可能得出不同结论。

本服务负责：

1. 读取旧版 JSON 数组与新版记录信封两类来源；
2. 按记录编号（record_no）映射 版本号 / 关联批次 / 检查项 / 判定结果，
   生成带版本号的质量审计记录信封数据集（联合表 ``audit_envelopes``）；
3. 批量处理多条旧文件、逐条核对、整组一次提交（落盘只有一个原子写入点）；
4. 重复导入幂等：同来源同内容只产生一个审计版本；
5. 冲突不静默覆盖：同一记录编号新旧值不一致时，按策略归档旧数组或进入
   待人工确认冲突清单，历史字段与审计结论始终保留可追溯；
6. 一个记录编号同时只允许有一个 current 版本。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from app.store import store

QC_MODULE = "quality_check"
ENVELOPE_TABLE = "audit_envelopes"
MIGRATION_TABLE = "audit_migrations"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "data")
DATA_DIR = os.path.abspath(DATA_DIR)
# 旧版 JSON 数组来源：data/audit_records.json + data/legacy_audit/*.json
LEGACY_MAIN_FILE = os.path.join(DATA_DIR, "audit_records.json")
LEGACY_DIR = os.path.join(DATA_DIR, "legacy_audit")
# 新版记录信封来源：data/new_envelopes/*.json
ENVELOPE_DIR = os.path.join(DATA_DIR, "new_envelopes")

# 生命周期状态
STATUS_CURRENT = "current"        # 当前版本（每个记录编号最多一条）
STATUS_ARCHIVED = "archived"      # 历史版本，保留可追溯
STATUS_PENDING = "pending_confirm"  # 冲突待人工确认，读取服务呈现「待确认」

# 冲突处理策略
POLICY_ARCHIVE = "archive"   # 以新版信封为当前结论，旧数组归档
POLICY_MANUAL = "manual"     # 两条都保留进冲突清单，等待人工确认
DEFAULT_POLICY = POLICY_MANUAL

# 来源类型
SOURCE_LEGACY = "legacy_array"
SOURCE_ENVELOPE = "new_envelope"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _content_hash(payload: dict[str, Any]) -> str:
    """对记录实际业务内容做稳定哈希，用于重复导入判定。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditMigrationService:
    """质量审计记录迁移、版本管理与统一读取。"""

    # ---------- 来源扫描与解析 ----------

    def _list_json_files(self, path: str) -> list[str]:
        if not os.path.isdir(path):
            return []
        return sorted(
            os.path.join(path, name)
            for name in os.listdir(path)
            if name.endswith(".json")
        )

    def legacy_sources(self) -> list[str]:
        """全部旧版 JSON 数组文件（主文件 + legacy_audit 目录）。"""
        files: list[str] = []
        if os.path.exists(LEGACY_MAIN_FILE):
            files.append(LEGACY_MAIN_FILE)
        files.extend(self._list_json_files(LEGACY_DIR))
        return files

    def envelope_sources(self) -> list[str]:
        """全部新版记录信封文件。"""
        return self._list_json_files(ENVELOPE_DIR)

    def _read_json_array(self, path: str) -> list[dict[str, Any]]:
        """读取一个 JSON 数组文件；文件缺失或结构损坏时抛错，由整组事务回滚。"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"{path} 不是 JSON 数组，无法按旧版质量审计记录解析")
        return [row for row in data if isinstance(row, dict)]

    def _read_envelope_file(self, path: str) -> list[dict[str, Any]]:
        """读取新版信封文件，兼容单个信封对象或信封数组。"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError(f"{path} 信封结构无法识别")
        return [row for row in data if isinstance(row, dict)]

    # ---------- 旧数组 -> 标准字段映射 ----------

    def _qc_entry(self, audit_entry_id: Any) -> dict[str, Any] | None:
        try:
            return store.find(QC_MODULE, int(audit_entry_id))
        except (TypeError, ValueError):
            return None

    def normalize_legacy(self, row: dict[str, Any], source_file: str) -> dict[str, Any]:
        """把旧数组行映射为标准字段：记录编号、关联批次、检查项、判定结果等。

        旧数组只有 id/batch_id/audit_entry_id/verdict/issues/audited_at，
        业务中文名通过 audit_entry_id 关联 quality_check 模块补齐；关联不到时
        用可追溯的兜底值，保证旧字段不丢失。
        """
        qc = self._qc_entry(row.get("audit_entry_id"))
        if qc is not None:
            record_no = str(qc.get("记录编号") or f"LEGACY-{row.get('id')}")
            batch_no = str(qc.get("关联批次") or "")
            check_item = str(qc.get("检查项") or "")
            threshold = str(qc.get("阈值") or "")
            actual = str(qc.get("实际值") or "")
            audited_at = str(row.get("audited_at") or qc.get("审计时间") or "")
            advice = str(qc.get("处理建议") or "")
        else:
            record_no = str(row.get("record_no") or f"LEGACY-{row.get('id')}")
            batch_no = str(row.get("batch_no") or f"batch-{row.get('batch_id', '')}")
            check_item = str(row.get("check_item") or "未命名检查项")
            threshold = str(row.get("threshold") or "")
            actual = str(row.get("actual") or "")
            audited_at = str(row.get("audited_at") or "")
            advice = "; ".join(str(i) for i in row.get("issues", []) if i)

        verdict = str(row.get("verdict") or (qc or {}).get("判定结果") or "")
        batch_id = row.get("batch_id", (qc or {}).get("id"))
        payload = {
            "记录编号": record_no,
            "关联批次": batch_no,
            "检查项": check_item,
            "阈值": threshold,
            "实际值": actual,
            "判定结果": verdict,
            "审计时间": audited_at,
            "处理建议": advice,
        }
        return {
            "record_no": record_no,
            "batch_id": batch_id,
            "batch_no": batch_no,
            "check_item": check_item,
            "verdict": verdict,
            "payload": payload,
            "source_type": SOURCE_LEGACY,
            "source_file": os.path.relpath(path=source_file, start=DATA_DIR),
            "source_record_id": row.get("id"),
            # 旧字段与审计结论原样保留，满足可追溯要求
            "legacy_snapshot": row,
            "audited_at": audited_at,
        }

    def normalize_envelope(self, row: dict[str, Any], source_file: str) -> dict[str, Any]:
        """新版信封标准化：信封本身就是目标结构，这里只做字段兜底与来源标注。"""
        payload = dict(row.get("payload") or {})
        record_no = str(
            row.get("record_no")
            or payload.get("记录编号")
            or f"ENV-{row.get('id', '')}"
        )
        # 信封内若只给了中文 payload，补齐标准键，保证数据集字段稳定
        payload.setdefault("记录编号", record_no)
        payload.setdefault("关联批次", row.get("batch_no", ""))
        payload.setdefault("检查项", row.get("check_item", ""))
        payload.setdefault("判定结果", row.get("verdict", ""))
        return {
            "record_no": record_no,
            "batch_id": row.get("batch_id"),
            "batch_no": str(row.get("batch_no") or payload.get("关联批次") or ""),
            "check_item": str(row.get("check_item") or payload.get("检查项") or ""),
            "verdict": str(row.get("verdict") or payload.get("判定结果") or ""),
            "payload": payload,
            "source_type": SOURCE_ENVELOPE,
            "source_file": os.path.relpath(path=source_file, start=DATA_DIR),
            "source_record_id": row.get("id"),
            "source_version": row.get("version"),
            "legacy_snapshot": None,
            "audited_at": str(row.get("audited_at") or payload.get("审计时间") or ""),
        }

    # ---------- 数据集内部查询 ----------

    def _envelopes(self) -> list[dict[str, Any]]:
        return store.joint_rows(ENVELOPE_TABLE)

    def _find_by_source(self, source_type: str, source_file: str,
                        source_record_id: Any) -> dict[str, Any] | None:
        """重复导入判定：同来源文件 + 同来源记录 id 视为同一条导入。"""
        for env in self._envelopes():
            src = env.get("source") or {}
            if (
                src.get("type") == source_type
                and src.get("file") == source_file
                and str(src.get("record_id")) == str(source_record_id)
            ):
                return env
        return None

    def _versions_of(self, record_no: str) -> list[dict[str, Any]]:
        return [e for e in self._envelopes() if e.get("record_no") == record_no]

    def _current_of(self, record_no: str) -> dict[str, Any] | None:
        for env in self._versions_of(record_no):
            if env.get("lifecycle") == STATUS_CURRENT:
                return env
        return None

    def _next_version(self, record_no: str) -> int:
        versions = [int(e.get("version", 0)) for e in self._versions_of(record_no)]
        return (max(versions) if versions else 0) + 1

    def _build_envelope(self, normalized: dict[str, Any], version: int,
                        lifecycle: str, migrated_at: str,
                        conflict: dict[str, Any] | None = None) -> dict[str, Any]:
        source = {
            "type": normalized["source_type"],
            "file": normalized["source_file"],
            "record_id": normalized["source_record_id"],
        }
        if normalized.get("source_version") is not None:
            # 显式标注来源信封自带版本
            source["source_version"] = normalized["source_version"]
        return {
            "id": None,  # 由 _persist_group 统一分配
            "record_no": normalized["record_no"],
            "version": version,
            "is_current": lifecycle == STATUS_CURRENT,
            "lifecycle": lifecycle,
            "batch_id": normalized["batch_id"],
            "batch_no": normalized["batch_no"],
            "check_item": normalized["check_item"],
            "verdict": normalized["verdict"],
            "payload": normalized["payload"],
            "source": source,
            "source_version": normalized.get("source_version"),
            "legacy_snapshot": normalized["legacy_snapshot"],
            "content_hash": _content_hash(normalized["payload"]),
            "conflict": conflict,
            "migrated_at": migrated_at,
            "audited_at": normalized["audited_at"],
        }

    # ---------- 迁移主流程 ----------

    def _collect_inputs(self) -> list[tuple[str, dict[str, Any]]]:
        """扫描全部来源文件，返回 (来源文件路径, 标准化记录) 列表，保持稳定顺序。"""
        inputs: list[tuple[str, dict[str, Any]]] = []
        for path in self.legacy_sources():
            for row in self._read_json_array(path):
                inputs.append((path, self.normalize_legacy(row, path)))
        for path in self.envelope_sources():
            for row in self._read_envelope_file(path):
                inputs.append((path, self.normalize_envelope(row, path)))
        return inputs

    def migrate(self, conflict_policy: str = DEFAULT_POLICY,
                trigger: str = "manual") -> dict[str, Any]:
        """执行一次迁移：批量处理全部来源文件，逐条核对，整组一次提交。

        - 幂等：已导入的同来源记录直接跳过；
        - 冲突：同记录编号且实际业务值不一致时按 conflict_policy 处理；
        - 原子性：先在内存副本上完成全部核对，最后一次 joint_replace 落盘，
          任何一条出错都不会产生半成品数据集。
        """
        if conflict_policy not in (POLICY_ARCHIVE, POLICY_MANUAL):
            raise ValueError(f"未知冲突策略：{conflict_policy}")

        inputs = self._collect_inputs()
        # 内存工作副本：整组核对期间不触碰已落盘数据
        working = [dict(env) for env in self._envelopes()]
        now = _now()

        scanned = len(inputs)
        skipped = 0
        migrated = 0
        conflicts: list[dict[str, Any]] = []
        per_file: dict[str, dict[str, int]] = {}

        def bump(file: str, key: str) -> None:
            per_file.setdefault(file, {"scanned": 0, "migrated": 0, "skipped": 0, "conflict": 0})
            per_file[file][key] += 1

        for path, normalized in inputs:
            rel = normalized["source_file"]
            bump(rel, "scanned")

            # 1) 重复导入：同来源同记录已在数据集中 -> 跳过，不产生新版本
            existing = self._find_in(working, normalized["source_type"],
                                     normalized["source_file"],
                                     normalized["source_record_id"])
            if existing is not None:
                skipped += 1
                bump(rel, "skipped")
                continue

            incoming_hash = _content_hash(normalized["payload"])
            current = self._current_in(working, normalized["record_no"])

            # 2) 同记录编号、同实际值：内容哈希一致，视为重复内容，不新增版本
            if current is not None and current.get("content_hash") == incoming_hash:
                skipped += 1
                bump(rel, "skipped")
                continue

            version = self._next_version_in(working, normalized["record_no"])

            # 3) 同记录编号但实际值不一致 -> 冲突，禁止静默覆盖
            if current is not None:
                conflict_info = {
                    "record_no": normalized["record_no"],
                    "current_version": current.get("version"),
                    "incoming_version": version,
                    "current_source": current.get("source"),
                    "incoming_source": {
                        "type": normalized["source_type"],
                        "file": normalized["source_file"],
                        "record_id": normalized["source_record_id"],
                    },
                    "current_verdict": current.get("verdict"),
                    "incoming_verdict": normalized["verdict"],
                    "policy": conflict_policy,
                    "status": "open",
                    "detected_at": now,
                }
                conflicts.append(conflict_info)
                bump(rel, "conflict")

                if conflict_policy == POLICY_ARCHIVE:
                    # 新版信封作为当前结论；旧当前版本归档，历史完整保留
                    current["is_current"] = False
                    current["lifecycle"] = STATUS_ARCHIVED
                    envelope = self._build_envelope(normalized, version,
                                                    STATUS_CURRENT, now)
                    working.append(envelope)
                else:
                    # 人工确认：两条都保留，新条目进入待确认状态，不抢占当前版本
                    envelope = self._build_envelope(normalized, version,
                                                    STATUS_PENDING, now,
                                                    conflict=dict(conflict_info))
                    working.append(envelope)
            else:
                # 4) 新记录编号：直接成为当前版本
                envelope = self._build_envelope(normalized, version,
                                                STATUS_CURRENT, now)
                working.append(envelope)

            migrated += 1
            bump(rel, "migrated")

        # 整组一次提交：唯一落盘点
        next_id = max((int(e.get("id") or 0) for e in working), default=0) + 1
        for env in working:
            if env.get("id") is None:
                env["id"] = next_id
                next_id += 1
        store.joint_replace(ENVELOPE_TABLE, working)

        batch_log = {
            "id": max((int(r.get("id", 0)) for r in store.joint_rows(MIGRATION_TABLE)),
                      default=0) + 1,
            "trigger": trigger,
            "conflict_policy": conflict_policy,
            "scanned": scanned,
            "migrated": migrated,
            "skipped": skipped,
            "conflicts": len(conflicts),
            "per_file": per_file,
            "sources": sorted(per_file.keys()),
            "finished_at": now,
        }
        store.joint_append(MIGRATION_TABLE, batch_log)
        return {
            "ok": True,
            "batch": batch_log,
            "conflicts": conflicts,
            "dataset_size": len(working),
        }

    # 工作副本版查询（迁移过程中不能读到已落盘表之外的状态）
    def _find_in(self, working: list[dict[str, Any]], source_type: str,
                 source_file: str, source_record_id: Any) -> dict[str, Any] | None:
        for env in working:
            src = env.get("source") or {}
            if (
                src.get("type") == source_type
                and src.get("file") == source_file
                and str(src.get("record_id")) == str(source_record_id)
            ):
                return env
        return None

    def _current_in(self, working: list[dict[str, Any]],
                    record_no: str) -> dict[str, Any] | None:
        for env in working:
            if env.get("record_no") == record_no and env.get("lifecycle") == STATUS_CURRENT:
                return env
        return None

    def _next_version_in(self, working: list[dict[str, Any]], record_no: str) -> int:
        versions = [int(e.get("version", 0)) for e in working
                    if e.get("record_no") == record_no]
        return (max(versions) if versions else 0) + 1

    # ---------- 冲突人工确认 ----------

    def list_conflicts(self) -> list[dict[str, Any]]:
        """待人工确认清单：所有 pending_confirm 信封及其冲突描述。"""
        return [
            {
                "envelope_id": env.get("id"),
                "record_no": env.get("record_no"),
                "version": env.get("version"),
                "verdict": env.get("verdict"),
                "source": env.get("source"),
                "conflict": env.get("conflict"),
                "migrated_at": env.get("migrated_at"),
            }
            for env in self._envelopes()
            if env.get("lifecycle") == STATUS_PENDING
        ]

    def resolve_conflict(self, envelope_id: int, resolution: str) -> dict[str, Any]:
        """人工确认冲突。

        - resolution=use_incoming：待确认信封转正，原当前版本归档；
        - resolution=keep_current：待确认信封归档，原当前版本保持不变。
        无论哪种选择历史都保留，且每个记录编号仍只有一个当前版本。
        """
        target = next((e for e in self._envelopes() if int(e.get("id") or 0) == envelope_id),
                      None)
        if target is None:
            return {"ok": False, "error": f"信封 {envelope_id} 不存在"}
        if target.get("lifecycle") != STATUS_PENDING:
            return {"ok": False, "error": f"信封 {envelope_id} 不在待确认状态"}
        if resolution not in ("use_incoming", "keep_current"):
            return {"ok": False, "error": "resolution 只能是 use_incoming 或 keep_current"}

        record_no = target["record_no"]
        current = self._current_of(record_no)
        if resolution == "use_incoming":
            if current is not None:
                current["is_current"] = False
                current["lifecycle"] = STATUS_ARCHIVED
            target["is_current"] = True
            target["lifecycle"] = STATUS_CURRENT
        else:
            target["is_current"] = False
            target["lifecycle"] = STATUS_ARCHIVED

        conflict = target.get("conflict") or {}
        conflict.update({"status": "resolved", "resolution": resolution,
                         "resolved_at": _now()})
        target["conflict"] = conflict
        store.joint_save(ENVELOPE_TABLE)
        return {"ok": True, "envelope": target}

    # ---------- 统一读取服务 ----------

    def _unmigrated_legacy_rows(self) -> list[dict[str, Any]]:
        """尚未进入新版数据集的旧数组记录（读取服务需要同时看到它们）。"""
        migrated_keys = {
            ((e.get("source") or {}).get("file"),
             str((e.get("source") or {}).get("record_id")))
            for e in self._envelopes()
            if (e.get("source") or {}).get("type") == SOURCE_LEGACY
        }
        rows: list[dict[str, Any]] = []
        for path in self.legacy_sources():
            rel = os.path.relpath(path=path, start=DATA_DIR)
            for row in self._read_json_array(path):
                if (rel, str(row.get("id"))) not in migrated_keys:
                    normalized = self.normalize_legacy(row, path)
                    rows.append({
                        "record_no": normalized["record_no"],
                        "batch_id": normalized["batch_id"],
                        "batch_no": normalized["batch_no"],
                        "check_item": normalized["check_item"],
                        "verdict": normalized["verdict"],
                        "payload": normalized["payload"],
                        "source": {"type": SOURCE_LEGACY, "file": rel,
                                   "record_id": row.get("id")},
                        "migrated": False,
                        "lifecycle": "unmigrated",
                        "version": None,
                        "is_current": None,
                        "audited_at": normalized["audited_at"],
                    })
        return rows

    def read_all(self) -> dict[str, Any]:
        """质量审计读取服务：同时返回新版数据集与未迁移旧记录。"""
        envelopes = [dict(env) for env in self._envelopes()]
        unmigrated = self._unmigrated_legacy_rows()
        current_nos = {env["record_no"] for env in envelopes
                       if env.get("lifecycle") == STATUS_CURRENT}
        return {
            "migrated": envelopes,
            "unmigrated": unmigrated,
            "current_record_nos": sorted(current_nos),
            "summary": {
                "migrated_total": len(envelopes),
                "current_total": len(current_nos),
                "pending_conflicts": len(self.list_conflicts()),
                "archived_total": sum(1 for e in envelopes
                                      if e.get("lifecycle") == STATUS_ARCHIVED),
                "unmigrated_total": len(unmigrated),
            },
        }

    def read_detail(self, record_no: str) -> dict[str, Any] | None:
        """迁移完成后返回质量审计详情：当前版本 + 全部历史版本 + 未迁移痕迹。"""
        versions = sorted(self._versions_of(record_no),
                          key=lambda e: int(e.get("version", 0)))
        if not versions:
            # 仍未迁移的旧记录也要能读到详情
            for row in self._unmigrated_legacy_rows():
                if row["record_no"] == record_no:
                    return {"record_no": record_no, "migrated": False,
                            "current": None, "versions": [row]}
            return None
        current = next((v for v in versions if v.get("lifecycle") == STATUS_CURRENT), None)
        return {
            "record_no": record_no,
            "migrated": True,
            "current": current,
            "versions": versions,
            "has_pending_conflict": any(v.get("lifecycle") == STATUS_PENDING
                                        for v in versions),
        }

    # ---------- 导出（落库后才能导出） ----------

    def export_rows(self) -> list[dict[str, Any]]:
        """导出数据集当前快照；未落库任何信封时拒绝导出。"""
        envelopes = self._envelopes()
        if not envelopes:
            raise RuntimeError("质量审计记录尚未迁移落库，无法导出；请先执行存量迁移")
        return [dict(env) for env in envelopes]

    def migration_status(self) -> dict[str, Any]:
        """迁移来源同步状态：供质量审计模块与跨模块入口共用。"""
        legacy_files = [os.path.relpath(p, DATA_DIR) for p in self.legacy_sources()]
        envelope_files = [os.path.relpath(p, DATA_DIR) for p in self.envelope_sources()]
        migrated_sources = {
            (e.get("source") or {}).get("file") for e in self._envelopes()
        }
        return {
            "dataset_size": len(self._envelopes()),
            "legacy_sources": legacy_files,
            "envelope_sources": envelope_files,
            "migrated_sources": sorted(s for s in migrated_sources if s),
            "pending_sources": sorted(
                set(legacy_files + envelope_files) - migrated_sources
            ),
            "pending_conflicts": len(self.list_conflicts()),
            "migration_batches": len(store.joint_rows(MIGRATION_TABLE)),
        }

    def append_live_audit(self, *, record_no: str, batch_id: Any, batch_no: str,
                          check_item: str, verdict: str, payload: dict[str, Any],
                          legacy_snapshot: dict[str, Any],
                          source_file: str = "data/audit_records.json",
                          source_record_id: Any = None) -> dict[str, Any]:
        """质量审计引擎新产出的记录直接进入版本化数据集。

        与迁移共用同一套版本/当前版本规则：同记录编号同内容不产生新版本，
        同记录编号新结论自动把旧当前版本归档（引擎结论即最新结论）。
        """
        working = [dict(env) for env in self._envelopes()]
        now = _now()
        incoming_hash = _content_hash(payload)
        current = self._current_in(working, record_no)

        if current is not None and current.get("content_hash") == incoming_hash:
            return {"ok": True, "envelope": current, "deduplicated": True}

        version = self._next_version_in(working, record_no)
        if current is not None:
            current["is_current"] = False
            current["lifecycle"] = STATUS_ARCHIVED

        normalized = {
            "record_no": record_no,
            "batch_id": batch_id,
            "batch_no": batch_no,
            "check_item": check_item,
            "verdict": verdict,
            "payload": payload,
            "source_type": SOURCE_LEGACY,
            "source_file": os.path.relpath(path=source_file, start=DATA_DIR),
            "source_record_id": source_record_id,
            "source_version": None,
            "legacy_snapshot": legacy_snapshot,
            "audited_at": str(payload.get("审计时间") or now),
        }
        envelope = self._build_envelope(normalized, version, STATUS_CURRENT, now)
        working.append(envelope)

        next_id = max((int(e.get("id") or 0) for e in working), default=0) + 1
        for env in working:
            if env.get("id") is None:
                env["id"] = next_id
                next_id += 1
        store.joint_replace(ENVELOPE_TABLE, working)
        return {"ok": True, "envelope": envelope, "deduplicated": False}

    def bootstrap_auto_migrate(self) -> dict[str, Any] | None:
        """服务启动时自动迁移主旧数组文件来源，保证读取服务开箱即有落库数据。

        幂等：已有迁移批次时跳过。
        """
        if store.joint_rows(MIGRATION_TABLE):
            return None
        if not self.legacy_sources() and not self.envelope_sources():
            return None
        return self.migrate(conflict_policy=DEFAULT_POLICY, trigger="startup")


audit_migration_service = AuditMigrationService()
