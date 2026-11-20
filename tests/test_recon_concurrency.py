"""对账仓并发与规模测试：切换期读者永不混代；少量配置改动服务上千任务；
两站断续交接。

并发模型：写方用独立连接反复切换任务对代；读者用独立连接不断 ``active_pair``
并 JOIN 读取两条任务，断言任一时刻看到的 (outbound_task, received_task) 必是
已登记的完整一代，不会出现“新发出 + 旧收到”的混代。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.recon import ReconciliationWarehouse

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


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cat = Catalog(self.d / "cat.db")
        self.cat.publish("p", CONFIG)

    def tearDown(self):
        self.cat.close()
        self.tmp.cleanup()

    def csv(self, name: str, rows: str) -> Path:
        p = self.d / name
        p.write_text("h,w\n" + rows, encoding="utf-8")
        return p

    def make_wh(self) -> ReconciliationWarehouse:
        return ReconciliationWarehouse(self.d / "recon.db", self.cat)


class ConcurrentSwitchTest(_Base):
    def test_readers_never_see_mixed_generation(self):
        wh = self.make_wh()
        generations = []
        # 造 4 代，每代两条任务
        for g in range(1, 5):
            a = wh.create_weigh_task(f"A{g}", "factory_a", "p", "doc-0", self.csv(f"a{g}.csv", "SYN-1,10\n"))
            b = wh.create_weigh_task(f"B{g}", "factory_b", "p", "doc-0", self.csv(f"b{g}.csv", "SYN-1,10\n"))
            wh.register_generation("P", f"A{g}", f"B{g}")
            generations.append((f"A{g}", f"B{g}"))
        wh.activate_generation("P", 1)
        allowed = set(generations)

        stop = threading.Event()
        errors: list[str] = []

        def reader():
            reader_wh = self.make_wh()
            try:
                while not stop.is_set():
                    view = reader_wh.active_pair("P")
                    pair = (view.outbound_task, view.received_task)
                    if pair not in allowed:
                        errors.append(f"混合代: {pair}")
                    g_a = int(view.outbound_task[1:])
                    g_b = int(view.received_task[1:])
                    if g_a != g_b:
                        errors.append(f"任务对不完整: {pair}")
            finally:
                reader_wh.close()

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for _ in range(200):
                for g in range(1, 5):
                    wh.activate_generation("P", g)
        finally:
            stop.set()
            for t in threads:
                t.join()
            wh.close()
        self.assertEqual(errors, [])


class ManyTasksSharedConfigTest(_Base):
    """少量配置修改被上千张任务共用：改 1 处系数后，新任务用新值、旧任务仍用旧值。"""

    def test_over_thousand_tasks_share_one_config_edit(self):
        wh = self.make_wh()
        n = 600  # 600 旧 + 600 新 = 1200 张任务，> 1000
        # 全部用 v1 生成旧任务
        for i in range(n):
            wh.create_weigh_task(
                f"old{i}", "factory_a", "p", "doc-0", self.csv(f"old{i}.csv", "SYN-1,10\n"), version=1
            )
        # 发布 v2：只改共享系数一处
        self.cat.publish("p", CONFIG.replace("rate: &rate 1", "rate: &rate 2"))
        for i in range(n):
            wh.create_weigh_task(
                f"new{i}", "factory_a", "p", "doc-0", self.csv(f"new{i}.csv", "SYN-1,10\n"), version=2
            )
        total = len(wh.conn.execute("SELECT task_id FROM weigh_task").fetchall())
        self.assertGreater(total, 1000)
        # 旧任务永久按旧系数，新任务按新系数
        self.assertEqual(wh.get_readings("old0")["SYN-1"]["converted"], "10")
        self.assertEqual(wh.get_readings(f"old{n-1}")["SYN-1"]["converted"], "10")
        self.assertEqual(wh.get_readings("new0")["SYN-1"]["converted"], "20")
        self.assertEqual(wh.get_readings(f"new{n-1}")["SYN-1"]["converted"], "20")


class IntermittentHandoverTest(_Base):
    """两站断续交接：一批移交号分批到达，逐次补交直至配齐才签。"""

    def test_staggered_arrival_signs_only_when_complete(self):
        wh = self.make_wh()
        # 第一次：发出腿 3 号，收到腿只到 1 号
        wh.create_weigh_task(
            "A1", "factory_a", "p", "doc-0",
            self.csv("a1.csv", "SYN-1,10\nSYN-2,20\nSYN-3,30\n"),
        )
        wh.create_weigh_task("B1", "factory_b", "p", "doc-0", self.csv("b1.csv", "SYN-1,10\n"))
        wh.register_generation("P", "A1", "B1")
        wh.activate_generation("P", 1)
        s1 = wh.settle("S1", "P")
        self.assertFalse(s1.signed)
        self.assertEqual(s1.reasons[0]["kind"], "incomplete")
        self.assertEqual(s1.reasons[0]["missing_received"], ["SYN-2", "SYN-3"])

        # 第二次：收到腿补齐 2、3 号并全部相等 -> 新代可签
        wh.create_weigh_task("B2", "factory_b", "p", "doc-0",
                              self.csv("b2.csv", "SYN-1,10\nSYN-2,20\nSYN-3,30\n"))
        g = wh.register_generation("P", "A1", "B2")
        wh.activate_generation("P", g)
        s2 = wh.settle("S2", "P")
        self.assertTrue(s2.signed)
        # 第一次日结仍为待核（历史冻结，不被后续补交改写）
        self.assertFalse(wh.get_settlement("S1").signed)


if __name__ == "__main__":
    unittest.main()
