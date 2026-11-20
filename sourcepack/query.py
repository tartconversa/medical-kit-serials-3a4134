"""查询：沿仓表从序列号核到整套，再从整套查回当时真实在客户处的各件。

时间口径
--------
每个查询都明确两个维度：

- session：使用哪个会话“已知”的事实版本（不指定则用最新会话）；
- as_of：业务时点，历史截点不使用较晚才知的值。

“新查询不能拼接新组成与旧库存”在这里落实为：组成、装配、位置、质量
都在同一个会话内按 as_of 选取，不会把新接收的组成套到旧库存上。
"""
from __future__ import annotations

import datetime as _dt
import json

from .load import BUSINESS_TZ

from . import load

_AT = '"at"'
SCOPE = load.SESSION_UP_TO


def parse_moment(value=None) -> _dt.datetime:
    if value is None:
        return _dt.datetime.now(BUSINESS_TZ).replace(tzinfo=None)
    if isinstance(value, _dt.datetime):
        stamp = value
    else:
        stamp = _dt.datetime.fromisoformat(value)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(BUSINESS_TZ).replace(tzinfo=None)
    return stamp


def resolve_session(conn, session_id: str | None) -> str:
    if session_id:
        return session_id
    current = load.latest_session(conn)
    if not current:
        raise ValueError("仓中还没有会话，请先装载资料")
    return current


# ------------------------------------------------------------------ 位置与状态

def position_at(conn, key: str, as_of: _dt.datetime, session_id: str) -> dict | None:
    component = conn.execute(
        "SELECT l.component_key, l.manufacturer, l.serial, l.class, l.lot "
        "FROM landing_component l JOIN meta_receive r ON r.receive_id = l.receive_id "
        "WHERE l.component_key = ? AND r.session_id IN " + SCOPE + " "
        "QUALIFY row_number() OVER (PARTITION BY l.component_key ORDER BY l.receive_id DESC) = 1",
        [key, *load._session_bound(conn, session_id)],
    ).fetchone()
    if not component:
        return None
    movement = conn.execute(
        f"SELECT to_location, {_AT}, scan_id FROM fact_movement "
        f"WHERE session_id = ? AND component_key = ? AND {_AT} <= ? "
        f"ORDER BY {_AT} DESC, scan_id DESC LIMIT 1",
        [session_id, key, as_of],
    ).fetchone()
    if movement:
        location, at, scan_id = movement
        basis = "scan"
    else:
        location, at, scan_id = "store", None, None
        basis = "opening"
    quality = conn.execute(
        f"SELECT state, {_AT}, event_id FROM fact_quality_event "
        f"WHERE session_id = ? AND component_key = ? AND {_AT} <= ? "
        f"ORDER BY {_AT} DESC, event_id DESC LIMIT 1",
        [session_id, key, as_of],
    ).fetchone()
    if quality:
        state, state_at, event_id = quality
    else:
        state, state_at, event_id = "working", None, None
    return {
        "component_key": component[0],
        "manufacturer": component[1],
        "serial": component[2],
        "class": component[3],
        "lot": component[4],
        "location": location,
        "location_basis": basis,
        "location_scan": scan_id,
        "location_at": at.isoformat() if at else None,
        "quality_state": state,
        "quality_event": event_id,
        "quality_at": state_at.isoformat() if state_at else None,
    }


def assembly_at(conn, kit_key: str, as_of: _dt.datetime, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT assembly_id, component_key, start, \"end\", basis FROM fact_assembly "
        "WHERE session_id = ? AND kit_key = ? AND start <= ? AND (\"end\" IS NULL OR \"end\" > ?) "
        "ORDER BY component_key, assembly_id",
        [session_id, kit_key, as_of, as_of],
    ).fetchall()
    return [
        {
            "assembly_id": r[0],
            "component_key": r[1],
            "start": r[2].isoformat(),
            "end": r[3].isoformat() if r[3] else None,
            "basis": r[4],
        }
        for r in rows
    ]


def composition_at(conn, model: str, version: str, as_of: _dt.datetime, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT rule_id, class, required, quantity, choice_group, effective_from, effective_to "
        "FROM rule_period WHERE session_id = ? AND model = ? AND version = ? "
        "AND effective_from <= ? AND (effective_to IS NULL OR effective_to > ?) "
        "ORDER BY rule_id",
        [session_id, model, version, as_of, as_of],
    ).fetchall()
    return [
        {
            "rule_id": r[0], "class": r[1], "required": r[2], "quantity": r[3],
            "choice_group": r[4],
            "effective_from": r[5].isoformat(),
            "effective_to": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]


def kit_at(conn, kit_key: str, as_of=None, session_id: str | None = None) -> dict | None:
    as_of = parse_moment(as_of)
    session_id = resolve_session(conn, session_id)
    kit = conn.execute(
        "SELECT l.kit_key, l.model, l.version FROM landing_kit l "
        "JOIN meta_receive r ON r.receive_id = l.receive_id "
        "WHERE l.kit_key = ? AND r.session_id IN " + SCOPE + " "
        "QUALIFY row_number() OVER (PARTITION BY l.kit_key ORDER BY l.receive_id DESC) = 1",
        [kit_key, *load._session_bound(conn, session_id)],
    ).fetchone()
    if not kit:
        return None
    return {
        "kit_key": kit[0],
        "model": kit[1],
        "version": kit[2],
        "as_of": as_of.isoformat(),
        "session_id": session_id,
        "composition": composition_at(conn, kit[1], kit[2], as_of, session_id),
        "assembled": [
            {**a, **position_at(conn, a["component_key"], as_of, session_id)}
            for a in assembly_at(conn, kit_key, as_of, session_id)
        ],
    }


# ------------------------------------------------------------------ 交付

def deliveries_for_customer(conn, customer: str, as_of=None,
                            session_id: str | None = None) -> list[dict]:
    as_of = parse_moment(as_of)
    session_id = resolve_session(conn, session_id)
    rows = conn.execute(
        f"SELECT shipment_id, kit_key, kind, {_AT}, evidence FROM fact_delivery "
        f"WHERE session_id = ? AND customer = ? AND {_AT} <= ? "
        f"ORDER BY {_AT}, shipment_id",
        [session_id, customer, as_of],
    ).fetchall()
    return [
        {"shipment_id": r[0], "kit_key": r[1], "kind": r[2],
         "at": r[3].isoformat(), "evidence": json.loads(r[4])}
        for r in rows
    ]


def kit_trail(conn, kit_key: str, as_of=None, session_id: str | None = None) -> dict | None:
    """整套视角：当时的组成与在套件；以及该套的交付与售后经历。"""
    as_of = parse_moment(as_of)
    session_id = resolve_session(conn, session_id)
    kit = kit_at(conn, kit_key, as_of, session_id)
    if not kit:
        return None
    deliveries = conn.execute(
        f"SELECT shipment_id, customer, kind, {_AT} FROM fact_delivery "
        f"WHERE session_id = ? AND kit_key = ? AND {_AT} <= ? ORDER BY {_AT}",
        [session_id, kit_key, as_of],
    ).fetchall()
    assemblies = conn.execute(
        "SELECT assembly_id, component_key, start, \"end\", basis FROM fact_assembly "
        "WHERE session_id = ? AND kit_key = ? AND start <= ? ORDER BY start, assembly_id",
        [session_id, kit_key, as_of],
    ).fetchall()
    return {
        **kit,
        "deliveries": [
            {"shipment_id": r[0], "customer": r[1], "kind": r[2], "at": r[3].isoformat()}
            for r in deliveries
        ],
        "assembly_history": [
            {"assembly_id": r[0], "component_key": r[1], "start": r[2].isoformat(),
             "end": r[3].isoformat() if r[3] else None, "basis": r[4]}
            for r in assemblies
        ],
    }


# ------------------------------------------------------------------ 单件

def component_trail(conn, key: str, as_of=None, session_id: str | None = None) -> dict | None:
    as_of = parse_moment(as_of)
    session_id = resolve_session(conn, session_id)
    here = position_at(conn, key, as_of, session_id)
    if not here:
        return None
    moves = conn.execute(
        f"SELECT scan_id, shipment_id, from_location, to_location, {_AT} FROM fact_movement "
        f"WHERE session_id = ? AND component_key = ? AND {_AT} <= ? ORDER BY {_AT}, scan_id",
        [session_id, key, as_of],
    ).fetchall()
    assemblies = conn.execute(
        "SELECT assembly_id, kit_key, start, \"end\", basis FROM fact_assembly "
        "WHERE session_id = ? AND component_key = ? AND start <= ? ORDER BY start, assembly_id",
        [session_id, key, as_of],
    ).fetchall()
    return {
        "component": here,
        "movements": [
            {"scan_id": m[0], "shipment_id": m[1], "from": m[2], "to": m[3],
             "at": m[4].isoformat()}
            for m in moves
        ],
        "assemblies": [
            {"assembly_id": a[0], "kit_key": a[1], "start": a[2].isoformat(),
             "end": a[3].isoformat() if a[3] else None, "basis": a[4]}
            for a in assemblies
        ],
    }


# ------------------------------------------------------------------ 批次追查

def _latest_components_cte(session_id: str) -> str:
    """该会话已知的最新组件主数据（lot 追查用；组件主数据没有业务生效时刻）。"""
    return (
        "SELECT l.component_key, l.lot FROM landing_component l "
        "JOIN meta_receive r ON r.receive_id = l.receive_id "
        "WHERE r.session_id IN " + SCOPE + " "
        "QUALIFY row_number() OVER (PARTITION BY l.component_key ORDER BY l.receive_id DESC) = 1"
    )


def lot_affected(conn, lot: str, as_of=None, session_id: str | None = None) -> dict:
    """只报查询时点实际装着该批件的套，而不是该套换过的全部历史部件。"""
    as_of = parse_moment(as_of)
    session_id = resolve_session(conn, session_id)
    bound = [*load._session_bound(conn, session_id)]
    live = conn.execute(
        "SELECT a.kit_key, a.component_key, a.assembly_id FROM fact_assembly a "
        "JOIN (" + _latest_components_cte(session_id) + ") c ON c.component_key = a.component_key "
        "WHERE a.session_id = ? AND c.lot = ? "
        "AND a.start <= ? AND (a.\"end\" IS NULL OR a.\"end\" > ?) "
        "ORDER BY a.kit_key, a.component_key",
        [*bound, session_id, lot, as_of, as_of],
    ).fetchall()
    historical = conn.execute(
        "SELECT DISTINCT a.kit_key, a.component_key FROM fact_assembly a "
        "JOIN (" + _latest_components_cte(session_id) + ") c ON c.component_key = a.component_key "
        "WHERE a.session_id = ? AND c.lot = ? "
        "AND NOT (a.start <= ? AND (a.\"end\" IS NULL OR a.\"end\" > ?)) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM fact_assembly live "
        "  WHERE live.session_id = a.session_id AND live.component_key = a.component_key "
        "  AND live.start <= ? AND (live.\"end\" IS NULL OR live.\"end\" > ?)"
        ") "
        "ORDER BY a.kit_key, a.component_key",
        [*bound, session_id, lot, as_of, as_of, as_of, as_of],
    ).fetchall()
    return {
        "lot": lot,
        "as_of": as_of.isoformat(),
        "session_id": session_id,
        "currently_assembled": [
            {"kit_key": r[0], "component_key": r[1], "assembly_id": r[2]} for r in live
        ],
        "historical_only": [
            {"kit_key": r[0], "component_key": r[1]} for r in historical
        ],
        "note": "historical_only 只说明曾经装过，不代表当前受影响。",
    }
