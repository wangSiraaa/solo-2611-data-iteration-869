"""三路语义汇合合同测试：别名身份、显式同值覆盖意图、未决冲突、互不相交合并。"""

from __future__ import annotations

import unittest

from confcat.errors import MergeConflictError, SemanticError
from confcat.merge3 import (
    merge_documents,
    merge_edit_packs,
    merge_yaml,
    node_equal,
    resolve_conflict,
)
from confcat.semantic import EditPack, SAlias, SDecl, SScalar, from_yaml

BASE = """rate: &rate 1
factory_a:
  rate: *rate
factory_b:
  rate: *rate
"""


def eff(text_or_doc):
    if isinstance(text_or_doc, str):
        return from_yaml(text_or_doc).effective_plain()
    return text_or_doc.effective_plain()


class SemanticRoundTripTest(unittest.TestCase):
    def test_alias_round_trip_preserves_effective(self):
        doc = from_yaml(BASE)
        again = from_yaml(doc.to_yaml())
        self.assertEqual(doc.effective_plain(), again.effective_plain())

    def test_alias_identity_ignores_spelling(self):
        # 把 factory_a 的别名换一种拼写（加一层带锚点的别名链），仍指原声明
        base = from_yaml(BASE)
        respelled = base.clone()
        respelled.set_node(("factory_a", "rate"), SDecl("aliasA", SAlias(("rate",))))
        self.assertTrue(node_equal(base.node_at(("factory_a", "rate")), respelled.node_at(("factory_a", "rate"))))

    def test_multi_document_rejected(self):
        with self.assertRaises(SemanticError):
            from_yaml("---\na: 1\n---\nb: 2\n")

    def test_merge_marker_unsupported_in_semantic_edit(self):
        with self.assertRaises(SemanticError):
            from_yaml("base: &b\n  k: v\nt:\n  <<: *b\n")


class ThreeWayMergeTest(unittest.TestCase):
    def test_explicit_override_left_shared_change_right(self):
        base = from_yaml(BASE)
        left = base.clone()
        left.add_override(("factory_a", "rate"), 1)  # 显式同值 1
        right = base.clone()
        right.set_value(("rate",), 2)  # 共享声明改 2
        res = merge_documents(base, left, right)
        self.assertFalse(res.has_conflicts)
        self.assertEqual(res.document.effective_at(("factory_a", "rate")), 1)
        self.assertEqual(res.document.effective_at(("factory_b", "rate")), 2)
        # 显式覆盖的“以后独立”意图保留：A 仍是显式标量而非别名
        self.assertTrue(res.document.is_override(("factory_a", "rate")))
        self.assertFalse(res.document.is_override(("factory_b", "rate")))

    def test_alias_respell_does_not_block_shared_change(self):
        base = from_yaml(BASE)
        left = base.clone()
        left.set_node(("factory_a", "rate"), SDecl("aliasA", SAlias(("rate",))))
        right = base.clone()
        right.set_value(("rate",), 2)
        res = merge_documents(base, left, right)
        self.assertFalse(res.has_conflicts)
        self.assertEqual(res.document.effective_at(("factory_a", "rate")), 2)
        self.assertEqual(res.document.effective_at(("factory_b", "rate")), 2)

    def test_conflicting_targets_kept_unresolved(self):
        base = from_yaml(BASE)
        left = base.clone()
        left.set_value(("rate",), 3)
        right = base.clone()
        right.set_value(("rate",), 2)
        res = merge_documents(base, left, right)
        self.assertTrue(res.has_conflicts)
        kinds = [c.kind for c in res.conflicts]
        self.assertEqual(kinds, ["value"])
        self.assertEqual(res.conflicts[0].path, ("rate",))
        # 未决冲突处保留 base 值，供人工裁定
        self.assertEqual(res.document.effective_at(("rate",)), 1)

    def test_identical_change_on_both_sides_merges(self):
        base = from_yaml(BASE)
        left = base.clone()
        left.set_value(("rate",), 7)
        right = base.clone()
        right.set_value(("rate",), 7)
        res = merge_documents(base, left, right)
        self.assertFalse(res.has_conflicts)
        self.assertEqual(res.document.effective_at(("rate",)), 7)

    def test_disjoint_edits_merge(self):
        base = from_yaml(BASE)
        left = base.clone()
        left.set_value(("factory_a", "rate"), 5)
        right = base.clone()
        right.set_value(("factory_b", "rate"), 7)
        res = merge_documents(base, left, right)
        self.assertFalse(res.has_conflicts)
        self.assertEqual(res.document.effective_at(("factory_a", "rate")), 5)
        self.assertEqual(res.document.effective_at(("factory_b", "rate")), 7)

    def test_delete_modify_kept_as_conflict(self):
        base = from_yaml(BASE)
        left = base.clone()
        left.remove(("factory_b",))
        right = base.clone()
        right.add_override(("factory_b", "rate"), 9)
        res = merge_documents(base, left, right)
        self.assertTrue(res.has_conflicts)
        self.assertEqual(res.conflicts[0].kind, "delete-modify")

    def test_resolve_conflict(self):
        base = from_yaml(BASE)
        left = base.clone()
        left.set_value(("rate",), 3)
        right = base.clone()
        right.set_value(("rate",), 2)
        res = merge_documents(base, left, right)
        resolve_conflict(res, ("rate",), 4)
        self.assertFalse(res.has_conflicts)
        self.assertEqual(res.document.effective_at(("rate",)), 4)

    def test_merged_yaml_reparses_to_same_effective(self):
        base = from_yaml(BASE)
        left = base.clone()
        left.add_override(("factory_a", "rate"), 1)
        right = base.clone()
        right.set_value(("rate",), 2)
        res = merge_documents(base, left, right)
        reparsed = from_yaml(res.document.to_yaml())
        self.assertEqual(reparsed.effective_plain(), res.document.effective_plain())

    def test_merge_yaml_convenience(self):
        left = BASE.replace("factory_a:\n  rate: *rate", "factory_a:\n  rate: 1")
        right = BASE.replace("rate: &rate 1", "rate: &rate 2")
        res = merge_yaml(BASE, left, right)
        self.assertFalse(res.has_conflicts)
        self.assertEqual(res.document.effective_at(("factory_a", "rate")), 1)
        self.assertEqual(res.document.effective_at(("factory_b", "rate")), 2)


class OverrideIntentTest(unittest.TestCase):
    def test_same_value_override_is_a_real_edit(self):
        base = from_yaml(BASE)
        with_override = base.clone()
        with_override.add_override(("factory_a", "rate"), 1)
        # 值相同，但节点种类（别名->显式标量）不同，属真实修改
        self.assertFalse(
            node_equal(base.node_at(("factory_a", "rate")), with_override.node_at(("factory_a", "rate")))
        )

    def test_remove_override_restores_alias(self):
        base = from_yaml(BASE)
        with_override = base.clone()
        with_override.add_override(("factory_a", "rate"), 1)
        restored = with_override.clone()
        restored.remove_override(("factory_a", "rate"), ("rate",))
        self.assertIsInstance(restored.node_at(("factory_a", "rate")), SAlias)
        self.assertFalse(restored.is_override(("factory_a", "rate")))

    def test_remove_override_then_shared_change_flows(self):
        base = from_yaml(BASE)
        left = base.clone()
        left.add_override(("factory_a", "rate"), 1)
        left.remove_override(("factory_a", "rate"), ("rate",))
        right = base.clone()
        right.set_value(("rate",), 2)
        res = merge_documents(base, left, right)
        self.assertFalse(res.has_conflicts)
        self.assertEqual(res.document.effective_at(("factory_a", "rate")), 2)


class EditPackTest(unittest.TestCase):
    """编辑包以来源声明或消费路径为对象：改共享、加同值覆盖、撤覆盖、换拼写。"""

    def test_override_vs_shared_change_via_packs(self):
        left = EditPack("A").add_override(("factory_a", "rate"), 1)
        right = EditPack("B").set_shared(("rate",), 2)
        res = merge_edit_packs(BASE, left, right)
        self.assertFalse(res.has_conflicts)
        self.assertEqual(res.document.effective_at(("factory_a", "rate")), 1)
        self.assertEqual(res.document.effective_at(("factory_b", "rate")), 2)
        self.assertTrue(res.document.is_override(("factory_a", "rate")))

    def test_alias_respell_via_pack(self):
        left = EditPack("A").alias(("factory_a", "rate"), ("rate",))
        right = EditPack("B").set_shared(("rate",), 2)
        res = merge_edit_packs(BASE, left, right)
        self.assertFalse(res.has_conflicts)
        self.assertEqual(res.document.effective_at(("factory_a", "rate")), 2)
        self.assertEqual(res.document.effective_at(("factory_b", "rate")), 2)

    def test_add_then_remove_override_via_pack(self):
        # 左支先加同值覆盖再撤去：拍板回到别名，右支共享改 2 应生效
        left = EditPack("A").add_override(("factory_a", "rate"), 1).remove_override(
            ("factory_a", "rate"), ("rate",)
        )
        right = EditPack("B").set_shared(("rate",), 2)
        res = merge_edit_packs(BASE, left, right)
        self.assertFalse(res.has_conflicts)
        self.assertEqual(res.document.effective_at(("factory_a", "rate")), 2)


class PublisherIntegrationTest(unittest.TestCase):
    def test_catalog_rejects_unresolved_and_publishes_clean_merge(self):
        import tempfile
        from pathlib import Path

        from confcat.catalog import Catalog

        tmp = tempfile.TemporaryDirectory()
        try:
            cat = Catalog(Path(tmp.name) / "c.db")
            left = EditPack("A").set_shared(("rate",), 3)
            right = EditPack("B").set_shared(("rate",), 2)
            conflicted = merge_edit_packs(BASE, left, right)
            with self.assertRaises(MergeConflictError):
                cat.publish_merge("p", conflicted)
            resolve_conflict(conflicted, ("rate",), 5)
            info = cat.publish_merge("p", conflicted)
            self.assertEqual(cat.query("p", "doc-0", ("rate",), version=info.version).value, 5)
            cat.close()
        finally:
            tmp.cleanup()

    def test_two_truths_survive_publish_and_query(self):
        import tempfile
        from pathlib import Path

        from confcat.catalog import Catalog

        tmp = tempfile.TemporaryDirectory()
        try:
            cat = Catalog(Path(tmp.name) / "c.db")

            # 真值 1：左支 A 显式 1，右支共享 2 -> A=1, B=2
            split = merge_edit_packs(
                BASE, EditPack("A").add_override(("factory_a", "rate"), 1),
                EditPack("B").set_shared(("rate",), 2),
            )
            assert not split.has_conflicts
            cat.publish_merge("p", split)
            qa = cat.query("p", "doc-0", ("factory_a", "rate"), version=1)
            qb = cat.query("p", "doc-0", ("factory_b", "rate"), version=1)
            self.assertEqual((qa.value, qb.value), (1, 2))
            # 结构上：A 是显式标量（以后独立），B 仍是别名指向共享声明
            src = cat.source_yaml("p", version=1)
            self.assertIn("rate: 1", src)          # A 的显式值
            self.assertIn("rate: *rate", src)      # B 保持别名

            # 真值 2：左支仅换别名拼写，右支共享 2 -> A=B=2
            unified = merge_edit_packs(
                BASE, EditPack("A").alias(("factory_a", "rate"), ("rate",)),
                EditPack("B").set_shared(("rate",), 2),
            )
            assert not unified.has_conflicts
            cat.publish_merge("p", unified)
            qa2 = cat.query("p", "doc-0", ("factory_a", "rate"), version=2)
            qb2 = cat.query("p", "doc-0", ("factory_b", "rate"), version=2)
            self.assertEqual((qa2.value, qb2.value), (2, 2))
            cat.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
