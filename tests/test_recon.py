"""对账仓合同测试：两腿配齐、同号重量相等才签、切换期完整对、旧任务可读、
补交晚到腿形成新日结、旧签单冻结来源复核。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.ingest import TaskRepository
from confcat.recon import ReconError, ReconciliationWarehouse

# 两厂各借共享系数 rate；A 可在各自子树显式覆盖。
CONFIG = """rate: &rate 1
factory_a:
  rate: *rate
  weigh:
    column_mapping:
      handover_id: h
      weight: w
factory_b:
  rate: *rate
  weigh:
    column_mapping:
      handover_id: h
      weight: w
"""

# 后一版本：共享系数 2，两厂都跟随。
CONFIG_RATE2 = CONFIG.replace("rate: &rate 1", "rate: &rate 2")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cat = Catalog(d / "cat.db")
        self.repo = TaskRepository(d / "task.db", self.cat)
        self.wh = ReconciliationWarehouse(d / "recon.db", self.cat)

    def tearDown(self):
        self.wh.close()
        self.repo.close()
        self.cat.close()
        self.tmp.cleanup()

    def csv(self, name: str, rows: str) -> Path:
        p = Path(self.tmp.name) / name
        p.write_text("h,w\n" + rows, encoding="utf-8")
        return p


class TwoLegRequirementTest(_Base):
    def test_pending_until_both_legs_present(self):
        self.cat.publish("p", CONFIG)
        a = self.wh.create_weigh_task("A", "factory_a", "p", "doc-0", self.csv("a.csv", "SYN-1,10\n"))
        b = self.wh.create_weigh_task("B", "factory_b", "p", "doc-0", self.csv("b.csv", "SYN-1,10\nSYN-2,20\n"))
        self.wh.register_generation("P", "A", "B")
        self.wh.activate_generation("P", 1)
        s = self.wh.settle("S", "P")
        self.assertFalse(s.signed)
        self.assertEqual(s.reasons[0]["kind"], "incomplete")
        # 发出腿只有 SYN-1，收到腿还多一条 SYN-2：缺少发出腿
        self.assertEqual(s.reasons[0]["missing_outbound"], ["SYN-2"])

    def test_signed_only_when_all_equal(self):
        self.cat.publish("p", CONFIG)
        a = self.wh.create_weigh_task("A", "factory_a", "p", "doc-0", self.csv("a.csv", "SYN-1,10\nSYN-2,20\n"))
        b = self.wh.create_weigh_task("B", "factory_b", "p", "doc-0", self.csv("b.csv", "SYN-1,10\nSYN-2,20\n"))
        self.wh.register_generation("P", "A", "B")
        self.wh.activate_generation("P", 1)
        s = self.wh.settle("S", "P")
        self.assertTrue(s.signed)
        self.assertIsNotNone(s.slip_id)

    def test_empty_day_does_not_sign(self):
        self.cat.publish("p", CONFIG)
        a = self.wh.create_weigh_task("A", "factory_a", "p", "doc-0", self.csv("a.csv", ""))  # 仅表头
        b = self.wh.create_weigh_task("B", "factory_b", "p", "doc-0", self.csv("b.csv", ""))
        self.wh.register_generation("P", "A", "B")
        self.wh.activate_generation("P", 1)
        s = self.wh.settle("S", "P")
        self.assertFalse(s.signed)
        self.assertEqual(s.reasons[0]["kind"], "empty")

    def test_one_handover_mismatch_blocks_signing(self):
        self.cat.publish("p", CONFIG)
        a = self.wh.create_weigh_task("A", "factory_a", "p", "doc-0", self.csv("a.csv", "SYN-1,10\n"))
        b = self.wh.create_weigh_task("B", "factory_b", "p", "doc-0", self.csv("b.csv", "SYN-1,11\n"))
        self.wh.register_generation("P", "A", "B")
        self.wh.activate_generation("P", 1)
        s = self.wh.settle("S", "P")
        self.assertFalse(s.signed)
        self.assertEqual(s.reasons[0]["kind"], "mismatch")


class FactorSelectionTest(_Base):
    """左支A显式1、右支共享2：A 用 1、B 用 2，原始两腿10 -> 10 vs 20 不得签。"""

    def test_split_factor_cannot_sign(self):
        # 汇合后：A 显式 1，B 跟随 2
        self.cat.publish(
            "split",
            """rate: &rate 2
factory_a:
  rate: 1
  weigh:
    column_mapping:
      handover_id: h
      weight: w
factory_b:
  rate: *rate
  weigh:
    column_mapping:
      handover_id: h
      weight: w
""",
        )
        a = self.wh.create_weigh_task("A", "factory_a", "split", "doc-0", self.csv("a.csv", "SYN-1,10\n"))
        b = self.wh.create_weigh_task("B", "factory_b", "split", "doc-0", self.csv("b.csv", "SYN-1,10\n"))
        self.assertEqual(self.wh.get_readings("A")["SYN-1"]["converted"], "10")
        self.assertEqual(self.wh.get_readings("B")["SYN-1"]["converted"], "20")
        self.wh.register_generation("P", "A", "B")
        self.wh.activate_generation("P", 1)
        s = self.wh.settle("S", "P")
        self.assertFalse(s.signed)

    def test_unified_factor_each_20_signs(self):
        self.cat.publish("unified", CONFIG_RATE2)
        a = self.wh.create_weigh_task("A", "factory_a", "unified", "doc-0", self.csv("a.csv", "SYN-1,10\n"))
        b = self.wh.create_weigh_task("B", "factory_b", "unified", "doc-0", self.csv("b.csv", "SYN-1,10\n"))
        self.assertEqual(self.wh.get_readings("A")["SYN-1"]["converted"], "20")
        self.assertEqual(self.wh.get_readings("B")["SYN-1"]["converted"], "20")
        self.wh.register_generation("P", "A", "B")
        self.wh.activate_generation("P", 1)
        s = self.wh.settle("S", "P")
        self.assertTrue(s.signed)

    def test_unified_factor_but_received_actual_9_blocks(self):
        self.cat.publish("unified", CONFIG_RATE2)
        a = self.wh.create_weigh_task("A", "factory_a", "unified", "doc-0", self.csv("a.csv", "SYN-1,10\n"))
        b = self.wh.create_weigh_task("B", "factory_b", "unified", "doc-0", self.csv("b.csv", "SYN-1,9\n"))
        self.assertEqual(self.wh.get_readings("B")["SYN-1"]["converted"], "18")
        self.wh.register_generation("P", "A", "B")
        self.wh.activate_generation("P", 1)
        s = self.wh.settle("S", "P")
        self.assertFalse(s.signed)
        self.assertEqual(s.reasons[0]["kind"], "mismatch")


class AtomicSwitchTest(_Base):
    def test_reader_sees_complete_pair_across_switch(self):
        self.cat.publish("v", CONFIG)
        self.cat.publish("v", CONFIG_RATE2)
        g1a = self.wh.create_weigh_task("G1A", "factory_a", "v", "doc-0", self.csv("a1.csv", "SYN-1,10\n"), version=1)
        g1b = self.wh.create_weigh_task("G1B", "factory_b", "v", "doc-0", self.csv("b1.csv", "SYN-1,10\n"), version=1)
        g2a = self.wh.create_weigh_task("G2A", "factory_a", "v", "doc-0", self.csv("a2.csv", "SYN-1,10\n"), version=2)
        g2b = self.wh.create_weigh_task("G2B", "factory_b", "v", "doc-0", self.csv("b2.csv", "SYN-1,10\n"), version=2)
        self.wh.register_generation("P", "G1A", "G1B")
        self.wh.register_generation("P", "G2A", "G2B")
        self.wh.activate_generation("P", 1)
        old = self.wh.active_pair("P")
        self.assertEqual((old.outbound_task, old.received_task), ("G1A", "G1B"))
        self.assertEqual((old.outbound_version, old.received_version), (1, 1))
        # 切到第 2 代：读者一次 JOIN 得到完整新对
        self.wh.activate_generation("P", 2)
        new = self.wh.active_pair("P")
        self.assertEqual(new.generation, 2)
        self.assertEqual((new.outbound_task, new.received_task), ("G2A", "G2B"))
        self.assertEqual((new.outbound_version, new.received_version), (2, 2))

    def test_old_task_still_readable_after_switch(self):
        self.cat.publish("v", CONFIG)
        self.cat.publish("v", CONFIG_RATE2)
        self.wh.create_weigh_task("G1A", "factory_a", "v", "doc-0", self.csv("a1.csv", "SYN-1,10\n"), version=1)
        self.wh.create_weigh_task("G1B", "factory_b", "v", "doc-0", self.csv("b1.csv", "SYN-1,10\n"), version=1)
        self.wh.create_weigh_task("G2A", "factory_a", "v", "doc-0", self.csv("a2.csv", "SYN-1,10\n"), version=2)
        self.wh.create_weigh_task("G2B", "factory_b", "v", "doc-0", self.csv("b2.csv", "SYN-1,10\n"), version=2)
        self.wh.register_generation("P", "G1A", "G1B")
        self.wh.register_generation("P", "G2A", "G2B")
        self.wh.activate_generation("P", 1)
        self.wh.activate_generation("P", 2)
        # 旧任务读数仍是旧系数（版本冻结）
        self.assertEqual(self.wh.get_readings("G2A")["SYN-1"]["converted"], "20")
        self.assertEqual(self.wh.get_readings("G1A")["SYN-1"]["converted"], "10")
        self.assertEqual(self.wh.get_task("G1A").version, 1)

    def test_activate_unknown_generation_rejected(self):
        self.cat.publish("v1", CONFIG)
        self.wh.create_weigh_task("A", "factory_a", "v1", "doc-0", self.csv("a.csv", "SYN-1,10\n"))
        self.wh.create_weigh_task("B", "factory_b", "v1", "doc-0", self.csv("b.csv", "SYN-1,10\n"))
        self.wh.register_generation("P", "A", "B")
        with self.assertRaises(ReconError):
            self.wh.activate_generation("P", 9)

    def test_pair_requires_two_factories_same_product(self):
        self.cat.publish("v", CONFIG)   # v1
        self.cat.publish("v", CONFIG)   # v2
        self.cat.publish("other", CONFIG)
        self.wh.create_weigh_task("A1", "factory_a", "v", "doc-0", self.csv("a.csv", "SYN-1,10\n"), version=1)
        self.wh.create_weigh_task("A2", "factory_a", "v", "doc-0", self.csv("a2.csv", "SYN-1,10\n"), version=1)
        self.wh.create_weigh_task("B1", "factory_b", "v", "doc-0", self.csv("b.csv", "SYN-1,10\n"), version=2)
        self.wh.create_weigh_task("C1", "factory_b", "other", "doc-0", self.csv("c.csv", "SYN-1,10\n"))
        # 同厂两腿
        with self.assertRaises(ReconError):
            self.wh.register_generation("P", "A1", "A2")
        # 不同产品
        with self.assertRaises(ReconError):
            self.wh.register_generation("P", "A1", "C1")
        # 同产品、两厂、不同版本（允许：切代本身就是要换版本）
        self.wh.register_generation("P", "A1", "B1")


class LateLegAndReopenTest(_Base):
    def test_late_leg_forms_new_settlement_old_slip_reopens(self):
        self.cat.publish("v1", CONFIG)
        # 首日：只有两腿各一条且相等 -> 签单
        self.wh.create_weigh_task("A", "factory_a", "v1", "doc-0", self.csv("a.csv", "SYN-1,10\n"))
        self.wh.create_weigh_task("B", "factory_b", "v1", "doc-0", self.csv("b.csv", "SYN-1,10\n"))
        self.wh.register_generation("P", "A", "B")
        self.wh.activate_generation("P", 1)
        first = self.wh.settle("S1", "P")
        self.assertTrue(first.signed)
        slip1 = first.slip_id

        # 晚到的腿（SYN-2）到达：另立新一代并形成新日结
        self.wh.create_weigh_task("A2", "factory_a", "v1", "doc-0",
                                   self.csv("a2.csv", "SYN-1,10\nSYN-2,20\n"))
        self.wh.create_weigh_task("B2", "factory_b", "v1", "doc-0",
                                  self.csv("b2.csv", "SYN-1,10\nSYN-2,20\n"))
        g2 = self.wh.register_generation("P", "A2", "B2")
        self.wh.activate_generation("P", g2)
        second = self.wh.settle("S2", "P")
        self.assertTrue(second.signed)
        self.assertNotEqual(second.slip_id, slip1)

        # 旧签单按冻结来源重开复核，仍是签单时的两腿读数，不受后续补交影响
        reopened = self.wh.reopen_slip(slip1)
        self.assertEqual(reopened["recheck"], "consistent")
        self.assertEqual(sorted(reopened["frozen"]["handovers"]), ["SYN-1"])
        self.assertEqual(reopened["frozen"]["handovers"]["SYN-1"]["outbound"], "10")
        # 旧日结仍然可查且状态冻结为已签
        self.assertEqual(self.wh.get_settlement("S1").status, "signed")

    def test_duplicate_settlement_rejected(self):
        self.cat.publish("v1", CONFIG)
        self.wh.create_weigh_task("A", "factory_a", "v1", "doc-0", self.csv("a.csv", "SYN-1,10\n"))
        self.wh.create_weigh_task("B", "factory_b", "v1", "doc-0", self.csv("b.csv", "SYN-1,10\n"))
        self.wh.register_generation("P", "A", "B")
        self.wh.activate_generation("P", 1)
        self.wh.settle("S", "P")
        with self.assertRaises(ReconError):
            self.wh.settle("S", "P")


class SingleVersionCompatibilityTest(_Base):
    def test_existing_single_version_ingest_still_works(self):
        # 保持现有单版本接入能力：原 TaskRepository 不受对账仓影响
        self.cat.publish(
            "legacy",
            "ingest:\n  source_uri: file:///x.csv\n"
            "  column_mapping:\n    order_id: h\n    region_name: w\n",
        )
        csv = self.csv("legacy.csv", "")
        summary = self.repo.create_task("legacy1", "legacy", "doc-0", csv)
        self.assertEqual(summary.row_count, 0)
        self.assertIn("legacy1", [t.task_id for t in self.repo.list_tasks()])


if __name__ == "__main__":
    unittest.main()
