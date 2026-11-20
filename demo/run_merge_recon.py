"""端到端演示：两厂离线编辑同一共享配置 -> 三路汇合 -> 对账仓原子切换与日结。

运行：``python demo/run_merge_recon.py``
产物：``data/demo_merge_catalog.db``、``data/demo_recon.db``，含内置断言。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from confcat.catalog import Catalog
from confcat.merge3 import merge_edit_packs
from confcat.recon import ReconciliationWarehouse
from confcat.semantic import EditPack, SAlias, from_yaml

CATALOG_DB = ROOT / "data" / "demo_merge_catalog.db"
RECON_DB = ROOT / "data" / "demo_recon.db"
for f in (CATALOG_DB, RECON_DB):
    f.unlink(missing_ok=True)

# 共同版本：两厂都借共享系数 rate=1。
BASE = """rate: &rate 1
factory_a:
  rate: *rate
  weigh:
    column_mapping:
      handover_id: handover_id
      weight: weight
factory_b:
  rate: *rate
  weigh:
    column_mapping:
      handover_id: handover_id
      weight: weight
"""


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def write_csv(path: Path, rows: list[tuple[str, int]]) -> Path:
    path.write_text("handover_id,weight\n" + "".join(f"{h},{w}\n" for h, w in rows), encoding="utf-8")
    return path


def main() -> int:
    hr("0) 语义检查：把别名换一种拼写仍指原声明，不算改值")
    base_doc = from_yaml(BASE)
    respelled = base_doc.clone()
    respelled.set_node(("factory_a", "rate"), SAlias(("rate",)))

    # 左支（A 厂）：给 A 加显式 1（同值覆盖，保留以后独立的意图）
    left_split = EditPack("A").add_override(("factory_a", "rate"), 1)
    # 右支（B 厂）：把共享声明改成 2
    right_shared = EditPack("B").set_shared(("rate",), 2)
    merged_split = merge_edit_packs(BASE, left_split, right_shared)
    print("汇合1（A 显式1 / 共享2） -> A=%s B=%s 冲突=%d"
          % (merged_split.document.effective_at(("factory_a", "rate")),
             merged_split.document.effective_at(("factory_b", "rate")),
             len(merged_split.conflicts)))
    assert merged_split.document.effective_at(("factory_a", "rate")) == 1
    assert merged_split.document.effective_at(("factory_b", "rate")) == 2
    assert merged_split.document.is_override(("factory_a", "rate"))  # 意图保留

    # 左支（A 厂）：仅把别名换拼写仍指原声明；右支共享改 2
    left_respell = EditPack("A").alias(("factory_a", "rate"), ("rate",))
    merged_unified = merge_edit_packs(BASE, left_respell, right_shared)
    print("汇合2（仅换拼写 / 共享2） -> A=%s B=%s 冲突=%d"
          % (merged_unified.document.effective_at(("factory_a", "rate")),
             merged_unified.document.effective_at(("factory_b", "rate")),
             len(merged_unified.conflicts)))
    assert merged_unified.document.effective_at(("factory_a", "rate")) == 2
    assert merged_unified.document.effective_at(("factory_b", "rate")) == 2

    csv_a = write_csv(ROOT / "data" / "handover_a.csv", [("SYN-1", 10), ("SYN-2", 20)])
    csv_b = write_csv(ROOT / "data" / "handover_b.csv", [("SYN-1", 10), ("SYN-2", 20)])
    csv_b_mismatch = write_csv(ROOT / "data" / "handover_b_9.csv", [("SYN-1", 9), ("SYN-2", 20)])

    with Catalog(CATALOG_DB) as cat, ReconciliationWarehouse(RECON_DB, cat) as wh:
        hr("1) 发布汇合结果：分裂系数版与统一系数版")
        split_v = cat.publish("shared", merged_split.document.to_yaml())
        unified_v = cat.publish("shared", merged_unified.document.to_yaml())
        print("shared v%s（A=1,B=2）, v%s（A=B=2）" % (split_v.version, unified_v.version))

        hr("2) 两站各自导入本地 CSV，按所绑版本系数换算实际重量")
        wh.create_weigh_task("A-split", "factory_a", "shared", "doc-0", csv_a, version=1)
        wh.create_weigh_task("B-split", "factory_b", "shared", "doc-0", csv_b, version=1)
        print("A-split SYN-1:", wh.get_readings("A-split")["SYN-1"])
        print("B-split SYN-1:", wh.get_readings("B-split")["SYN-1"])
        assert wh.get_readings("A-split")["SYN-1"]["converted"] == "10"
        assert wh.get_readings("B-split")["SYN-1"]["converted"] == "20"
        wh.register_generation("P", "A-split", "B-split")
        wh.activate_generation("P", 1)
        s = wh.settle("day-split", "P")
        print("分裂系数日结:", s.status, s.reasons[0]["kind"] if s.reasons else "")
        assert not s.signed  # 原始两腿都是 10，但换算 10 vs 20 -> 不得签

        hr("3) 统一切到第二版：两腿各 20，可签日结")
        wh.create_weigh_task("A-uni", "factory_a", "shared", "doc-0", csv_a, version=2)
        wh.create_weigh_task("B-uni", "factory_b", "shared", "doc-0", csv_b, version=2)
        g = wh.register_generation("P", "A-uni", "B-uni")
        wh.activate_generation("P", g)
        s2 = wh.settle("day-uni", "P")
        print("统一系数日结:", s2.status, "签单", s2.slip_id)
        assert s2.signed
        # 旧任务仍可读旧版
        print("旧任务 A-split 仍是 v%s, 换算 %s"
              % (wh.get_task("A-split").version, wh.get_readings("A-split")["SYN-1"]["converted"]))
        assert wh.get_task("A-split").version == 1

        hr("4) 固定第二版但收到腿实为 9：仍不得签")
        wh.create_weigh_task("A-uni2", "factory_a", "shared", "doc-0", csv_a, version=2)
        wh.create_weigh_task("B-uni9", "factory_b", "shared", "doc-0", csv_b_mismatch, version=2)
        g2 = wh.register_generation("P", "A-uni2", "B-uni9")
        wh.activate_generation("P", g2)
        s3 = wh.settle("day-uni9", "P")
        print("收到腿 9 日结:", s3.status, s3.reasons[0]["kind"])
        assert not s3.signed

        hr("5) 补交晚到的腿 -> 新日结；旧签单按冻结来源重开复核")
        csv_b_late = write_csv(ROOT / "data" / "handover_b_late.csv", [("SYN-1", 10), ("SYN-2", 20)])
        csv_a_late = write_csv(ROOT / "data" / "handover_a_late.csv", [("SYN-1", 10), ("SYN-2", 20)])
        wh.create_weigh_task("A-late", "factory_a", "shared", "doc-0", csv_a_late, version=2)
        wh.create_weigh_task("B-late", "factory_b", "shared", "doc-0", csv_b_late, version=2)
        g3 = wh.register_generation("P", "A-late", "B-late")
        wh.activate_generation("P", g3)
        s4 = wh.settle("day-late", "P")
        print("补交后新日结:", s4.status, "签单", s4.slip_id)
        assert s4.signed
        reopened = wh.reopen_slip(s2.slip_id)
        print("旧签单重开:", reopened["recheck"], "冻结移交号", sorted(reopened["frozen"]["handovers"]))
        assert reopened["recheck"] == "consistent"

        hr("6) 切换期完整对：读者一次 JOIN 只见同代两条任务")
        view = wh.active_pair("P")
        print("当前激活代:", view.generation, "->", view.outbound_task, "/", view.received_task)
        registered = {
            (g["outbound_task"], g["received_task"]): g["generation"]
            for g in wh.list_generations("P")
        }
        # 读者拿到的两条任务必属于同一已登记代，绝不混代
        assert registered[(view.outbound_task, view.received_task)] == view.generation

    hr("全部演示断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
