"""质量审计记录版本化迁移的单元测试（标准库 unittest，无额外依赖）。

运行：cd backend && .venv/bin/python -m unittest tests.test_audit_migration -v
测试数据隔离：把 DATA_DIR 指到临时目录，不污染 backend/data。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import app.services.audit_migration as am
from app.services.audit_migration import (
    POLICY_ARCHIVE,
    POLICY_MANUAL,
    STATUS_ARCHIVED,
    STATUS_CURRENT,
    STATUS_PENDING,
    AuditMigrationService,
)


def _write(path: str, data: object) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


class MigrationTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="audit-mig-")
        # 重定向服务里的全部路径常量到临时目录
        am.DATA_DIR = self.tmp
        am.LEGACY_MAIN_FILE = os.path.join(self.tmp, "audit_records.json")
        am.LEGACY_DIR = os.path.join(self.tmp, "legacy_audit")
        am.ENVELOPE_DIR = os.path.join(self.tmp, "new_envelopes")

        self.legacy_main = [
            {"id": 1, "batch_id": 1, "audit_entry_id": 4, "verdict": "不通过",
             "issues": ["校验摘要过短"], "audited_at": "2026-11-16 10:57:45"},
            {"id": 2, "batch_id": 2, "audit_entry_id": 5, "verdict": "不通过",
             "issues": ["校验摘要过短"], "audited_at": "2026-11-16 10:57:45"},
        ]
        self.legacy_extra = [
            {"id": 101, "batch_id": 7, "audit_entry_id": 101, "verdict": "通过",
             "issues": [], "audited_at": "2026-10-08 09:12:30",
             "record_no": "QUAL-0101", "batch_no": "RESU-0101",
             "check_item": "字段完整率", "threshold": "完整率>=95%",
             "actual": "完整率97.2%"},
            # 与新版信封同记录编号、不同实际值 -> 冲突
            {"id": 102, "batch_id": 8, "audit_entry_id": 102, "verdict": "不通过",
             "issues": ["空值率超阈值"], "audited_at": "2026-10-09 14:05:11",
             "record_no": "QUAL-0102", "batch_no": "RESU-0102",
             "check_item": "空值率", "threshold": "空值率<=2%",
             "actual": "空值率5.8%"},
        ]
        self.new_envelopes = [
            {"id": 9001, "record_no": "QUAL-0102", "batch_id": 8,
             "batch_no": "RESU-0102", "check_item": "空值率", "verdict": "通过",
             "version": 3, "audited_at": "2026-11-10 16:40:00",
             "payload": {"记录编号": "QUAL-0102", "关联批次": "RESU-0102",
                         "检查项": "空值率", "阈值": "空值率<=2%",
                         "实际值": "空值率1.4%（修复后复检）", "判定结果": "通过",
                         "审计时间": "2026-11-10 16:40:00", "处理建议": "复检通过"}},
            {"id": 9002, "record_no": "QUAL-0201", "batch_id": 9,
             "batch_no": "RESU-0201", "check_item": "主键唯一性", "verdict": "通过",
             "audited_at": "2026-11-11 10:02:09",
             "payload": {"记录编号": "QUAL-0201", "关联批次": "RESU-0201",
                         "检查项": "主键唯一性", "阈值": "重复率=0",
                         "实际值": "重复率0", "判定结果": "通过",
                         "审计时间": "2026-11-11 10:02:09", "处理建议": "无需处理"}},
        ]
        _write(am.LEGACY_MAIN_FILE, self.legacy_main)
        _write(os.path.join(am.LEGACY_DIR, "audit_records_2026q4.json"),
               self.legacy_extra)
        _write(os.path.join(am.ENVELOPE_DIR, "audit_envelopes_202611.json"),
               self.new_envelopes)

        # 清空全局 store 中的迁移表，保证测试间隔离；落盘目录也重定向到临时目录
        from app.store import store
        store.set_data_dir(self.tmp)
        self.svc = AuditMigrationService()
        self.store = store

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestMigration(MigrationTestBase):
    def test_batch_migrate_maps_fields_and_versions(self):
        """批量多文件迁移：字段映射正确、版本号从 1 递增、整组一次提交落盘。"""
        result = self.svc.migrate(POLICY_MANUAL)
        self.assertTrue(result["ok"])
        # 扫描 2(主文件) + 2(旧目录) + 2(新信封) = 6 条
        self.assertEqual(result["batch"]["scanned"], 6)
        envelopes = self.store.joint_rows(am.ENVELOPE_TABLE)
        # 落盘文件确实生成（整组一次提交的唯一写入点）
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "audit_envelopes.json")))
        with open(os.path.join(self.tmp, "audit_envelopes.json"), encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(len(persisted), len(envelopes))
        # 每条信封都带版本号、记录编号、关联批次、检查项、判定结果
        for env in envelopes:
            self.assertIn("version", env)
            self.assertTrue(env["record_no"])
            self.assertIn("batch_no", env)
            self.assertIn("check_item", env)
            self.assertIn("verdict", env)
            self.assertIn("payload", env)
            self.assertIn("source", env)

    def test_legacy_fields_and_conclusion_preserved(self):
        """旧字段和审计结论必须原样保留可追溯。"""
        self.svc.migrate(POLICY_MANUAL)
        legacy_envs = [e for e in self.store.joint_rows(am.ENVELOPE_TABLE)
                       if e["source"]["type"] == am.SOURCE_LEGACY]
        self.assertTrue(legacy_envs)
        sample = next(e for e in legacy_envs if e["source"]["file"].endswith("audit_records.json"))
        self.assertEqual(sample["legacy_snapshot"]["verdict"], "不通过")
        self.assertEqual(sample["legacy_snapshot"]["issues"], ["校验摘要过短"])
        self.assertEqual(sample["verdict"], "不通过")
        # 数据集保存来源版本
        self.assertEqual(sample["source"]["type"], am.SOURCE_LEGACY)

    def test_idempotent_reimport_no_duplicate_versions(self):
        """重复导入不产生重复审计版本。"""
        first = self.svc.migrate(POLICY_MANUAL)
        size_after_first = len(self.store.joint_rows(am.ENVELOPE_TABLE))
        second = self.svc.migrate(POLICY_MANUAL)
        size_after_second = len(self.store.joint_rows(am.ENVELOPE_TABLE))
        self.assertEqual(size_after_first, size_after_second)
        self.assertEqual(second["batch"]["migrated"], 0)
        self.assertEqual(second["batch"]["skipped"], first["batch"]["scanned"])
        # 迁移批次日志仍然记录（整组提交凭证）
        self.assertEqual(len(self.store.joint_rows(am.MIGRATION_TABLE)), 2)

    def test_one_current_version_per_record(self):
        """同一个审计记录只能有一个当前版本。"""
        self.svc.migrate(POLICY_ARCHIVE)
        by_no: dict[str, list] = {}
        for env in self.store.joint_rows(am.ENVELOPE_TABLE):
            by_no.setdefault(env["record_no"], []).append(env)
        for record_no, versions in by_no.items():
            currents = [v for v in versions if v["lifecycle"] == STATUS_CURRENT]
            self.assertEqual(len(currents), 1, f"{record_no} 当前版本不唯一")

    def test_conflict_manual_goes_to_pending_list(self):
        """manual 策略：冲突两条都保留，新条目待确认，不静默覆盖。"""
        result = self.svc.migrate(POLICY_MANUAL)
        self.assertEqual(result["batch"]["conflicts"], 1)
        conflicts = self.svc.list_conflicts()
        self.assertEqual(len(conflicts), 1)
        pending = conflicts[0]
        self.assertEqual(pending["record_no"], "QUAL-0102")
        # 旧数组仍是当前结论，新信封待确认
        detail = self.svc.read_detail("QUAL-0102")
        self.assertTrue(detail["has_pending_conflict"])
        self.assertEqual(detail["current"]["verdict"], "不通过")
        pending_env = next(v for v in detail["versions"]
                           if v["lifecycle"] == STATUS_PENDING)
        self.assertEqual(pending_env["verdict"], "通过")
        # 读取服务呈现待确认状态
        view = self.svc.read_all()
        self.assertEqual(view["summary"]["pending_conflicts"], 1)

    def test_conflict_manual_resolve_use_incoming(self):
        """人工确认 use_incoming：待确认转正，旧当前归档，历史保留。"""
        self.svc.migrate(POLICY_MANUAL)
        pending = self.svc.list_conflicts()[0]
        res = self.svc.resolve_conflict(pending["envelope_id"], "use_incoming")
        self.assertTrue(res["ok"])
        detail = self.svc.read_detail("QUAL-0102")
        self.assertEqual(detail["current"]["verdict"], "通过")
        statuses = [v["lifecycle"] for v in detail["versions"]]
        self.assertIn(STATUS_ARCHIVED, statuses)
        self.assertEqual(len([s for s in statuses if s == STATUS_CURRENT]), 1)
        self.assertEqual(self.svc.list_conflicts(), [])

    def test_conflict_manual_resolve_keep_current(self):
        """人工确认 keep_current：待确认归档，原当前结论保持。"""
        self.svc.migrate(POLICY_MANUAL)
        pending = self.svc.list_conflicts()[0]
        self.svc.resolve_conflict(pending["envelope_id"], "keep_current")
        detail = self.svc.read_detail("QUAL-0102")
        self.assertEqual(detail["current"]["verdict"], "不通过")
        self.assertTrue(all(v["lifecycle"] != STATUS_PENDING for v in detail["versions"]))

    def test_conflict_archive_policy(self):
        """archive 策略：新版信封为当前结论，旧数组归档，不静默覆盖（历史还在）。"""
        self.svc.migrate(POLICY_ARCHIVE)
        detail = self.svc.read_detail("QUAL-0102")
        self.assertEqual(detail["current"]["verdict"], "通过")
        self.assertEqual(detail["current"]["source"]["type"], am.SOURCE_ENVELOPE)
        archived = [v for v in detail["versions"] if v["lifecycle"] == STATUS_ARCHIVED]
        self.assertTrue(any(v["verdict"] == "不通过" for v in archived))
        self.assertEqual(self.svc.list_conflicts(), [])

    def test_read_service_sees_unmigrated_and_migrated(self):
        """读取服务同时看到未迁移旧记录与新版数据集。"""
        # 只迁移主文件之外的来源前，先迁移一次全部
        self.svc.migrate(POLICY_MANUAL)
        view = self.svc.read_all()
        self.assertGreaterEqual(view["summary"]["migrated_total"], 6)
        self.assertEqual(view["summary"]["unmigrated_total"], 0)

        # 新增一个从未迁移的旧文件，读取服务应能看到未迁移记录
        extra = [{"id": 201, "batch_id": 10, "audit_entry_id": 201,
                  "verdict": "通过", "issues": [], "audited_at": "2026-11-15 08:00:00",
                  "record_no": "QUAL-0301", "batch_no": "RESU-0301",
                  "check_item": "行数一致性", "threshold": "偏差<=1%",
                  "actual": "偏差0.2%"}]
        _write(os.path.join(am.LEGACY_DIR, "audit_records_202611b.json"), extra)
        view2 = self.svc.read_all()
        self.assertEqual(view2["summary"]["unmigrated_total"], 1)
        self.assertEqual(view2["unmigrated"][0]["record_no"], "QUAL-0301")
        self.assertFalse(view2["unmigrated"][0]["migrated"])
        # 详情接口对未迁移记录同样可用
        detail = self.svc.read_detail("QUAL-0301")
        self.assertFalse(detail["migrated"])

    def test_export_blocked_until_persisted(self):
        """记录落库后才能导出：空数据集拒绝导出。"""
        with self.assertRaises(RuntimeError):
            self.svc.export_rows()
        self.svc.migrate(POLICY_MANUAL)
        rows = self.svc.export_rows()
        self.assertTrue(rows)

    def test_rollback_on_bad_source_file(self):
        """来源文件损坏时整组回滚，不产生半成品。"""
        bad_path = os.path.join(am.LEGACY_DIR, "broken.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{not-a-json-array")
        with self.assertRaises(Exception):
            self.svc.migrate(POLICY_MANUAL)
        self.assertEqual(self.store.joint_rows(am.ENVELOPE_TABLE), [])
        self.assertEqual(self.store.joint_rows(am.MIGRATION_TABLE), [])

    def test_migration_status_sources(self):
        """迁移来源同步状态覆盖旧数组与新版信封。"""
        status = self.svc.migration_status()
        self.assertEqual(len(status["legacy_sources"]), 2)
        self.assertEqual(len(status["envelope_sources"]), 1)
        self.assertEqual(status["pending_sources"].sort(),
                         (status["legacy_sources"] + status["envelope_sources"]).sort())
        self.svc.migrate(POLICY_MANUAL)
        status2 = self.svc.migration_status()
        self.assertEqual(status2["pending_sources"], [])
        self.assertEqual(status2["dataset_size"],
                         len(self.store.joint_rows(am.ENVELOPE_TABLE)))

    def test_append_live_audit_versions_and_archives(self):
        """审计引擎新结论入数据集：新版本递增、旧当前版本归档、内容去重。"""
        self.svc.migrate(POLICY_MANUAL)
        payload = {"记录编号": "QUAL-0101", "关联批次": "RESU-0101",
                   "检查项": "字段完整率", "阈值": "完整率>=95%",
                   "实际值": "完整率88.0%", "判定结果": "不通过",
                   "审计时间": "2026-11-17 09:00:00", "处理建议": "补录字段"}
        res = self.svc.append_live_audit(
            record_no="QUAL-0101", batch_id=7, batch_no="RESU-0101",
            check_item="字段完整率", verdict="不通过", payload=payload,
            legacy_snapshot={"id": 555, "verdict": "不通过"}, source_record_id=555)
        self.assertTrue(res["ok"])
        detail = self.svc.read_detail("QUAL-0101")
        self.assertEqual(detail["current"]["version"], 2)
        self.assertEqual(detail["current"]["verdict"], "不通过")
        # 同内容再次写入 -> 去重，不新增版本
        res2 = self.svc.append_live_audit(
            record_no="QUAL-0101", batch_id=7, batch_no="RESU-0101",
            check_item="字段完整率", verdict="不通过", payload=dict(payload),
            legacy_snapshot={"id": 556}, source_record_id=556)
        self.assertTrue(res2["deduplicated"])
        self.assertEqual(len(self.svc.read_detail("QUAL-0101")["versions"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
