"""命令行入口：目录可独立用于人工检查。

示例
====
::

    python -m confcat.cli publish ./data/catalog.db orders-product samples/orders_config.yaml
    python -m confcat.cli products ./data/catalog.db
    python -m confcat.cli show ./data/catalog.db orders-product doc-0
    python -m confcat.cli query ./data/catalog.db orders-product doc-0 ingest/column_mapping
    python -m confcat.cli verify ./data/catalog.db orders-product
    python -m confcat.cli tasks ./data/tasks.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .catalog import Catalog
from .codec import path_str
from .ingest import TaskRepository


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="confcat", description="受限 YAML 配置目录与消费仓")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pub = sub.add_parser("publish", help="发布 YAML 为新产品版本")
    p_pub.add_argument("db")
    p_pub.add_argument("product")
    p_pub.add_argument("yaml_path")
    p_pub.add_argument("--docs", nargs="*", help="多文档命名，顺序对应文档序号")

    p_prod = sub.add_parser("products", help="列出产品与版本")
    p_prod.add_argument("db")

    p_docs = sub.add_parser("docs", help="列出某产品文档")
    p_docs.add_argument("db")
    p_docs.add_argument("product")
    p_docs.add_argument("--version", type=int)

    p_show = sub.add_parser("show", help="导出文档有效配置（普通 JSON 视图）")
    p_show.add_argument("db")
    p_show.add_argument("product")
    p_show.add_argument("doc")
    p_show.add_argument("--version", type=int)

    p_q = sub.add_parser("query", help="查询路径有效值与来源证据")
    p_q.add_argument("db")
    p_q.add_argument("product")
    p_q.add_argument("doc")
    p_q.add_argument("path")
    p_q.add_argument("--version", type=int)

    p_v = sub.add_parser("verify", help="重算哈希并回查配置链完整性")
    p_v.add_argument("db")
    p_v.add_argument("product")
    p_v.add_argument("--version", type=int)

    p_run = sub.add_parser("run-task", help="创建接入任务（消费仓）")
    p_run.add_argument("catalog_db")
    p_run.add_argument("task_db")
    p_run.add_argument("task_id")
    p_run.add_argument("product")
    p_run.add_argument("doc")
    p_run.add_argument("csv_path")
    p_run.add_argument("--version", type=int)

    p_tasks = sub.add_parser("tasks", help="列出消费仓任务")
    p_tasks.add_argument("task_db")
    p_tasks.add_argument("catalog_db")

    p_rows = sub.add_parser("rows", help="逐行查看任务结果")
    p_rows.add_argument("task_db")
    p_rows.add_argument("catalog_db")
    p_rows.add_argument("task_id")

    # --- 三路汇合 ------------------------------------------------------ #
    p_merge = sub.add_parser("merge", help="三路语义汇合两包离线编辑（base/left/right YAML）")
    p_merge.add_argument("base")
    p_merge.add_argument("left")
    p_merge.add_argument("right")
    p_merge.add_argument("--out", help="写出汇合后 YAML（默认打印）")
    p_merge.add_argument("--allow-conflicts", action="store_true", help="存在冲突仍返回 0")
    p_merge.add_argument("--catalog-db", help="给出则发布汇合结果到该目录的 --product 产品")
    p_merge.add_argument("--product")

    # --- 对账仓 -------------------------------------------------------- #
    p_recon_init = sub.add_parser("recon-init", help="初始化对账仓（仅建表）")
    p_recon_init.add_argument("recon_db")
    p_recon_init.add_argument("catalog_db")
    p_recon_task = sub.add_parser("recon-task", help="导入某厂称重 CSV 为任务")
    p_recon_task.add_argument("recon_db")
    p_recon_task.add_argument("catalog_db")
    p_recon_task.add_argument("task_id")
    p_recon_task.add_argument("factory")
    p_recon_task.add_argument("product")
    p_recon_task.add_argument("doc")
    p_recon_task.add_argument("csv_path")
    p_recon_task.add_argument("--version", type=int)

    p_recon_pair = sub.add_parser("recon-pair", help="登记/激活一对任务")
    p_recon_pair.add_argument("recon_db")
    p_recon_pair.add_argument("catalog_db")
    p_recon_pair.add_argument("pair_id")
    p_recon_pair.add_argument("--register", nargs=2, metavar=("OUT", "RECV"))
    p_recon_pair.add_argument("--activate", type=int)

    p_recon_settle = sub.add_parser("recon-settle", help="按激活任务对签日结")
    p_recon_settle.add_argument("recon_db")
    p_recon_settle.add_argument("catalog_db")
    p_recon_settle.add_argument("settlement_id")
    p_recon_settle.add_argument("pair_id")

    p_recon_reopen = sub.add_parser("recon-reopen", help="按冻结来源重开复核签单")
    p_recon_reopen.add_argument("recon_db")
    p_recon_reopen.add_argument("catalog_db")
    p_recon_reopen.add_argument("slip_id")

    args = parser.parse_args(argv)

    if args.cmd == "merge":
        from .merge3 import merge_yaml

        base_text = Path(args.base).read_text(encoding="utf-8")
        left_text = Path(args.left).read_text(encoding="utf-8")
        right_text = Path(args.right).read_text(encoding="utf-8")
        result = merge_yaml(base_text, left_text, right_text)
        out_text = result.document.to_yaml()
        if args.out:
            Path(args.out).write_text(out_text, encoding="utf-8")
        else:
            print(out_text)
        if result.conflicts:
            for c in result.conflicts:
                print(f"[CONFLICT {c.kind}] /{'/'.join(map(str, c.path))}", file=sys.stderr)
        if args.catalog_db:
            if not args.product:
                parser.error("--catalog-db 需要 --product")
            with Catalog(args.catalog_db) as cat:
                info = cat.publish_merge(args.product, result)
            _print({"product": info.product, "version": info.version, "docs": list(info.docs)})
        return 0 if (not result.conflicts or args.allow_conflicts) else 2

    if args.cmd == "recon-init":
        from .recon import ReconciliationWarehouse

        with Catalog(args.catalog_db) as cat, ReconciliationWarehouse(args.recon_db, cat):
            pass
        _print({"recon_db": args.recon_db, "status": "initialized"})
        return 0

    if args.cmd.startswith("recon-") and args.cmd != "recon-init":
        from .recon import ReconciliationWarehouse

        with Catalog(args.catalog_db) as cat, ReconciliationWarehouse(args.recon_db, cat) as wh:
            if args.cmd == "recon-task":
                summary = wh.create_weigh_task(
                    args.task_id,
                    args.factory,
                    args.product,
                    args.doc,
                    args.csv_path,
                    version=args.version,
                )
                _print(summary.__dict__)
            elif args.cmd == "recon-pair":
                if args.register:
                    gen = wh.register_generation(args.pair_id, args.register[0], args.register[1])
                    _print({"pair_id": args.pair_id, "registered_generation": gen})
                if args.activate is not None:
                    wh.activate_generation(args.pair_id, args.activate)
                if args.register or args.activate is not None:
                    _print(wh.active_pair(args.pair_id).__dict__)
                else:
                    _print(wh.list_generations(args.pair_id))
            elif args.cmd == "recon-settle":
                res = wh.settle(args.settlement_id, args.pair_id)
                _print(
                    {
                        "settlement_id": res.settlement_id,
                        "status": res.status,
                        "slip_id": res.slip_id,
                        "reasons": res.reasons,
                    }
                )
            elif args.cmd == "recon-reopen":
                _print(wh.reopen_slip(args.slip_id))
        return 0

    if args.cmd == "publish":
        with Catalog(args.db) as cat:
            info = cat.publish_file(args.product, Path(args.yaml_path), doc_names=args.docs)
        _print(
            {
                "product": info.product,
                "version": info.version,
                "docs": list(info.docs),
                "digest": info.digest,
            }
        )
        return 0

    is_task_cmd = args.cmd in ("run-task", "tasks", "rows")
    catalog_db = args.catalog_db if is_task_cmd else args.db
    with Catalog(catalog_db) as cat:
        if args.cmd == "products":
            _print(
                [
                    {
                        "product": p.product,
                        "version": p.version,
                        "docs": list(p.docs),
                        "source_path": p.source_path,
                        "digest": p.digest,
                        "created_at": p.created_at,
                    }
                    for p in cat.list_products()
                ]
            )
        elif args.cmd == "docs":
            _print(cat.list_docs(args.product, args.version))
        elif args.cmd == "show":
            _print(cat.export_plain(args.product, args.doc, args.version))
        elif args.cmd == "query":
            r = cat.query(args.product, args.doc, args.path, args.version)
            _print(
                {
                    "product": r.product,
                    "version": r.version,
                    "doc": r.doc_name,
                    "path": path_str(r.path),
                    "value": r.value,
                    "provenance": r.provenance.as_dict() if r.provenance else None,
                }
            )
        elif args.cmd == "verify":
            _print(cat.verify(args.product, args.version))
        elif is_task_cmd:
            with TaskRepository(args.task_db, cat) as repo:
                if args.cmd == "run-task":
                    summary = repo.create_task(
                        args.task_id, args.product, args.doc, args.csv_path, args.version
                    )
                    _print(summary.__dict__ | {"partitions": summary.partitions})
                elif args.cmd == "tasks":
                    _print([s.__dict__ for s in repo.list_tasks()])
                else:
                    _print(
                        {
                            "summary": repo.get_task(args.task_id).__dict__,
                            "params": repo.get_params(args.task_id),
                            "lineage": repo.get_lineage(args.task_id),
                            "rows": repo.get_rows(args.task_id),
                        }
                    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
