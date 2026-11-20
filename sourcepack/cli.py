"""命令行入口：装载、对账、核件、批次追查、售后责任、历史比较。

用法示例：

    python3 -m sourcepack load --db build/wh.duckdb --root . --label 初始资料
    python3 -m sourcepack availability --db build/wh.duckdb --customer C1
    python3 -m sourcepack kit --db build/wh.duckdb --kit A:K1 --as-of 2026-10-15T00:00:00+08:00
    python3 -m sourcepack component --db build/wh.duckdb --key A:2
    python3 -m sourcepack lot --db build/wh.duckdb --lot L2
    python3 -m sourcepack service --db build/wh.duckdb --customer C1
    python3 -m sourcepack reconcile --db build/wh.duckdb --customer C1 --note 首次对账
    python3 -m sourcepack revisions --db build/wh.duckdb --customer C1
    python3 -m sourcepack compare --db build/wh.duckdb --left 1 --right 2
    python3 -m sourcepack conflicts --db build/wh.duckdb
    python3 -m sourcepack sessions --db build/wh.duckdb
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import load, query, reconcile, service


def _dump(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _now(value):
    return query.parse_moment(value)


def cmd_load(args):
    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    fresh = not db.exists()
    conn = load.open_database(db) if fresh else load.open_existing(db)
    now = _now(args.now)
    report = load.receive(conn, Path(args.root), args.label, now)
    if report.get("status") != "received":
        report["conflicts"] = reconcile.rebuild_conflicts(
            conn, report["session_id"], now)["review_count"]
        _dump(report)
        conn.close()
        return
    snapshot = reconcile.rebuild_availability(conn, report["session_id"], now)
    report["availability"] = {
        "customers": snapshot["customers"],
        "kits": [
            {k: r[k] for k in ("customer", "kit_key", "delivered_sets", "available_sets",
                               "service_interrupted")}
            for r in snapshot["kits"]
        ],
    }
    report["conflicts"] = reconcile.rebuild_conflicts(conn, report["session_id"], now)["review_count"]
    _dump(report)
    conn.close()


def cmd_availability(args):
    conn = load.open_existing(args.db)
    _dump(reconcile.availability(conn, args.customer, _now(args.as_of), args.session))
    conn.close()


def cmd_kit(args):
    conn = load.open_existing(args.db)
    _dump(query.kit_trail(conn, args.kit, _now(args.as_of), args.session))
    conn.close()


def cmd_component(args):
    conn = load.open_existing(args.db)
    _dump(query.component_trail(conn, args.key, _now(args.as_of), args.session))
    conn.close()


def cmd_lot(args):
    conn = load.open_existing(args.db)
    _dump(query.lot_affected(conn, args.lot, _now(args.as_of), args.session))
    conn.close()


def cmd_service(args):
    conn = load.open_existing(args.db)
    _dump(service.service_exposure(conn, args.customer, _now(args.as_of), args.session))
    conn.close()


def cmd_reconcile(args):
    conn = load.open_existing(args.db)
    _dump(reconcile.freeze_reconcile(conn, args.customer, _now(args.as_of), args.note, args.session))
    conn.close()


def cmd_revisions(args):
    conn = load.open_existing(args.db)
    _dump(reconcile.revisions(conn, args.customer, args.kind))
    conn.close()


def cmd_compare(args):
    conn = load.open_existing(args.db)
    _dump(reconcile.compare_revisions(conn, args.left, args.right))
    conn.close()


def cmd_conflicts(args):
    conn = load.open_existing(args.db)
    _dump(reconcile.rebuild_conflicts(conn, args.session, _now(args.as_of)))
    conn.close()


def cmd_sessions(args):
    conn = load.open_existing(args.db)
    rows = conn.execute(
        "SELECT session_id, created_at, label, catalog_sha256, previous_session, sealed_at "
        "FROM meta_session ORDER BY created_at, session_id"
    ).fetchall()
    _dump([
        {"session_id": r[0], "created_at": r[1].isoformat(), "label": r[2],
         "catalog_sha256": r[3], "previous_session": r[4],
         "sealed_at": r[5].isoformat() if r[5] else None}
        for r in rows
    ])
    conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sourcepack", description="器械序列号仓")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("load", help="装载资料并生成会话")
    p.add_argument("--db", required=True)
    p.add_argument("--root", default=".")
    p.add_argument("--label", default="装载")
    p.add_argument("--now", default=None, help="接收时点，缺省为当前时刻（+08:00）")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("availability", help="客户已交付/当前可用套数")
    p.add_argument("--db", required=True)
    p.add_argument("--customer", required=True)
    p.add_argument("--as-of", default=None)
    p.add_argument("--session", default=None)
    p.set_defaults(func=cmd_availability)

    p = sub.add_parser("kit", help="整套当时组成与在套件的当前位置/状态")
    p.add_argument("--db", required=True)
    p.add_argument("--kit", required=True)
    p.add_argument("--as-of", default=None)
    p.add_argument("--session", default=None)
    p.set_defaults(func=cmd_kit)

    p = sub.add_parser("component", help="序列号明细：位置、装配与交付证据")
    p.add_argument("--db", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--as-of", default=None)
    p.add_argument("--session", default=None)
    p.set_defaults(func=cmd_component)

    p = sub.add_parser("lot", help="按制造批查当前实际受影响的套")
    p.add_argument("--db", required=True)
    p.add_argument("--lot", required=True)
    p.add_argument("--as-of", default=None)
    p.add_argument("--session", default=None)
    p.set_defaults(func=cmd_lot)

    p = sub.add_parser("service", help="售后责任：中断经历、未闭合换件、借件")
    p.add_argument("--db", required=True)
    p.add_argument("--customer", default=None)
    p.add_argument("--as-of", default=None)
    p.add_argument("--session", default=None)
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("reconcile", help="确认并冻结一次客户对账")
    p.add_argument("--db", required=True)
    p.add_argument("--customer", required=True)
    p.add_argument("--as-of", default=None)
    p.add_argument("--note", default=None)
    p.add_argument("--session", default=None)
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("revisions", help="列出对账修订")
    p.add_argument("--db", required=True)
    p.add_argument("--customer", default=None)
    p.add_argument("--kind", default=None)
    p.set_defaults(func=cmd_revisions)

    p = sub.add_parser("compare", help="比较两次修订的解释")
    p.add_argument("--db", required=True)
    p.add_argument("--left", type=int, required=True)
    p.add_argument("--right", type=int, required=True)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("conflicts", help="待核范围复核")
    p.add_argument("--db", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--as-of", default=None)
    p.set_defaults(func=cmd_conflicts)

    p = sub.add_parser("sessions", help="列出装载会话")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_sessions)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
