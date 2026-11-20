"""售后：换件闭合、借件归还、当前责任范围。

换件闭合要求（口径原文）：原件卸下、新件装入，且各自位置一致。
本模块只把已有证据拼起来，不臆断缺失的扫描。
"""
from __future__ import annotations

import datetime as _dt

from . import query
from . import reconcile
from .load import BUSINESS_TZ

_TZ = BUSINESS_TZ


def _moment(value):
    return query.parse_moment(value)


def swap_candidates(conn, kit_key: str, session_id: str) -> list[dict]:
    """沿装配经历找出“旧件卸下、新件装入”的候选换件对。

    候选只依据装配区间；是否闭合还要看扫描证据。结束为空表示仍在套内。
    """
    rows = conn.execute(
        "SELECT assembly_id, component_key, start, \"end\", basis FROM fact_assembly "
        "WHERE session_id = ? AND kit_key = ? ORDER BY start, assembly_id",
        [session_id, kit_key],
    ).fetchall()
    assemblies = [
        {"assembly_id": r[0], "component_key": r[1], "start": r[2], "end": r[3], "basis": r[4]}
        for r in rows
    ]
    closed = [a for a in assemblies if a["end"] is not None]
    opened = [a for a in assemblies if a["start"] is not None]
    candidates = []
    for old in closed:
        for new in opened:
            if new["component_key"] == old["component_key"]:
                continue
            gap = (new["start"] - old["end"]).total_seconds()
            if 0 <= gap <= 7 * 24 * 3600:  # 同一维修周期的默认观察窗口
                candidates.append({
                    "kit_key": kit_key,
                    "removed": old,
                    "installed": new,
                    "gap_hours": gap / 3600,
                })
    return candidates


def swap_closure(conn, kit_key: str, as_of=None, session_id: str | None = None) -> dict:
    """检查每个换件候选是否闭合。

    闭合判据：
    - 原件在卸下时点之后有“离开客户位置”的扫描；
    - 新件在装入时点之前有“到达客户位置”的扫描；
    - 原件离场后回到仓库（store）的扫描存在，才算回库闭合。
    缺失回库扫描保持在途或未知，不写成已返回。
    """
    as_of = _moment(as_of)
    session_id = query.resolve_session(conn, session_id)
    results = []
    for candidate in swap_candidates(conn, kit_key, session_id):
        removed, installed = candidate["removed"], candidate["installed"]
        removed_moves = _moves_between(conn, session_id, removed["component_key"],
                                       removed["end"], as_of)
        installed_moves = _moves_between(conn, session_id, installed["component_key"],
                                         _dt.datetime(1900, 1, 1), installed["start"])
        left_customer = [
            m for m in removed_moves
            if m["from_location"] not in (None, "store") and m["from_location"] != m["to_location"]
        ]
        arrived_customer = [
            m for m in installed_moves
            if m["to_location"] not in (None, "store") and m["from_location"] != m["to_location"]
        ]
        returned_to_store = [m for m in removed_moves if m["to_location"] == "store"]
        # 原件这次离场后，必须有一次回库扫描才算回库；不能借用更早的历史回库记录。
        # “C1 → store”这一条扫描同时是离场和回库证据。
        returned_after_leaving = False
        if left_customer:
            first_leave = left_customer[0]["at"]
            returned_after_leaving = any(
                m["to_location"] == "store" and m["at"] >= first_leave for m in removed_moves
            )
        status = "closed" if (left_customer and arrived_customer and returned_after_leaving) else "open"
        if not left_customer:
            reason = "原件缺少离开客户的扫描，按在途或未知处理"
        elif not arrived_customer:
            reason = "新件缺少到达客户的扫描，按在途或未知处理"
        elif not returned_after_leaving:
            reason = "原件缺少回库扫描，按在途或未知处理"
        else:
            reason = "原件卸下、回库与新件装入均有位置证据"
        results.append({
            "kit_key": kit_key,
            "removed": _assembly_view(removed),
            "installed": _assembly_view(installed),
            "gap_hours": candidate["gap_hours"],
            "status": status,
            "reason": reason,
            "evidence": {
                "removed_movements": removed_moves,
                "installed_movements": installed_moves,
                "removed_returned_to_store": [m["scan_id"] for m in returned_to_store],
            },
        })
    return {"kit_key": kit_key, "as_of": as_of.isoformat(), "swaps": results}


def _assembly_view(item):
    return {
        "assembly_id": item["assembly_id"],
        "component_key": item["component_key"],
        "start": item["start"].isoformat(),
        "end": item["end"].isoformat() if item["end"] else None,
        "basis": item["basis"],
    }


def _moves_between(conn, session_id, component_key, start, end):
    rows = conn.execute(
        "SELECT scan_id, from_location, to_location, \"at\", shipment_id "
        "FROM fact_movement WHERE session_id = ? AND component_key = ? "
        "AND \"at\" >= ? AND \"at\" <= ? ORDER BY \"at\", scan_id",
        [session_id, component_key, start, end],
    ).fetchall()
    return [
        {"scan_id": r[0], "from_location": r[1], "to_location": r[2],
         "at": r[3].isoformat(), "shipment_id": r[4]}
        for r in rows
    ]


def loan_returns(conn, as_of=None, session_id: str | None = None) -> list[dict]:
    """借件是否已归还；未归还的仍算在客户侧临时使用权。

    归还要求装配已结束、结束时刻早于查询时点，且该件此后确有回库位置证据；
    只有装配结束而没有回库扫描，保持“在途或未知”，不写成已返回。
    """
    as_of = _moment(as_of)
    session_id = query.resolve_session(conn, session_id)
    rows = conn.execute(
        "SELECT assembly_id, kit_key, component_key, start, \"end\" FROM fact_assembly "
        "WHERE session_id = ? AND basis = 'loan' ORDER BY kit_key, start",
        [session_id],
    ).fetchall()
    result = []
    for assembly_id, kit_key, component_key, start, end in rows:
        if start > as_of:
            continue
        position = query.position_at(conn, component_key, as_of, session_id)
        ended = bool(end and end <= as_of)
        back_in_store = False
        if ended:
            back_in_store = conn.execute(
                "SELECT 1 FROM fact_movement WHERE session_id = ? AND component_key = ? "
                "AND \"at\" >= ? AND \"at\" <= ? AND to_location = 'store' LIMIT 1",
                [session_id, component_key, end, as_of],
            ).fetchone() is not None
        returned = ended and back_in_store
        if returned:
            state = "returned"
        elif ended:
            state = "in_transit_or_unknown"
        else:
            state = "on_loan"
        result.append({
            "assembly_id": assembly_id,
            "kit_key": kit_key,
            "component_key": component_key,
            "loan_start": start.isoformat(),
            "loan_end": end.isoformat() if end else None,
            "current_location": position["location"] if position else None,
            "state": state,
            "returned": returned,
            "note": "借入件不增加销量；归还只影响可用性，不冲销原销售。",
        })
    return result


def service_exposure(conn, customer: str | None = None, as_of=None,
                     session_id: str | None = None) -> dict:
    """售后责任视图：客户手里的套、当前服务状态、中断经历、未闭合换件与未归还借件。"""
    as_of = _moment(as_of)
    session_id = query.resolve_session(conn, session_id)
    if customer:
        customers = [customer]
    else:
        customers = [r[0] for r in conn.execute(
            "SELECT DISTINCT customer FROM fact_delivery WHERE session_id = ? AND \"at\" <= ? "
            "ORDER BY customer", [session_id, as_of]
        ).fetchall()]
    kits = []
    for name in customers:
        availability = reconcile.availability(conn, name, as_of, session_id)
        for row in availability["kits"]:
            deliveries = [d["shipment_id"] for d in
                          query.deliveries_for_customer(conn, name, as_of, session_id)
                          if d["kit_key"] == row["kit_key"]]
            first = conn.execute(
                "SELECT min(\"at\") FROM fact_delivery "
                "WHERE session_id = ? AND customer = ? AND kit_key = ? AND \"at\" <= ?",
                [session_id, name, row["kit_key"], as_of],
            ).fetchone()[0]
            intervals = reconcile.interruption_intervals(conn, name, row["kit_key"], first,
                                                         as_of, session_id)
            closure = swap_closure(conn, row["kit_key"], as_of, session_id)
            loans = [item for item in loan_returns(conn, as_of, session_id)
                     if item["kit_key"] == row["kit_key"]]
            kits.append({
                "customer": name,
                "kit_key": row["kit_key"],
                "deliveries": deliveries,
                "delivered_sets": row["delivered_sets"],
                "available_sets": row["available_sets"],
                "service_interrupted": row["service_interrupted"],
                "interruptions": intervals,
                "open_swaps": [s for s in closure["swaps"] if s["status"] != "closed"],
                "loans": loans,
            })
    return {"as_of": as_of.isoformat(), "session_id": session_id,
            "customers": customers, "kits": kits}
