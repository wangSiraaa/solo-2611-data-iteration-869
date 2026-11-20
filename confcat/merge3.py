"""三路语义汇合：以共同版本为基，合并两厂各自的离线编辑包。

身份与冲突规则
==============
* 节点身份是**结构路径**；别名以目标声明路径为身份，因此“换一种拼写但
  仍指原声明”不构成变更（左支别名改名 + 右支改共享值 => 共享值变化生效）。
* 每个路径对 (base, left, right) 做三路比较：
  - 仅一侧改动 => 采用该侧；
  - 两侧改成相同的值 => 采用该值（对别名目标/声明身份做语义比较）；
  - 两侧改成不同的值 => **保留未决冲突**，此处输出 base，并记录冲突；
  - 一侧删除、另一侧改动 => 保留冲突（删除与修改不可自动判定）。
* 是否“改动”用语义相等判定：``SAlias`` 按目标路径比较，``SScalar`` 按值与
  类型比较，``SDecl`` 比较内部节点（锚点拼写视为无关属性）。因此把别名换成
  同值显式值是一次**真实改动**（种类变化），显式覆盖意图得以保留。
* 互不相交的编辑（不同路径各改各的）自然合并，无需冲突记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import MergeConflictError, SemanticError
from .semantic import (
    SAlias,
    SDecl,
    SMap,
    SNode,
    SScalar,
    SSeq,
    SemanticDocument,
)


@dataclass
class Conflict:
    path: tuple
    base: SNode | None
    left: SNode | None
    right: SNode | None
    kind: str  # "value" | "delete-modify" | "structure"

    def as_dict(self) -> dict:
        return {
            "path": list(self.path),
            "kind": self.kind,
            "base": _describe(self.base),
            "left": _describe(self.left),
            "right": _describe(self.right),
        }


def _describe(node: SNode | None) -> str:
    if node is None:
        return "<absent>"
    if isinstance(node, SAlias):
        return f"&alias->{'/'.join(map(str, node.target))}"
    if isinstance(node, SScalar):
        return f"scalar({node.value!r})"
    if isinstance(node, SDecl):
        return f"decl(&{node.anchor}, {type(node.inner).__name__})"
    return type(node).__name__


@dataclass
class MergeResult:
    document: SemanticDocument
    conflicts: list[Conflict] = field(default_factory=list)
    # 每个采用路径的来源说明（base/left/right/both），便于证据回查
    decisions: dict[tuple, str] = field(default_factory=dict)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    def require_clean(self) -> SemanticDocument:
        if self.conflicts:
            raise MergeConflictError(
                f"三路汇合仍有 {len(self.conflicts)} 处未决冲突", self.conflicts
            )
        return self.document


# --------------------------------------------------------------------------- #
# 语义相等
# --------------------------------------------------------------------------- #
def node_equal(a: SNode | None, b: SNode | None) -> bool:
    """语义相等：别名按目标路径、声明忽略锚点名、标量含类型。"""
    if a is None or b is None:
        return a is b
    # 声明包裹对“值身份”透明：先剥掉再比较，使“给别名换锚点拼写”不构成变更
    if isinstance(a, SDecl):
        a = a.inner
    if isinstance(b, SDecl):
        b = b.inner
    if isinstance(a, SAlias) or isinstance(b, SAlias):
        return isinstance(a, SAlias) and isinstance(b, SAlias) and a.target == b.target
    if isinstance(a, SScalar) and isinstance(b, SScalar):
        return type(a.value) is type(b.value) and a.value == b.value
    if isinstance(a, SSeq) and isinstance(b, SSeq):
        return len(a.items) == len(b.items) and all(
            node_equal(x, y) for x, y in zip(a.items, b.items)
        )
    if isinstance(a, SMap) and isinstance(b, SMap):
        if list(a.entries.keys()) != list(b.entries.keys()):
            # 键集合不同即不相等；键顺序不参与值相等，但键集合必须一致
            if set(a.entries.keys()) != set(b.entries.keys()):
                return False
        return all(node_equal(a.entries[k], b.entries[k]) for k in a.entries)
    return False


def _strip_decl(node: SNode | None) -> SNode | None:
    return node.inner if isinstance(node, SDecl) else node


# --------------------------------------------------------------------------- #
# 汇合
# --------------------------------------------------------------------------- #
class _Merger:
    def __init__(self) -> None:
        self.conflicts: list[Conflict] = []
        self.decisions: dict[tuple, str] = {}

    def merge(
        self, base: SNode | None, left: SNode | None, right: SNode | None, path: tuple
    ) -> SNode | None:
        if node_equal(left, right):
            self.decisions[path] = "both" if not node_equal(base, left) else "base"
            return _clone(left)
        if node_equal(base, left):
            self.decisions[path] = "right"
            return _clone(right)
        if node_equal(base, right):
            self.decisions[path] = "left"
            return _clone(left)

        # 两侧都改了且不同：尝试结构合并
        lb = _strip_decl(left) if left is not None else None
        rb = _strip_decl(right) if right is not None else None
        bb = _strip_decl(base) if base is not None else None

        if left is None or right is None:
            # 一侧删除、另一侧修改
            self.conflicts.append(Conflict(path, base, left, right, "delete-modify"))
            self.decisions[path] = "conflict"
            return _clone(base)

        if isinstance(lb, SMap) and isinstance(rb, SMap):
            inner = self._merge_maps(bb if isinstance(bb, SMap) else None, lb, rb, path)
            return _rewrap(left, right, inner)

        # 叶级或结构不同的冲突
        self.conflicts.append(Conflict(path, base, left, right, "value"))
        self.decisions[path] = "conflict"
        return _clone(base)

    def _merge_maps(
        self,
        base: SMap | None,
        left: SMap,
        right: SMap,
        path: tuple,
    ) -> SMap:
        keys: list = []
        for source in (base.entries if base else {}, left.entries, right.entries):
            for k in source:
                if k not in keys:
                    keys.append(k)
        merged: dict = {}
        for k in keys:
            b = base.entries.get(k) if base else None
            l = left.entries.get(k, _ABSENT)
            r = right.entries.get(k, _ABSENT)
            child = self.merge(
                None if b is None else b,
                None if l is _ABSENT else l,
                None if r is _ABSENT else r,
                path + (k,),
            )
            if child is not None:
                merged[k] = child
        return SMap(merged)


_ABSENT = object()


def _rewrap(left: SNode, right: SNode, inner: SMap) -> SNode:
    """结构合并后恢复声明包裹：任一侧声明了锚点则保留（拼写取有声明的一侧）。"""
    for side in (left, right):
        if isinstance(side, SDecl):
            return SDecl(side.anchor, inner)
    return inner


def _clone(node: SNode | None) -> SNode | None:
    if node is None:
        return None
    if isinstance(node, SScalar):
        return SScalar(node.value)
    if isinstance(node, SAlias):
        return SAlias(node.target)
    if isinstance(node, SSeq):
        return SSeq([_clone(x) for x in node.items])
    if isinstance(node, SMap):
        return SMap({k: _clone(v) for k, v in node.entries.items()})
    if isinstance(node, SDecl):
        return SDecl(node.anchor, _clone(node.inner))
    raise SemanticError(f"无法克隆节点 {type(node).__name__}")


def merge_documents(
    base: SemanticDocument,
    left: SemanticDocument,
    right: SemanticDocument,
) -> MergeResult:
    merger = _Merger()
    # 声明包裹的锚点：三路合并时以“存在声明”为优先；锚点拼写取改动侧
    root = merger.merge(base.root, left.root, right.root, ())
    doc = SemanticDocument(root=root if root is not None else SMap({}))
    return MergeResult(document=doc, conflicts=merger.conflicts, decisions=merger.decisions)


def merge_yaml(base_text: str, left_text: str, right_text: str) -> MergeResult:
    """三路汇合三份 YAML 文本；``document.to_yaml()`` 得到可再次发布的配置。"""
    return merge_documents(from_yaml(base_text), from_yaml(left_text), from_yaml(right_text))


def merge_edit_packs(base_text: str, left_pack, right_pack) -> MergeResult:
    """从共同版本出发，分别应用两厂编辑包后做三路汇合。

    ``left_pack`` / ``right_pack`` 为 :class:`confcat.semantic.EditPack`，
    其操作对象是共享声明或消费路径。
    """
    base = from_yaml(base_text)
    left = left_pack.apply(base)
    right = right_pack.apply(base)
    return merge_documents(base, left, right)


def resolve_conflict(result: MergeResult, path: tuple, value) -> None:
    """以显式标量裁定一处未决冲突，并从冲突清单移除该路径。"""
    from .semantic import SScalar

    result.document.set_value(tuple(path), value)
    result.conflicts = [c for c in result.conflicts if c.path != tuple(path)]
    result.decisions[tuple(path)] = "resolved"


def apply_edits(base: SemanticDocument, doc: SemanticDocument) -> SemanticDocument:
    """离线编辑包：编辑方从共同版本出发得到的完整语义文档（约定式 API）。"""
    return doc


from .semantic import from_yaml  # noqa: E402  (放在末尾避免循环导入)
