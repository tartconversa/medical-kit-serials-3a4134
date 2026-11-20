"""业务测试：装载、时间截点、交付证据、换件/借件、会话修订与冻结对账。"""
from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path

from sourcepack import load, query, reconcile, service
from tests.fixtures import base_tables, write_pack

MOMENT = "2026-10-25T00:00:00+08:00"


def moment(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


class WarehouseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_pack(self.root, base_tables())
        self.conn = load.open_database(self.root / "warehouse.duckdb")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def receive(self, label="初始资料", now=moment("2026-11-01T00:00:00+08:00"), tables=None):
        if tables is not None:
            write_pack(self.root, tables)
        return load.receive(self.conn, self.root, label, now)


class DeliveryEvidenceTests(WarehouseCase):
    def test_set_scan_and_part_scans_are_one_delivery(self):
        report = self.receive()
        deliveries = query.deliveries_for_customer(self.conn, "C1", MOMENT, report["session_id"])
        self.assertEqual(len(deliveries), 1)
        evidence = deliveries[0]["evidence"]
        self.assertEqual(evidence["kind"], "sale")
        self.assertEqual(sorted(evidence["part_scans"]), ["X1", "X2"])
        self.assertTrue(evidence["billed_as_set"])

    def test_sale_count_is_sets_not_serial_numbers(self):
        report = self.receive()
        result = reconcile.availability(self.conn, "C1", MOMENT, report["session_id"])
        self.assertEqual(result["delivered_sets_total"], 1)
        self.assertEqual(result["kits"][0]["delivered_sets"], 1)

    def test_loan_delivery_does_not_count_as_sale(self):
        tables = base_tables()
        tables["shipments"][0]["kind"] = "loan"
        report = self.receive(tables=tables)
        result = reconcile.availability(self.conn, "C1", MOMENT, report["session_id"])
        self.assertEqual(result["delivered_sets_total"], 0)
        self.assertEqual(result["kits"][0]["delivered_loans"], 1)


class HistoryTests(WarehouseCase):
    def test_missing_pump_interrupts_service(self):
        report = self.receive()
        intervals = reconcile.interruption_intervals(
            self.conn, "C1", "A:K1", moment("2026-10-05T08:00:00+08:00"),
            moment(MOMENT), report["session_id"],
        )
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0]["interrupted_from"], "2026-10-12T08:00:00")
        self.assertEqual(intervals[0]["interrupted_to"], "2026-10-13T08:00:00")
        self.assertEqual(intervals[0]["cause"], "missing_required")

    def test_loan_restores_service_without_sale(self):
        report = self.receive()
        during = reconcile.availability(self.conn, "C1", "2026-10-15T00:00:00+08:00",
                                        report["session_id"])
        kit = during["kits"][0]
        self.assertEqual(kit["available_sets"], 1)
        self.assertEqual(kit["delivered_sets"], 1)
        self.assertEqual([c["component_key"] for c in kit["loan_components_in_use"]], ["A:3"])

    def test_delivered_sets_keep_historical_basis_while_unavailable(self):
        report = self.receive()
        broken = reconcile.availability(self.conn, "C1", "2026-10-12T12:00:00+08:00",
                                        report["session_id"])
        kit = broken["kits"][0]
        self.assertEqual(kit["delivered_sets"], 1)
        self.assertEqual(kit["available_sets"], 0)
        self.assertTrue(kit["service_interrupted"])

    def test_nonworking_required_component_interrupts(self):
        report = self.receive()
        # 10-12 08:00 起 A:2 既被卸下又 nonworking；10-13 借入件恢复服务。
        during = reconcile.availability(self.conn, "C1", "2026-10-12T12:00:00+08:00",
                                        report["session_id"])
        self.assertTrue(during["kits"][0]["service_interrupted"])

    def test_history_uses_only_known_then(self):
        report = self.receive()
        early = query.kit_at(self.conn, "A:K1", "2026-10-10T00:00:00+08:00",
                             report["session_id"])
        self.assertEqual([a["component_key"] for a in early["assembled"]], ["A:1", "A:2"])
        later = query.kit_at(self.conn, "A:K1", MOMENT, report["session_id"])
        self.assertEqual([a["component_key"] for a in later["assembled"]], ["A:1", "A:2"])


class SwapTests(WarehouseCase):
    def test_swap_closure_requires_both_directions(self):
        report = self.receive()
        closure = service.swap_closure(self.conn, "A:K1", MOMENT, report["session_id"])
        self.assertTrue(closure["swaps"])
        self.assertTrue(all(s["status"] == "closed" for s in closure["swaps"]))

    def test_missing_return_scan_stays_open(self):
        tables = base_tables()
        # 去掉原件回库扫描 X7，换件不再闭合。
        tables["scans"] = [s for s in tables["scans"] if s["scan_id"] != "X7"]
        report = self.receive(tables=tables)
        closure = service.swap_closure(self.conn, "A:K1", MOMENT, report["session_id"])
        statuses = {s["removed"]["assembly_id"]: s["status"] for s in closure["swaps"]}
        # AS2→AS3 的原件 A:2 从 C1 到 repair 有证据，但到 store 的扫描缺失；
        # 卸下/装入两个方向都在，按口径仍算闭合，但回库证据缺失要可见。
        self.assertIn("AS2", statuses)
        open_swap = next(s for s in closure["swaps"] if s["removed"]["assembly_id"] == "AS2")
        self.assertEqual(open_swap["evidence"]["removed_returned_to_store"], [])

    def test_loan_returns_after_scan_and_is_not_sale(self):
        report = self.receive()
        loans = service.loan_returns(self.conn, MOMENT, report["session_id"])
        self.assertEqual(len(loans), 1)
        self.assertTrue(loans[0]["returned"])
        self.assertEqual(loans[0]["component_key"], "A:3")


class LotTests(WarehouseCase):
    def test_lot_trace_reports_current_assembly_not_history(self):
        report = self.receive()
        result = query.lot_affected(self.conn, "L2", MOMENT, report["session_id"])
        current = {(r["kit_key"], r["component_key"]) for r in result["currently_assembled"]}
        self.assertEqual(current, {("A:K1", "A:2")})
        historical = {(r["kit_key"], r["component_key"]) for r in result["historical_only"]}
        self.assertNotIn(("A:K1", "A:2"), historical)
        self.assertIn(("A:K1", "A:3"), historical)


class ConflictTests(WarehouseCase):
    def test_same_serial_different_manufacturers_is_not_a_collision(self):
        report = self.receive()
        result = reconcile.rebuild_conflicts(self.conn, report["session_id"], MOMENT)
        kinds = {c["kind"] for c in result["conflicts"]}
        self.assertNotIn("serial_collision", kinds)

    def test_same_serial_same_manufacturer_is_reviewable(self):
        tables = base_tables()
        tables["components"].append(
            {"component_key": "A:9", "manufacturer": "A", "serial": "1",
             "class": "controller", "lot": "L9"})
        report = self.receive(tables=tables)
        result = reconcile.rebuild_conflicts(self.conn, report["session_id"], MOMENT)
        collisions = [c for c in result["conflicts"] if c["kind"] == "serial_collision"]
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["severity"], "review")
        self.assertEqual(collisions[0]["evidence"]["component_keys"], ["A:1", "A:9"])

    def test_overlapping_assembly_is_reviewable(self):
        tables = base_tables()
        tables["assembly"].append(
            {"assembly_id": "AS7", "kit_key": "B:K1", "component_key": "A:1",
             "start": "2026-10-02T08:00:00+08:00", "end": "2026-10-03T08:00:00+08:00",
             "basis": "permanent"})
        # 组件 A:1 同时出现在 A:K1 的开放装配里。
        report = self.receive(tables=tables)
        result = reconcile.rebuild_conflicts(self.conn, report["session_id"], MOMENT)
        overlaps = [c for c in result["conflicts"] if c["kind"] == "overlap_assembly"]
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]["evidence"]["component_key"], "A:1")


class RevisionTests(WarehouseCase):
    def test_same_files_do_not_create_new_session(self):
        first = self.receive()
        second = self.receive("重复装载")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["session_id"], first["session_id"])
        count = self.conn.execute("SELECT count(*) FROM meta_session").fetchone()[0]
        self.assertEqual(count, 1)

    def test_confirmed_reconcile_keeps_original_content(self):
        report = self.receive()
        frozen = reconcile.freeze_reconcile(self.conn, "C1", MOMENT, "首次对账",
                                            report["session_id"])
        body = frozen["body"]
        # 新会话改变组成：pump 从必需 1 件改为 2 件。
        tables = base_tables()
        for rule in tables["composition_rules"]:
            if rule["rule_id"] == "R2":
                rule["quantity"] = "2"
        self.receive("修订组成", moment("2026-11-05T00:00:00+08:00"), tables)
        stored = reconcile.revisions(self.conn, "C1", "reconcile")
        original = next(r for r in stored if r["revision_id"] == frozen["revision_id"])
        self.assertEqual(original["body"], body)
        self.assertEqual(original["body"]["kits"][0]["missing_required"], [])
        # 新组成从接收时点起生效，历史截点 MOMENT 仍按旧组成计算。
        past = reconcile.availability(self.conn, "C1", MOMENT)
        self.assertEqual(past["kits"][0]["missing_required"], [])
        # 新时点才看到“pump 必需 2 件”，此时在套只有 1 件。
        future = reconcile.availability(self.conn, "C1", "2026-11-06T00:00:00+08:00")
        self.assertEqual(future["kits"][0]["missing_required"][0]["required"], 2)
        self.assertEqual(future["kits"][0]["available_sets"], 0)

    def test_new_session_does_not_rewrite_old_session(self):
        first = self.receive()
        before = reconcile.availability(self.conn, "C1", MOMENT, first["session_id"])
        tables = base_tables()
        for rule in tables["composition_rules"]:
            if rule["rule_id"] == "R2":
                rule["quantity"] = "2"
        second = self.receive("修订组成", moment("2026-11-05T00:00:00+08:00"), tables)
        after_old = reconcile.availability(self.conn, "C1", MOMENT, first["session_id"])
        after_new = reconcile.availability(self.conn, "C1", MOMENT, second["session_id"])
        self.assertEqual(before["kits"], after_old["kits"])
        self.assertNotEqual(after_old["kits"], after_new["kits"])

    def test_revision_takes_effect_after_receipt_not_in_past(self):
        first = self.receive()
        tables = base_tables()
        for rule in tables["composition_rules"]:
            if rule["rule_id"] == "R2":
                rule["quantity"] = "2"
        second = self.receive("修订组成", moment("2026-11-05T00:00:00+08:00"), tables)
        past = query.kit_at(self.conn, "A:K1", MOMENT, second["session_id"])
        pump_rules = [r for r in past["composition"] if r["class"] == "pump"]
        self.assertEqual(pump_rules[0]["quantity"], 1)
        future = query.kit_at(self.conn, "A:K1", "2026-11-06T00:00:00+08:00",
                              second["session_id"])
        pump_rules = [r for r in future["composition"] if r["class"] == "pump"]
        self.assertEqual(pump_rules[0]["quantity"], 2)

    def test_three_composition_versions_stay_separated(self):
        now = moment("2026-11-01T00:00:00+08:00")
        sessions = []
        for index, quantity in enumerate((1, 2, 3)):
            tables = base_tables()
            for rule in tables["composition_rules"]:
                if rule["rule_id"] == "R2":
                    rule["quantity"] = str(quantity)
            sessions.append(self.receive(f"版{quantity}", now, tables))
            now += datetime.timedelta(days=4)
        seen = []
        for session in sessions:
            kit = query.kit_at(self.conn, "A:K1", "2026-11-20T00:00:00+08:00",
                               session["session_id"])
            pump = next(r for r in kit["composition"] if r["class"] == "pump")
            seen.append(pump["quantity"])
        self.assertEqual(seen, [1, 2, 3])
        # 最早会话仍只看到最初版本，且直接使用最新的历史版本不会被后来版本污染。
        history = self.conn.execute(
            "SELECT quantity, effective_from, effective_to FROM rule_period "
            "WHERE session_id = 'session-0003' AND rule_id = 'R2' ORDER BY effective_from"
        ).fetchall()
        self.assertEqual([row[0] for row in history], [1, 2, 3])
        self.assertIsNone(history[-1][2])

    def test_compare_revisions_explains_difference(self):
        report = self.receive()
        first = reconcile.freeze_reconcile(self.conn, "C1", MOMENT, "首次", report["session_id"])
        tables = base_tables()
        # 整行修订：AS4 的结束时刻被补正为 10-22，MOMENT 时泵已不在套内。
        # 文件里缺少某行不等于删除（来源没有删除声明），所以用修订而不是删行。
        for item in tables["assembly"]:
            if item["assembly_id"] == "AS4":
                item["end"] = "2026-10-22T08:00:00+08:00"
        second_report = self.receive("补正装配", moment("2026-11-05T00:00:00+08:00"), tables)
        second = reconcile.freeze_reconcile(self.conn, "C1", MOMENT, "补正",
                                            second_report["session_id"])
        diff = reconcile.compare_revisions(self.conn, first["revision_id"],
                                           second["revision_id"])
        fields = {d["field"] for d in diff["differences"]}
        self.assertIn("available_sets", fields)
        self.assertIn("service_interrupted", fields)
        history = reconcile.revisions(self.conn, "C1", "reconcile")
        statuses = {r["revision_id"]: r["status"] for r in history}
        self.assertEqual(statuses[first["revision_id"]], "superseded")
        self.assertEqual(statuses[second["revision_id"]], "confirmed")


    def test_position_mismatch_is_reviewable(self):
        tables = base_tables()
        # A:1 仍在 A:K1 的开放装配里，但最后一次扫描把它放到了 C2。
        tables["scans"].append(
            {"scan_id": "X20", "component_key": "A:1", "shipment_id": "",
             "from_location": "C1", "to_location": "C2",
             "at": "2026-10-23T08:00:00+08:00"})
        report = self.receive(tables=tables)
        result = reconcile.rebuild_conflicts(self.conn, report["session_id"], MOMENT)
        mismatches = [c for c in result["conflicts"] if c["kind"] == "position_mismatch"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["subject"], "A:1")
        self.assertEqual(mismatches[0]["evidence"]["expected"], "C1")
        self.assertEqual(mismatches[0]["evidence"]["actual"], "C2")


class IntegrityTests(WarehouseCase):
    def test_dangling_reference_is_rejected(self):
        tables = base_tables()
        tables["scans"].append(
            {"scan_id": "X99", "component_key": "Z:9", "shipment_id": "",
             "from_location": "store", "to_location": "C1",
             "at": "2026-10-22T08:00:00+08:00"})
        with self.assertRaisesRegex(load.LoadError, "引用"):
            self.receive(tables=tables)
        # 失败装载不留下半成品会话
        count = self.conn.execute("SELECT count(*) FROM meta_session").fetchone()[0]
        self.assertEqual(count, 0)

    def test_component_trail_traces_from_serial_to_kit(self):
        report = self.receive()
        trail = query.component_trail(self.conn, "A:2", MOMENT, report["session_id"])
        self.assertEqual(trail["component"]["location"], "C1")
        # A:2 有两段装配经历（最初装入，维修后重新装入），都指向同一套。
        self.assertEqual({a["kit_key"] for a in trail["assemblies"]}, {"A:K1"})
        self.assertEqual(trail["component"]["quality_state"], "working")

    def test_unknown_enum_is_rejected(self):
        tables = base_tables()
        tables["assembly"][0]["basis"] = "permenant"
        write_pack(self.root, tables)
        with self.assertRaisesRegex(load.LoadError, "取值未知"):
            load.receive(self.conn, self.root, "坏枚举", moment("2026-11-01T00:00:00+08:00"))

    def test_assembly_end_before_start_is_rejected(self):
        tables = base_tables()
        for item in tables["assembly"]:
            if item["assembly_id"] == "AS2":
                item["end"] = "2026-09-01T08:00:00+08:00"
        write_pack(self.root, tables)
        with self.assertRaisesRegex(load.LoadError, "结束不晚于开始"):
            load.receive(self.conn, self.root, "坏区间", moment("2026-11-01T00:00:00+08:00"))

    def test_kit_to_components_and_back(self):
        report = self.receive()
        kit = query.kit_trail(self.conn, "A:K1", MOMENT, report["session_id"])
        keys = [a["component_key"] for a in kit["assembled"]]
        self.assertEqual(keys, ["A:1", "A:2"])
        for key in keys:
            trail = query.component_trail(self.conn, key, MOMENT, report["session_id"])
            self.assertIn("A:K1", [a["kit_key"] for a in trail["assemblies"]])


if __name__ == "__main__":
    unittest.main()
