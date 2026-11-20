"""对账仓：两厂按共同移交号核对发出/收到两腿重量，只在配齐且同号重量
全部相等时签日结；切换期读者只见同一对任务的完整版本。

关键合同
========
* **两腿配齐**：每个移交号必须同时有发出腿与收到腿；否则日结 ``pending``
  且原因 ``incomplete``（未配齐先待核）。
* **同号重量相等**：两腿各按其所绑工厂配置版本的系数把 CSV 实际重量换算后，
  所有同号换算重量逐一相等才可 ``signed``；否则 ``pending`` 原因 ``mismatch``。
* **任务版本冻结**：每张称重任务绑定 产品+版本+文档 快照，历史任务永久可读；
  发布新版本或切换任务对不影响已取得旧任务的读者（任何一支可继续读旧版）。
* **完整切版**：任务对以“代”（generation）为单位，一“代”同时登记发出任务与
  收到任务；激活切换是单事务 ``UPDATE``，读者一次 JOIN 取出同一代的两条任务，
  因此在切换期间只会看到完整的旧对或完整的新对，绝不混代。
* **签单冻结来源**：签名时把全部两腿读数（任务、版本、原重、系数、换算重量）
  冻结进签单；补交晚到的腿可另立新日结，旧签单仍可按其冻结来源重开复核。

与既有单版本接入的关系
====================
本模块**复用**现有 :mod:`confcat.csv_pipeline` 与目录版本快照，不改变
:class:`confcat.ingest.TaskRepository` 的单版本接入行为；它只是在现有能力之上
叠加对账语义。读者可继续按 ``task_id`` 读取任一代的旧任务结果。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .catalog import Catalog
from .csv_pipeline import MissingRule, normalize
from .errors import CatalogError, ConfCatError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS weigh_task (
    task_id      TEXT PRIMARY KEY,
    factory      TEXT NOT NULL,
    product      TEXT NOT NULL,
    version      INTEGER NOT NULL,
    doc_name     TEXT NOT NULL,
    csv_path     TEXT NOT NULL,
    rate_path    TEXT NOT NULL,
    mapping_path TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weigh_reading (
    task_id     TEXT NOT NULL,
    handover_id TEXT NOT NULL,
    gross       TEXT NOT NULL,
    rate        TEXT NOT NULL,
    converted   TEXT NOT NULL,
    PRIMARY KEY (task_id, handover_id),
    FOREIGN KEY (task_id) REFERENCES weigh_task(task_id)
);
CREATE TABLE IF NOT EXISTS pair_generation (
    pair_id       TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    outbound_task TEXT NOT NULL,
    received_task TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (pair_id, generation)
);
CREATE TABLE IF NOT EXISTS pair_active (
    pair_id    TEXT PRIMARY KEY,
    generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement (
    settlement_id TEXT PRIMARY KEY,
    pair_id       TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    outbound_task TEXT NOT NULL,
    received_task TEXT NOT NULL,
    status        TEXT NOT NULL,
    reasons_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_slip (
    slip_id       TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL,
    pair_id       TEXT NOT NULL,
    frozen_json   TEXT NOT NULL,
    signed_at     TEXT NOT NULL
);
"""


class ReconError(ConfCatError):
    """对账仓操作错误（任务/移交号/签单不存在、CSV 合同违规等）。"""


# 称重/对账配置的固定读取路径（以各厂子树为根）
PATH_RATE_SUFFIX = ("rate",)
PATH_WEIGH_MAPPING_SUFFIX = ("weigh", "column_mapping")
REQUIRED_TARGETS = ("handover_id", "weight")


@dataclass
class WeighTaskSummary:
    task_id: str
    factory: str
    product: str
    version: int
    doc_name: str
    csv_path: str
    readings: int
    status: str
    created_at: str


@dataclass
class PairView:
    """读者视角的“一对完整任务”：同一代的两条任务。"""

    pair_id: str
    generation: int
    outbound_task: str
    received_task: str
    outbound_version: int
    received_version: int


@dataclass
class SettlementResult:
    settlement_id: str
    pair_id: str
    generation: int
    outbound_task: str
    received_task: str
    status: str  # "signed" | "pending"
    reasons: list[dict] = field(default_factory=list)
    slip_id: str | None = None
    legs: dict[str, dict] = field(default_factory=dict)

    @property
    def signed(self) -> bool:
        return self.status == "signed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dec(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ReconError(f"无法解析为数值: {value!r}") from None


def _dec_str(d: Decimal) -> str:
    return format(d.normalize(), "f")


class ReconciliationWarehouse:
    """两厂称重任务与日结对账。线程模型：一连接 + 锁；可多实例共享库。"""

    def __init__(self, db_path: str | Path, catalog: Catalog):
        self.db_path = str(db_path)
        self.catalog = catalog
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> "ReconciliationWarehouse":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 称重任务
    # ------------------------------------------------------------------ #
    def create_weigh_task(
        self,
        task_id: str,
        factory: str,
        product: str,
        doc_name: str,
        csv_path: str | Path,
        *,
        rate_path: tuple | None = None,
        mapping_path: tuple | None = None,
        version: int | None = None,
    ) -> WeighTaskSummary:
        """导入一个工厂的本地 CSV，按绑定版本系数换算实际重量。

        ``rate_path`` / ``mapping_path`` 缺省时按 ``(factory, 'rate')`` 与
        ``(factory, 'weigh', 'column_mapping')`` 取；任务冻结到当时的目录版本。
        """
        rate_path = tuple(rate_path) if rate_path else (factory,) + PATH_RATE_SUFFIX
        mapping_path = (
            tuple(mapping_path) if mapping_path else (factory,) + PATH_WEIGH_MAPPING_SUFFIX
        )
        with self._lock:
            if self.conn.execute(
                "SELECT 1 FROM weigh_task WHERE task_id = ?", (task_id,)
            ).fetchone():
                raise ReconError(f"称重任务 {task_id!r} 已存在")

            resolved_version = self.catalog._resolve_version(product, version)
            rate = _dec(
                self.catalog.query_typed(product, doc_name, rate_path, int, resolved_version).value
            )

            from .model import MatMapping, MatScalar

            mapping_node = self.catalog.load_document(product, doc_name, resolved_version).find(
                mapping_path
            )
            if not isinstance(mapping_node, MatMapping):
                raise ReconError(f"配置路径 {'/'.join(map(str, mapping_path))} 不是映射")
            column_mapping: dict[str, str] = {}
            for key, child in mapping_node.entries.items():
                if not isinstance(key, str) or not isinstance(child, MatScalar) or not isinstance(
                    child.value, str
                ):
                    raise ReconError("称重列映射必须为 字符串->字符串")
                column_mapping[key] = child.value
            missing_targets = [t for t in REQUIRED_TARGETS if t not in column_mapping]
            if missing_targets:
                raise ReconError(f"称重列映射缺少目标列 {missing_targets}")

            table = normalize(
                csv_path=csv_path,
                column_mapping=column_mapping,
                mapping_prov={},
                missing=MissingRule(),
            )

            readings: dict[str, tuple[Decimal, Decimal]] = {}
            for row_no, row in enumerate(table.rows):
                hid = row["handover_id"]
                if hid == "":
                    raise ReconError(f"第 {row_no + 2} 行移交号为空")
                if hid in readings:
                    raise ReconError(f"同一任务内移交号重复: {hid!r}")
                gross = _dec(row["weight"])
                readings[hid] = (gross, gross * rate)

            created_at = _now()
            try:
                self.conn.execute(
                    "INSERT INTO weigh_task(task_id, factory, product, version, doc_name,"
                    " csv_path, rate_path, mapping_path, status, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        factory,
                        product,
                        resolved_version,
                        doc_name,
                        str(csv_path),
                        "/" + "/".join(map(str, rate_path)),
                        "/" + "/".join(map(str, mapping_path)),
                        "imported",
                        created_at,
                    ),
                )
                self.conn.executemany(
                    "INSERT INTO weigh_reading(task_id, handover_id, gross, rate, converted)"
                    " VALUES(?, ?, ?, ?, ?)",
                    [
                        (task_id, hid, _dec_str(gross), _dec_str(rate), _dec_str(conv))
                        for hid, (gross, conv) in readings.items()
                    ],
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

            return WeighTaskSummary(
                task_id,
                factory,
                product,
                resolved_version,
                doc_name,
                str(csv_path),
                len(readings),
                "imported",
                created_at,
            )

    def get_task(self, task_id: str) -> WeighTaskSummary:
        row = self.conn.execute(
            "SELECT * FROM weigh_task WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise ReconError(f"称重任务 {task_id!r} 不存在")
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM weigh_reading WHERE task_id = ?", (task_id,)
        ).fetchone()["c"]
        return WeighTaskSummary(
            row["task_id"],
            row["factory"],
            row["product"],
            row["version"],
            row["doc_name"],
            row["csv_path"],
            count,
            row["status"],
            row["created_at"],
        )

    def get_readings(self, task_id: str) -> dict[str, dict]:
        """按移交号读取该任务换算结果；旧任务永久可读（版本冻结）。"""
        self.get_task(task_id)
        rows = self.conn.execute(
            "SELECT handover_id, gross, rate, converted FROM weigh_reading"
            " WHERE task_id = ? ORDER BY handover_id",
            (task_id,),
        ).fetchall()
        return {
            r["handover_id"]: {
                "gross": r["gross"],
                "rate": r["rate"],
                "converted": r["converted"],
            }
            for r in rows
        }

    # ------------------------------------------------------------------ #
    # 任务对：代与原子切换
    # ------------------------------------------------------------------ #
    def register_generation(
        self, pair_id: str, outbound_task: str, received_task: str
    ) -> int:
        """登记一代（两条任务成对），返回代数；不自动激活。

        一代的两条任务必须来自不同厂、同一产品，否则它们不可比，拒绝登记。
        """
        if outbound_task == received_task:
            raise ReconError("一对任务的发出腿与收到腿不能是同一任务")
        out = self.get_task(outbound_task)
        recv = self.get_task(received_task)
        if out.factory == recv.factory:
            raise ReconError(
                f"任务对 {pair_id!r} 两腿来自同一厂 {out.factory!r}，应为两厂各一条"
            )
        if out.product != recv.product:
            raise ReconError(
                f"任务对 {pair_id!r} 两腿产品不一致: {out.product!r} vs {recv.product!r}"
            )
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(generation), 0) AS g FROM pair_generation WHERE pair_id = ?",
                (pair_id,),
            ).fetchone()
            generation = int(row["g"]) + 1
            self.conn.execute(
                "INSERT INTO pair_generation(pair_id, generation, outbound_task, received_task,"
                " created_at) VALUES(?, ?, ?, ?, ?)",
                (pair_id, generation, outbound_task, received_task, _now()),
            )
            self.conn.commit()
            return generation

    def activate_generation(self, pair_id: str, generation: int) -> None:
        """单事务切换读者可见代；切换前后读者都只能看到同一代的完整任务对。"""
        with self._lock:
            exists = self.conn.execute(
                "SELECT 1 FROM pair_generation WHERE pair_id = ? AND generation = ?",
                (pair_id, generation),
            ).fetchone()
            if not exists:
                raise ReconError(f"任务对 {pair_id!r} 不存在第 {generation} 代")
            self.conn.execute(
                "INSERT INTO pair_active(pair_id, generation) VALUES(?, ?)"
                " ON CONFLICT(pair_id) DO UPDATE SET generation = excluded.generation",
                (pair_id, generation),
            )
            self.conn.commit()

    def active_pair(self, pair_id: str) -> PairView:
        """一次 JOIN 读取激活代与其两条任务，保证读者看到完整一对。"""
        row = self.conn.execute(
            "SELECT a.generation AS generation, g.outbound_task AS outbound_task,"
            " g.received_task AS received_task,"
            " ot.version AS outbound_version, rt.version AS received_version"
            " FROM pair_active a"
            " JOIN pair_generation g ON g.pair_id = a.pair_id AND g.generation = a.generation"
            " JOIN weigh_task ot ON ot.task_id = g.outbound_task"
            " JOIN weigh_task rt ON rt.task_id = g.received_task"
            " WHERE a.pair_id = ?",
            (pair_id,),
        ).fetchone()
        if row is None:
            raise ReconError(f"任务对 {pair_id!r} 尚未激活任何一代")
        return PairView(
            pair_id=pair_id,
            generation=row["generation"],
            outbound_task=row["outbound_task"],
            received_task=row["received_task"],
            outbound_version=row["outbound_version"],
            received_version=row["received_version"],
        )

    def list_generations(self, pair_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT generation, outbound_task, received_task, created_at"
            " FROM pair_generation WHERE pair_id = ? ORDER BY generation",
            (pair_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_settlements(self, pair_id: str | None = None) -> list[dict]:
        if pair_id is None:
            rows = self.conn.execute(
                "SELECT settlement_id, pair_id, generation, status, created_at"
                " FROM settlement ORDER BY created_at"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT settlement_id, pair_id, generation, status, created_at"
                " FROM settlement WHERE pair_id = ? ORDER BY created_at",
                (pair_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 日结对账
    # ------------------------------------------------------------------ #
    def settle(self, settlement_id: str, pair_id: str) -> SettlementResult:
        """按激活的任务对做一次日结对账。

        未配齐 -> pending(incomplete)；任一同号换算重量不等 -> pending(mismatch)；
        全部配齐且同号相等 -> signed 并冻结来源生成签单。
        """
        view = self.active_pair(pair_id)
        out = self.get_readings(view.outbound_task)
        rec = self.get_readings(view.received_task)
        legs = self._compare(out, rec)

        reasons: list[dict] = []
        if not legs:
            reasons.append({"kind": "empty", "handovers": []})
        missing_received = sorted(set(out) - set(rec))
        missing_outbound = sorted(set(rec) - set(out))
        if missing_received or missing_outbound:
            reasons.append(
                {
                    "kind": "incomplete",
                    "missing_received": missing_received,
                    "missing_outbound": missing_outbound,
                }
            )
        mismatches = [
            {
                "handover_id": hid,
                "outbound": legs[hid]["outbound"],
                "received": legs[hid]["received"],
            }
            for hid in sorted(legs)
            if "outbound" in legs[hid]
            and "received" in legs[hid]
            and _dec(legs[hid]["outbound"]) != _dec(legs[hid]["received"])
        ]
        if mismatches:
            reasons.append({"kind": "mismatch", "handovers": mismatches})

        status = "pending" if reasons else "signed"
        created_at = _now()
        with self._lock:
            if self.conn.execute(
                "SELECT 1 FROM settlement WHERE settlement_id = ?", (settlement_id,)
            ).fetchone():
                raise ReconError(f"日结 {settlement_id!r} 已存在")
            try:
                self.conn.execute(
                    "INSERT INTO settlement(settlement_id, pair_id, generation, outbound_task,"
                    " received_task, status, reasons_json, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        settlement_id,
                        pair_id,
                        view.generation,
                        view.outbound_task,
                        view.received_task,
                        status,
                        json.dumps(reasons, ensure_ascii=False),
                        created_at,
                    ),
                )
                slip_id = None
                if status == "signed":
                    slip_id = f"{settlement_id}-slip"
                    frozen = self._freeze(view, legs)
                    self.conn.execute(
                        "INSERT INTO settlement_slip(slip_id, settlement_id, pair_id,"
                        " frozen_json, signed_at) VALUES(?, ?, ?, ?, ?)",
                        (
                            slip_id,
                            settlement_id,
                            pair_id,
                            json.dumps(frozen, ensure_ascii=False),
                            created_at,
                        ),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

        return SettlementResult(
            settlement_id,
            pair_id,
            view.generation,
            view.outbound_task,
            view.received_task,
            status,
            reasons,
            slip_id,
            legs,
        )

    @staticmethod
    def _compare(out: dict[str, dict], rec: dict[str, dict]) -> dict[str, dict]:
        legs: dict[str, dict] = {}
        for hid, reading in out.items():
            legs.setdefault(hid, {})["outbound"] = reading["converted"]
        for hid, reading in rec.items():
            legs.setdefault(hid, {})["received"] = reading["converted"]
        return legs

    def _freeze(self, view: PairView, legs: dict[str, dict]) -> dict:
        out_meta = self._task_meta(view.outbound_task)
        rec_meta = self._task_meta(view.received_task)
        return {
            "pair_id": view.pair_id,
            "generation": view.generation,
            "outbound_task": out_meta,
            "received_task": rec_meta,
            "handovers": {
                hid: {
                    "outbound": legs[hid].get("outbound"),
                    "received": legs[hid].get("received"),
                }
                for hid in sorted(legs)
            },
        }

    def _task_meta(self, task_id: str) -> dict:
        row = self.conn.execute(
            "SELECT task_id, factory, product, version, doc_name, csv_path FROM weigh_task"
            " WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        readings = self.get_readings(task_id)
        return {
            "task_id": row["task_id"],
            "factory": row["factory"],
            "product": row["product"],
            "version": row["version"],
            "doc_name": row["doc_name"],
            "csv_path": row["csv_path"],
            "readings": readings,
        }

    def get_settlement(self, settlement_id: str) -> SettlementResult:
        row = self.conn.execute(
            "SELECT * FROM settlement WHERE settlement_id = ?", (settlement_id,)
        ).fetchone()
        if row is None:
            raise ReconError(f"日结 {settlement_id!r} 不存在")
        slip = self.conn.execute(
            "SELECT slip_id FROM settlement_slip WHERE settlement_id = ?", (settlement_id,)
        ).fetchone()
        out = self.get_readings(row["outbound_task"])
        rec = self.get_readings(row["received_task"])
        return SettlementResult(
            row["settlement_id"],
            row["pair_id"],
            row["generation"],
            row["outbound_task"],
            row["received_task"],
            row["status"],
            json.loads(row["reasons_json"]),
            slip["slip_id"] if slip else None,
            self._compare(out, rec),
        )

    def reopen_slip(self, slip_id: str) -> dict:
        """按签单冻结来源重开复核：不读实时任务，故后续补交/切版不影响旧签单。"""
        row = self.conn.execute(
            "SELECT * FROM settlement_slip WHERE slip_id = ?", (slip_id,)
        ).fetchone()
        if row is None:
            raise ReconError(f"签单 {slip_id!r} 不存在")
        frozen = json.loads(row["frozen_json"])
        handovers = frozen["handovers"]
        all_equal = all(
            leg.get("outbound") is not None
            and leg.get("received") is not None
            and _dec(leg["outbound"]) == _dec(leg["received"])
            for leg in handovers.values()
        )
        return {
            "slip_id": slip_id,
            "pair_id": frozen["pair_id"],
            "generation": frozen["generation"],
            "signed_at": row["signed_at"],
            "frozen": frozen,
            "recheck": "consistent" if all_equal else "changed",
        }
