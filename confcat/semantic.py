"""离线编辑用的语义配置模型：声明（锚点）、使用路径（别名）与普通值。

为什么需要它
============
两厂各自从同一产品离线编辑 YAML，再把两包改动汇合。若直接比较文本，
“把同一别名换一种拼写但仍指向原声明”会被误判成值变更。语义模型以
**结构路径**作为声明与使用路径的身份：

* 声明 ``SDecl``：锚点名只是属性，身份是它所在的结构路径；
* 使用 ``SAlias``：只记录目标**声明路径**，不记录别名的拼写；
* 普通值 ``SScalar`` / ``SMap`` / ``SSeq`` 保留原有标量类型与键类型。

因此别名改名（拼写变化）在语义上是“无变化”，而声明取值变化才是真变化；
显式同值覆盖因为节点种类由 ``SAlias`` 变成 ``SScalar``，也会被保留为一次
真实修改，从而保留“以后独立”的意图。

模块还提供一个受限 YAML 发射器，把汇合后的语义树重新写成可被
:mod:`confcat.yaml_parser` 解析的文本（锚点/别名会按合并后的声明重新拼写）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from .errors import SemanticError
from .model import ConfigKey, ParsedDocument, RawAlias, RawMapping, RawNode, RawScalar, RawSequence
from .yaml_parser import MERGE_KEY, _INT_RE, parse_text


# --------------------------------------------------------------------------- #
# 语义节点
# --------------------------------------------------------------------------- #
class SNode:
    """语义节点基类。"""


@dataclass
class SScalar(SNode):
    value: Any  # str | int | bool | None


@dataclass
class SSeq(SNode):
    items: list[SNode] = field(default_factory=list)


@dataclass
class SMap(SNode):
    # dict 保序；int 1 与 str "1" 天然是两个键
    entries: dict[ConfigKey, SNode] = field(default_factory=dict)


@dataclass
class SAlias(SNode):
    """使用路径：只记录目标声明路径（身份），不记录拼写。"""

    target: tuple


@dataclass
class SDecl(SNode):
    """来源声明：``anchor`` 是可变的拼写属性，身份由所在路径承担。"""

    anchor: str
    inner: SNode


# --------------------------------------------------------------------------- #
# 语义文档
# --------------------------------------------------------------------------- #
@dataclass
class SemanticDocument:
    root: SNode
    source_text: str = ""
    index: dict[tuple, SNode] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._reindex()
        self._canonicalize_aliases()
        self._reindex()

    # -- 别名规范化 ----------------------------------------------------- #
    def _canonicalize_aliases(self) -> None:
        """把别名目标折叠到**最终真声明路径**。

        ``x: &x *rate`` 这样的别名带锚点时会形成别名链；若两厂只是给链上
        某一环换了拼写，语义身份仍应是同一个真声明。这里把所有 ``SAlias``
        的目标统一改写为最终 ``SDecl`` 所在路径，使“换拼写不改身份”成立。
        """
        def final_decl_path(target: tuple, seen: frozenset[tuple]) -> tuple:
            node = self.index.get(target)
            if node is None or target in seen:
                return target
            if isinstance(node, SDecl):
                if isinstance(node.inner, SAlias):
                    return final_decl_path(node.inner.target, seen | {target})
                return target
            if isinstance(node, SAlias):
                return final_decl_path(node.target, seen | {target})
            return target

        def rewrite(node: SNode) -> None:
            if isinstance(node, SAlias):
                node.target = final_decl_path(node.target, frozenset())
            elif isinstance(node, SDecl):
                rewrite(node.inner)
            elif isinstance(node, SMap):
                for v in node.entries.values():
                    rewrite(v)
            elif isinstance(node, SSeq):
                for v in node.items:
                    rewrite(v)

        rewrite(self.root)

    # -- 路径索引 ------------------------------------------------------- #
    def _reindex(self) -> None:
        self.index = {}

        def walk(node: SNode, path: tuple, assign: bool) -> None:
            if assign:
                self.index[path] = node
            if isinstance(node, SMap):
                for k, v in node.entries.items():
                    walk(v, path + (k,), True)
            elif isinstance(node, SSeq):
                for i, v in enumerate(node.items):
                    walk(v, path + (i,), True)
            elif isinstance(node, SDecl):
                # 声明包裹节点：路径索引保留 SDecl（别名解析需要声明身份），
                # 内部子节点仍按同一路径向下展开
                walk(node.inner, path, False)

        walk(self.root, (), True)

    def node_at(self, path: tuple) -> SNode:
        try:
            return self.index[tuple(path)]
        except KeyError:
            raise SemanticError(f"语义路径不存在: {'/'.join(map(str, path))}") from None

    # -- 别名解析 ------------------------------------------------------- #
    def resolve(self, node: SNode) -> SNode:
        seen: set[tuple] = set()
        while isinstance(node, SAlias):
            if node.target in seen:
                raise SemanticError(f"别名目标形成循环: {node.target}")
            seen.add(node.target)
            node = self.index.get(node.target)
            if node is None:
                raise SemanticError(f"别名目标声明不存在: {node!r}")
        return node

    def effective_plain(self) -> Any:
        """解析别名/声明后的普通 Python 视图（供检查与断言）。"""

        def conv(node: SNode) -> Any:
            node = self.resolve(node)
            if isinstance(node, SDecl):
                return conv(node.inner)
            if isinstance(node, SScalar):
                return node.value
            if isinstance(node, SSeq):
                return [conv(x) for x in node.items]
            if isinstance(node, SMap):
                return {k: conv(v) for k, v in node.entries.items()}
            raise SemanticError(f"未知语义节点 {type(node).__name__}")

        return conv(self.root)

    def effective_at(self, path: tuple) -> Any:
        """按结构路径取解析别名/声明后的普通值。

        注意：``path`` 必须是**结构路径**（逐级真实键）。若中间某一级是别名
        （例如 ``factory_a/settings`` 指向 ``common`` 声明），该级之后没有独立
        结构键，请改用 ``effective_plain()`` 取整树视图。
        """
        def conv(node: SNode) -> Any:
            node = self.resolve(node)
            if isinstance(node, SDecl):
                return conv(node.inner)
            if isinstance(node, SScalar):
                return node.value
            if isinstance(node, SSeq):
                return [conv(x) for x in node.items]
            if isinstance(node, SMap):
                return {k: conv(v) for k, v in node.entries.items()}
            raise SemanticError(f"未知语义节点 {type(node).__name__}")

        return conv(self.node_at(path))

    # -- 发射 ----------------------------------------------------------- #
    def to_yaml(self) -> str:
        return emit_yaml(self)

    # -- 编辑操作（离线编辑包以声明或消费路径为对象） ------------------- #
    def clone(self) -> "SemanticDocument":
        return SemanticDocument(_clone_node(self.root), self.source_text)

    def _parent_and_key(self, path: tuple) -> tuple[SNode, Any]:
        if not path:
            raise SemanticError("不能编辑根路径")
        parent = self.node_at(path[:-1])
        while isinstance(parent, SDecl):
            parent = parent.inner
        if not isinstance(parent, (SMap, SSeq)):
            raise SemanticError(f"路径 {path} 的父节点不可编辑")
        return parent, path[-1]

    def _replace(self, parent, key: Any, node: SNode) -> None:
        # 编辑共享声明时保留其锚点包裹，别名仍指向同一身份
        current = parent.entries[key] if isinstance(parent, SMap) else parent.items[key]
        if isinstance(current, SDecl) and not isinstance(node, SDecl):
            node = SDecl(current.anchor, node)
        if isinstance(parent, SMap):
            parent.entries[key] = node
        else:
            parent.items[key] = node
        self._reindex()

    def set_value(self, path: tuple, value: Any) -> None:
        """改共享定义或普通值：把路径处的节点替换为标量（声明保留锚点）。"""
        parent, key = self._parent_and_key(path)
        self._replace(parent, key, SScalar(value))

    def set_node(self, path: tuple, node: SNode) -> None:
        parent, key = self._parent_and_key(path)
        self._replace(parent, key, node)

    def add_override(self, path: tuple, value: Any) -> None:
        """给某条使用路径加同值局部覆盖：以显式标量替换别名，保留独立意图。"""
        self.set_value(path, value)

    def remove_override(self, path: tuple, target: tuple) -> None:
        """撤去某条使用路径的显式覆盖，恢复为指向原声明的别名。"""
        self.set_node(path, SAlias(tuple(target)))

    def remove(self, path: tuple) -> None:
        parent, key = self._parent_and_key(path)
        if isinstance(parent, SMap):
            del parent.entries[key]
        else:
            del parent.items[key]
        self._reindex()

    def is_override(self, path: tuple) -> bool:
        return isinstance(self.node_at(path), SScalar)


def _clone_node(node: SNode) -> SNode:
    if isinstance(node, SScalar):
        return SScalar(node.value)
    if isinstance(node, SAlias):
        return SAlias(node.target)
    if isinstance(node, SSeq):
        return SSeq([_clone_node(x) for x in node.items])
    if isinstance(node, SMap):
        return SMap({k: _clone_node(v) for k, v in node.entries.items()})
    if isinstance(node, SDecl):
        return SDecl(node.anchor, _clone_node(node.inner))
    raise SemanticError(f"无法克隆节点 {type(node).__name__}")


# --------------------------------------------------------------------------- #
# 离线编辑包：以“来源声明 / 消费路径”为对象的显式操作序列
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Edit:
    """一条离线编辑操作。``path`` 是可作用于声明或使用路径的结构路径。"""

    op: str  # "set-shared" | "override" | "remove-override" | "alias" | "delete"
    path: tuple
    value: Any = None
    target: tuple | None = None

    def apply(self, doc: SemanticDocument) -> None:
        if self.op == "set-shared":
            doc.set_value(self.path, self.value)
        elif self.op == "override":
            doc.add_override(self.path, self.value)
        elif self.op == "remove-override":
            if self.target is None:
                raise SemanticError("remove-override 需要 target 声明路径")
            doc.remove_override(self.path, self.target)
        elif self.op == "alias":
            if self.target is None:
                raise SemanticError("alias 需要 target 声明路径")
            doc.set_node(self.path, SAlias(self.target))
        elif self.op == "delete":
            doc.remove(self.path)
        else:
            raise SemanticError(f"未知编辑操作 {self.op!r}")


@dataclass
class EditPack:
    """一厂的离线编辑包：共同版本 + 一串操作。

    操作对象要么是**共享声明的结构路径**（``set-shared``），要么是
    **某条使用路径**（``override`` / ``remove-override`` / ``alias``）。
    显式 ``override`` 即使与共享值相同，也把该路径从别名改为显式标量，
    因而在三路汇合中保留“以后独立”的意图。
    """

    name: str
    edits: list[Edit] = field(default_factory=list)

    def set_shared(self, path: tuple, value: Any) -> "EditPack":
        self.edits.append(Edit("set-shared", tuple(path), value=value))
        return self

    def add_override(self, path: tuple, value: Any) -> "EditPack":
        self.edits.append(Edit("override", tuple(path), value=value))
        return self

    def remove_override(self, path: tuple, target: tuple) -> "EditPack":
        self.edits.append(Edit("remove-override", tuple(path), target=tuple(target)))
        return self

    def alias(self, path: tuple, target: tuple) -> "EditPack":
        self.edits.append(Edit("alias", tuple(path), target=tuple(target)))
        return self

    def delete(self, path: tuple) -> "EditPack":
        self.edits.append(Edit("delete", tuple(path)))
        return self

    def apply(self, base: SemanticDocument) -> SemanticDocument:
        doc = base.clone()
        for edit in self.edits:
            edit.apply(doc)
        return doc


# --------------------------------------------------------------------------- #
# 提取：共享图 -> 语义树
# --------------------------------------------------------------------------- #
def _collect_anchor_paths(root: RawNode) -> dict[str, tuple]:
    paths: dict[str, tuple] = {}

    def walk(node: RawNode, path: tuple) -> None:
        if node.anchor is not None:
            if node.anchor in paths:
                raise SemanticError(f"锚点 {node.anchor!r} 重复出现")
            paths[node.anchor] = path
        if isinstance(node, RawMapping):
            for k, _km, v in node.entries:
                if k is MERGE_KEY:
                    raise SemanticError(
                        "离线汇合暂不支持 merge 键 <<；请改用锚点/别名表达共享"
                    )
                walk(v, path + (k,))
        elif isinstance(node, RawSequence):
            for i, v in enumerate(node.items):
                walk(v, path + (i,))

    walk(root, ())
    return paths


def _encode(node: RawNode, path: tuple, anchors: dict[str, tuple]) -> SNode:
    if isinstance(node, RawAlias):
        if node.target not in anchors:
            raise SemanticError(f"别名 *{node.target} 目标声明不存在")
        inner: SNode = SAlias(anchors[node.target])
    elif isinstance(node, RawScalar):
        inner = SScalar(node.value)
    elif isinstance(node, RawSequence):
        inner = SSeq([_encode(v, path + (i,), anchors) for i, v in enumerate(node.items)])
    elif isinstance(node, RawMapping):
        entries: dict[ConfigKey, SNode] = {}
        for k, _km, v in node.entries:
            if k is MERGE_KEY:
                raise SemanticError("离线汇合暂不支持 merge 键 <<")
            entries[k] = _encode(v, path + (k,), anchors)
        inner = SMap(entries)
    else:
        raise SemanticError(f"无法提取的原始节点 {type(node).__name__}")
    if node.anchor is not None:
        return SDecl(node.anchor, inner)
    return inner


def extract(parsed: ParsedDocument) -> SemanticDocument:
    """从单篇解析结果提取语义文档。"""
    anchors = _collect_anchor_paths(parsed.root)
    root = _encode(parsed.root, (), anchors)
    return SemanticDocument(root=root, source_text=parsed.source_text)


def from_yaml(text: str) -> SemanticDocument:
    docs = parse_text(text)
    if len(docs) != 1:
        raise SemanticError(f"离线汇合要求单文档，实际为 {len(docs)} 篇")
    return extract(docs[0])


# --------------------------------------------------------------------------- #
# 发射：语义树 -> 受限 YAML 文本
# --------------------------------------------------------------------------- #
_SAFE_SCALAR_RE = __import__("re").compile(r"[A-Za-z0-9_./:@+\-]+$")
_ANCHOR_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9_-]+$")


def _defined_paths(node: SNode, path: tuple) -> set[tuple]:
    out: set[tuple] = set()
    if isinstance(node, SDecl):
        out.add(path)
        out |= _defined_paths(node.inner, path)
    elif isinstance(node, SMap):
        for k, v in node.entries.items():
            out |= _defined_paths(v, path + (k,))
    elif isinstance(node, SSeq):
        for i, v in enumerate(node.items):
            out |= _defined_paths(v, path + (i,))
    return out


def _referenced_paths(node: SNode) -> set[tuple]:
    out: set[tuple] = set()
    if isinstance(node, SAlias):
        out.add(node.target)
    elif isinstance(node, SDecl):
        out |= _referenced_paths(node.inner)
    elif isinstance(node, SMap):
        for v in node.entries.values():
            out |= _referenced_paths(v)
    elif isinstance(node, SSeq):
        for v in node.items:
            out |= _referenced_paths(v)
    return out


def _order_mapping(mapping: SMap, path: tuple) -> SMap:
    """稳定拓扑排序：使被同级条目引用的声明先于引用者出现。

    YAML 合同要求锚点先于别名定义。三路汇合可能把“引用者”插到“声明者”前面
    （例如左支新增别名、右支新增声明），这里按同级依赖最小重排；无依赖时保持原序，
    因而不会改变 ``column_mapping`` 这类无别名映射的键序。
    """
    keys = list(mapping.entries.keys())
    if not any(_referenced_paths(v) for v in mapping.entries.values()):
        # 仍递归修复子映射
        new_entries = {
            k: _order_node(v, path + (k,)) for k, v in mapping.entries.items()
        }
        return SMap(new_entries)

    defined: dict[int, set[tuple]] = {}
    referenced: dict[int, set[tuple]] = {}
    for idx, v in enumerate(mapping.entries.values()):
        defined[idx] = _defined_paths(v, path + (keys[idx],))
        referenced[idx] = _referenced_paths(v)

    deps: dict[int, set[int]] = {i: set() for i in range(len(keys))}
    for i in range(len(keys)):
        for j in range(len(keys)):
            if i == j:
                continue
            # i 的引用若落在 j 定义的声明里，则 j 必须先于 i
            if referenced[i] & defined[j] and not (referenced[i] & defined[i]):
                deps[i].add(j)
    order = _stable_topo(keys, deps)
    new_entries = {}
    for idx in order:
        k = keys[idx]
        new_entries[k] = _order_node(mapping.entries[k], path + (k,))
    return SMap(new_entries)


def _order_node(node: SNode, path: tuple) -> SNode:
    if isinstance(node, SDecl):
        return SDecl(node.anchor, _order_node(node.inner, path))
    if isinstance(node, SMap):
        return _order_mapping(node, path)
    if isinstance(node, SSeq):
        return SSeq([_order_node(v, path + (i,)) for i, v in enumerate(node.items)])
    return node


def _stable_topo(keys: list, deps: dict[int, set[int]]) -> list[int]:
    ordered: list[int] = []
    done: set[int] = set()
    n = len(keys)
    remaining = set(range(n))
    while remaining:
        progressed = False
        for i in range(n):
            if i in remaining and deps[i] <= done:
                ordered.append(i)
                done.add(i)
                remaining.discard(i)
                progressed = True
        if not progressed:
            raise SemanticError("声明/别名顺序形成循环，无法发射为合法 YAML")
    return ordered


def _collect_decl_anchors(node: SNode, path: tuple, anchors: dict[tuple, str], used: set[str]) -> None:
    if isinstance(node, SDecl):
        base = node.anchor if _ANCHOR_NAME_RE.match(node.anchor) else "decl"
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        anchors[path] = name
        _collect_decl_anchors(node.inner, path, anchors, used)
    elif isinstance(node, SMap):
        for k, v in node.entries.items():
            _collect_decl_anchors(v, path + (k,), anchors, used)
    elif isinstance(node, SSeq):
        for i, v in enumerate(node.items):
            _collect_decl_anchors(v, path + (i,), anchors, used)


def _double_quote(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    if s and s not in ("true", "false", "null") and not _INT_RE.match(s) and _SAFE_SCALAR_RE.fullmatch(s):
        return s
    return _double_quote(s)


def _unwrap_decl(node: SNode) -> tuple[str | None, SNode]:
    anchor = None
    if isinstance(node, SDecl):
        anchor = node.anchor
        node = node.inner
    return anchor, node


def _emit_mapping(
    mapping: SMap, indent: int, lines: list[str], anchors: dict[tuple, str], path: tuple
) -> None:
    pad = " " * indent
    for key, value in mapping.entries.items():
        child_path = path + (key,)
        anchor, inner = _unwrap_decl(value)
        anchor_name = anchors.get(child_path)
        key_repr = _format_scalar(key)
        head = f"{pad}{key_repr}:"
        if isinstance(inner, SAlias):
            # 受限语法不允许 &anchor 与 *alias 组合；别名目标已规范化到真声明，
            # 因此别名节点自身不再声明锚点。
            lines.append(f"{head} {_alias_ref(inner, anchors)}")
            continue
        if anchor_name:
            head += f" &{anchor_name}"
        if isinstance(inner, SScalar):
            lines.append(f"{head} {_format_scalar(inner.value)}")
        elif isinstance(inner, SMap):
            lines.append(head)
            _emit_mapping(inner, indent + 2, lines, anchors, child_path)
        elif isinstance(inner, SSeq):
            lines.append(head)
            _emit_sequence(inner, indent + 2, lines, anchors, child_path)
        else:
            raise SemanticError(f"无法发射节点 {type(inner).__name__}")


def _emit_sequence(
    seq: SSeq, indent: int, lines: list[str], anchors: dict[tuple, str], path: tuple
) -> None:
    pad = " " * indent
    for i, item in enumerate(seq.items):
        child_path = path + (i,)
        anchor, inner = _unwrap_decl(item)
        anchor_name = anchors.get(child_path)
        head = f"{pad}-"
        if isinstance(inner, SAlias):
            lines.append(f"{head} {_alias_ref(inner, anchors)}")
            continue
        if anchor_name:
            head += f" &{anchor_name}"
        if isinstance(inner, SScalar):
            lines.append(f"{head} {_format_scalar(inner.value)}")
        elif isinstance(inner, SMap):
            lines.append(head)
            _emit_mapping(inner, indent + 2, lines, anchors, child_path)
        elif isinstance(inner, SSeq):
            lines.append(head)
            _emit_sequence(inner, indent + 2, lines, anchors, child_path)
        else:
            raise SemanticError(f"无法发射序列项 {type(inner).__name__}")


def _alias_ref(alias: SAlias, anchors: dict[tuple, str]) -> str:
    anchor = anchors.get(alias.target)
    if anchor is None:
        raise SemanticError(f"别名目标声明没有锚点: {alias.target}")
    return f"*{anchor}"


def emit_yaml(doc: SemanticDocument) -> str:
    ordered_root = _order_node(doc.root, ())
    anchors: dict[tuple, str] = {}
    _collect_decl_anchors(ordered_root, (), anchors, set())
    lines: list[str] = []
    anchor, inner = _unwrap_decl(ordered_root)
    if isinstance(inner, SMap) and anchor is None:
        _emit_mapping(inner, 0, lines, anchors, ())
    elif isinstance(inner, SMap):
        raise SemanticError("根节点声明无法无伤发射，请将声明放在映射条目下")
    elif isinstance(inner, SSeq):
        _emit_sequence(inner, 0, lines, anchors, ())
    elif isinstance(inner, SScalar):
        lines.append(_format_scalar(inner.value))
    elif isinstance(inner, SAlias):
        lines.append(_alias_ref(inner, anchors))
    else:
        raise SemanticError(f"不支持的根节点 {type(inner).__name__}")
    return "\n".join(lines) + "\n"
