"""对账集市：按客户/套计算“已交付”与“当前可用”，解释中断，冻结已确认对账。

业务规则（全部可追溯到仓表）
----------------------------
1. 销量只来自 kind=sale 的整套发运单；一次整套扫描加同单部件扫描仍是一套。
2. 借入件恢复服务但不增加销量；归还只撤回“可用”，不冲销原销售。
3. 服务中断 = 已交客户的套，在查询时点缺少必需件或必需件 nonworking。
4. 已确认对账冻结为修订（revision）：新会话/新查询不覆盖旧内容，
   只追加新修订并标记旧修订 superseded。
5. 每个数字都带证据（发运、扫描、装配 id），售后可沿证据找差异。
"""
from __future__ import annotations

import datetime as _dt
import json

from .load import BUSINESS_TZ
from . import load
from . import query

_TZ = BUSINESS_TZ
_AT = '"at"'


def _now() -> _dt.datetime:
    return _dt.datetime.now(_TZ).replace(tzinfo=None)


def _customer_kits(conn, customer: str, as_of: _dt.datetime, session_id: str) -> list[str]:
    rows = conn.execute(
        f"SELECT DISTINCT kit_key FROM fact_delivery "
        f"WHERE session_id = ? AND customer = ? AND {_AT} <= ? ORDER BY kit_key",
        [session_id, customer, as_of],
    ).fetchall()
    return [r[0] for r in rows]


def availability(conn, customer: str, as_of=None, session_id: str | None = None) -> dict:
    """已交付套数与当前可用套数分离；可用与否由当时必需件和状态决定。"""
    as_of = query.parse_moment(as_of)
    session_id = query.resolve_session(conn, session_id)
    rows = []
    for kit_key in _customer_kits(conn, customer, as_of, session_id):
        kit = query.kit_at(conn, kit_key, as_of, session_id)
        if not kit:
            continue
        deliveries = [
            d for d in query.deliveries_for_customer(conn, customer, as_of, session_id)
            if d["kit_key"] == kit_key
        ]
        sale_deliveries = [d for d in deliveries if d["kind"] == "sale"]
        loan_deliveries = [d for d in deliveries if d["kind"] == "loan"]
        evaluation = _evaluate_kit(conn, kit_key, kit, as_of, deliveries)
        rows.append({
            "customer": customer,
            "kit_key": kit_key,
            "model": kit["model"],
            "version": kit["version"],
            "delivered_sets": len(sale_deliveries),
            "delivered_loans": len(loan_deliveries),
            "available_sets": 1 if evaluation["complete"] else 0,
            "service_interrupted": not evaluation["complete"],
            "missing_required": evaluation["missing"],
            "nonworking_required": evaluation["nonworking"],
            "over_limit": evaluation["over_limit"],
            "loan_components_in_use": evaluation["loans"],
            "evidence": {
                "deliveries": [d["shipment_id"] for d in deliveries],
                "composition": kit["composition"],
                "assembled": [a["component_key"] for a in kit["assembled"]],
            },
        })
    return {
        "customer": customer,
        "as_of": as_of.isoformat(),
        "session_id": session_id,
        "kits": rows,
        "delivered_sets_total": sum(r["delivered_sets"] for r in rows),
        "available_sets_total": sum(r["available_sets"] for r in rows),
        "service_interrupted_total": sum(1 for r in rows if r["service_interrupted"]),
        "note": "历史销量由交付事实确定；借入件不增加 delivered_sets。",
    }


def _evaluate_kit(conn, kit_key, kit, as_of, deliveries) -> dict:
    """按当时组成核对：必需件数量、互斥组上限、必需件质量状态。

    - 必需：同一 class 下所有 required 规则的 quantity 求和为该类最低件数；
    - 互斥组：同组各项合计不得超过组内声明的 quantity；
    - 可选：只记录在场，不影响完整性；
    - 缺件 = 必需类实际在套件数不足；nonworking = 必需类在套件状态非 working。
    """
    assembled = kit["assembled"]
    composition = kit["composition"]
    present_by_class: dict[str, list[dict]] = {}
    for item in assembled:
        present_by_class.setdefault(item["class"], []).append(item)

    missing, nonworking, loans, over_limit = [], [], [], []
    required_by_class: dict[str, dict] = {}
    for rule in composition:
        if not rule["required"]:
            continue
        bucket = required_by_class.setdefault(rule["class"], {"needed": 0, "rules": []})
        bucket["needed"] += rule["quantity"] or 1
        bucket["rules"].append(rule["rule_id"])
    for cls, need in sorted(required_by_class.items()):
        present = present_by_class.get(cls, [])
        if len(present) < need["needed"]:
            missing.append({
                "class": cls,
                "required": need["needed"],
                "present": len(present),
                "rules": need["rules"],
                "present_components": [p["component_key"] for p in present],
            })

    groups: dict[str, dict] = {}
    for rule in composition:
        group = rule["choice_group"]
        if not group:
            continue
        entry = groups.setdefault(group, {"limit": 0, "classes": []})
        entry["limit"] = max(entry["limit"], rule["quantity"] or 1)
        entry["classes"].append(rule["class"])
    for group, entry in sorted(groups.items()):
        present = [p for cls in entry["classes"] for p in present_by_class.get(cls, [])]
        if len(present) > entry["limit"]:
            over_limit.append({
                "choice_group": group, "limit": entry["limit"], "present": len(present),
                "components": [p["component_key"] for p in present],
            })

    # 质量只约束必需类的在套件；可选件损坏不构成服务中断。
    for cls in required_by_class:
        for item in present_by_class.get(cls, []):
            if item["quality_state"] != "working":
                nonworking.append({
                    "component_key": item["component_key"], "class": cls,
                    "quality_state": item["quality_state"], "quality_at": item["quality_at"],
                })
    for item in assembled:
        if item["basis"] == "loan":
            loans.append({
                "component_key": item["component_key"], "class": item["class"],
                "assembly_id": item["assembly_id"],
            })
    is_delivered = any(query.parse_moment(d["at"]) <= as_of for d in deliveries)
    complete = is_delivered and not missing and not nonworking
    return {
        "complete": complete,
        "missing": missing,
        "nonworking": nonworking,
        "loans": loans,
        "over_limit": over_limit,
    }


def interruption_intervals(conn, customer: str, kit_key: str, start,
                           end=None, session_id: str | None = None) -> list[dict]:
    """沿真实经历找出服务中断区间：[开始, 结束)，结束为空表示至今仍中断。

    只在“必需件缺少或 nonworking”时中断；借入件可恢复服务，但不改变销量。
    """
    session_id = query.resolve_session(conn, session_id)
    end = query.parse_moment(end) if end is not None else _now()
    deliveries = [
        d for d in query.deliveries_for_customer(conn, customer, end, session_id)
        if d["kit_key"] == kit_key
    ]
    if not deliveries:
        return []
    start = max(query.parse_moment(start), min(query.parse_moment(d["at"]) for d in deliveries))
    # 边界：交付时刻，以及区间内每次位置/质量/装配变化。每段的状态在该段起点之后判定，
    # 因此“10-12 卸下”属于 10-12 起的那一段，而不是 10-12 之前那一段。
    points = {start}
    for table in ("fact_movement", "fact_quality_event"):
        rows = conn.execute(
            f"SELECT DISTINCT {_AT} FROM {table} "
            f"WHERE session_id = ? AND {_AT} >= ? AND {_AT} < ?",
            [session_id, start, end],
        ).fetchall()
        points.update(r[0] for r in rows)
    rows = conn.execute(
        "SELECT DISTINCT start FROM fact_assembly WHERE session_id = ? AND kit_key = ? "
        "AND start >= ? AND start < ? "
        "UNION SELECT DISTINCT \"end\" FROM fact_assembly WHERE session_id = ? AND kit_key = ? "
        "AND \"end\" >= ? AND \"end\" < ?",
        [session_id, kit_key, start, end, session_id, kit_key, start, end],
    ).fetchall()
    points.update(r[0] for r in rows if r[0] is not None)
    points = sorted(points)

    def cause_at(moment):
        kit = query.kit_at(conn, kit_key, moment, session_id)
        if not kit:
            return None, None
        evaluation = _evaluate_kit(conn, kit_key, kit, moment, deliveries)
        if evaluation["missing"]:
            return "missing_required", evaluation
        if evaluation["nonworking"]:
            return "nonworking_required", evaluation
        return None, evaluation

    intervals, open_since, open_cause, open_eval = [], None, None, None
    for moment in points:
        cause, evaluation = cause_at(moment)
        if cause and open_since is None:
            open_since, open_cause, open_eval = moment, cause, evaluation
        elif not cause and open_since is not None:
            intervals.append({"from": open_since, "to": moment, "cause": open_cause,
                              "evaluation": open_eval})
            open_since = None
    if open_since is not None:
        intervals.append({"from": open_since, "to": None, "cause": open_cause,
                          "evaluation": open_eval})
    detail = []
    for interval in intervals:
        at = interval["from"]
        evaluation = interval["evaluation"] or {"missing": [], "nonworking": [], "loans": []}
        detail.append({
            "customer": customer,
            "kit_key": kit_key,
            "delivery_id": next((d["shipment_id"] for d in deliveries
                                 if query.parse_moment(d["at"]) <= at), None),
            "interrupted_from": interval["from"].isoformat(),
            "interrupted_to": interval["to"].isoformat() if interval["to"] else None,
            "cause": interval["cause"],
            "detail": {"missing": evaluation["missing"],
                       "nonworking": evaluation["nonworking"],
                       "loans": evaluation["loans"]},
        })
    return detail


# ------------------------------------------------------------------ 当前可用快照

def rebuild_availability(conn, session_id: str, as_of=None) -> dict:
    """把当前可用套数写为会话快照，供商务查询与冻结对账复用。"""
    as_of = query.parse_moment(as_of)
    conn.execute("DELETE FROM mart_current_availability WHERE session_id = ?", [session_id])
    customers = conn.execute(
        f"SELECT DISTINCT customer FROM fact_delivery WHERE session_id = ? AND {_AT} <= ? "
        f"ORDER BY customer", [session_id, as_of]
    ).fetchall()
    snapshot = []
    for (customer,) in customers:
        result = availability(conn, customer, as_of, session_id)
        for row in result["kits"]:
            detail = {
                "missing_required": row["missing_required"],
                "nonworking_required": row["nonworking_required"],
                "over_limit": row["over_limit"],
                "loan_components_in_use": row["loan_components_in_use"],
                "evidence": row["evidence"],
                "delivered_loans": row["delivered_loans"],
            }
            conn.execute(
                "INSERT INTO mart_current_availability VALUES (?,?,?,?,?,?,?,?,?,?)",
                [session_id, as_of, customer, row["kit_key"], row["model"], row["version"],
                 row["delivered_sets"], row["available_sets"], row["service_interrupted"],
                 json.dumps(detail, ensure_ascii=False)],
            )
            snapshot.append({**row, "session_id": session_id})
    return {
        "session_id": session_id,
        "as_of": as_of.isoformat(),
        "customers": [c[0] for c in customers],
        "kits": snapshot,
    }


# ------------------------------------------------------------------ 冻结对账

def _next_revision(conn) -> int:
    return conn.execute(
        "SELECT coalesce(max(revision_id), 0) + 1 FROM mart_contract_revision"
    ).fetchone()[0]


def freeze_reconcile(conn, customer: str, as_of=None, note: str | None = None,
                     session_id: str | None = None) -> dict:
    """确认一次客户对账：冻结该时点的解释；旧修订保留内容并被标记 superseded。"""
    as_of = query.parse_moment(as_of)
    session_id = query.resolve_session(conn, session_id)
    snapshot = rebuild_availability(conn, session_id, as_of)
    my_kits = [k for k in snapshot["kits"] if k["customer"] == customer]
    body = {
        "customer": customer,
        "as_of": as_of.isoformat(),
        "session_id": session_id,
        "kits": my_kits,
        "delivered_sets_total": sum(k["delivered_sets"] for k in my_kits),
        "available_sets_total": sum(k["available_sets"] for k in my_kits),
        "service_interrupted_total": sum(1 for k in my_kits if k["service_interrupted"]),
    }
    previous = conn.execute(
        "SELECT revision_id FROM mart_contract_revision "
        "WHERE kind = 'reconcile' AND subject = ? AND status = 'confirmed' "
        "ORDER BY revision_id DESC LIMIT 1", [customer]
    ).fetchone()
    revision_id = _next_revision(conn)
    conn.execute(
        "INSERT INTO mart_contract_revision VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [revision_id, session_id, _now(), "reconcile", customer, as_of, session_id,
         "confirmed", json.dumps(body, ensure_ascii=False),
         previous[0] if previous else None, note],
    )
    if previous:
        conn.execute(
            "UPDATE mart_contract_revision SET status = 'superseded' WHERE revision_id = ?",
            [previous[0]],
        )
    return {"revision_id": revision_id, "supersedes": previous[0] if previous else None,
            "body": body}


def revisions(conn, customer: str | None = None, kind: str | None = None) -> list[dict]:
    clauses, params = [], []
    if customer is not None:
        clauses.append("subject = ?")
        params.append(customer)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT revision_id, session_id, created_at, kind, subject, query_at, as_of_session, "
        f"status, body, supersedes, note FROM mart_contract_revision{where} "
        f"ORDER BY revision_id", params
    ).fetchall()
    return [
        {
            "revision_id": r[0], "session_id": r[1],
            "created_at": r[2].isoformat(), "kind": r[3], "subject": r[4],
            "query_at": r[5].isoformat(), "as_of_session": r[6], "status": r[7],
            "body": json.loads(r[8]), "supersedes": r[9], "note": r[10],
        }
        for r in rows
    ]


def compare_revisions(conn, left_id: int, right_id: int) -> dict:
    """比较补正前后的解释：逐套列出差异字段，不重新计算旧修订。"""
    rows = conn.execute(
        "SELECT revision_id, body FROM mart_contract_revision WHERE revision_id IN (?, ?)",
        [left_id, right_id],
    ).fetchall()
    bodies = {r[0]: json.loads(r[1]) for r in rows}
    if left_id not in bodies or right_id not in bodies:
        raise ValueError("修订不存在")
    left, right = bodies[left_id], bodies[right_id]
    left_kits = {k["kit_key"]: k for k in left.get("kits", [])}
    right_kits = {k["kit_key"]: k for k in right.get("kits", [])}
    differences = []
    for kit_key in sorted(set(left_kits) | set(right_kits)):
        a, b = left_kits.get(kit_key), right_kits.get(kit_key)
        if a is None or b is None:
            differences.append({"kit_key": kit_key, "field": "presence",
                                "left": a is not None, "right": b is not None})
            continue
        for field in ("delivered_sets", "available_sets", "service_interrupted"):
            if a.get(field) != b.get(field):
                differences.append({"kit_key": kit_key, "field": field,
                                    "left": a.get(field), "right": b.get(field)})
    return {
        "left": {"revision_id": left_id, "as_of": left.get("as_of"),
                 "session_id": left.get("session_id"),
                 "delivered_sets_total": left.get("delivered_sets_total"),
                 "available_sets_total": left.get("available_sets_total")},
        "right": {"revision_id": right_id, "as_of": right.get("as_of"),
                  "session_id": right.get("session_id"),
                  "delivered_sets_total": right.get("delivered_sets_total"),
                  "available_sets_total": right.get("available_sets_total")},
        "differences": differences,
        "note": "差异逐套列示；确认过的修订正文不改写。",
    }


# ------------------------------------------------------------------ 冲突与待核

def rebuild_conflicts(conn, session_id: str, as_of=None) -> dict:
    """同号冲突、重叠装配、位置不一致等以可解释的待核范围保存。"""
    as_of = query.parse_moment(as_of)
    conn.execute("DELETE FROM mart_conflict WHERE session_id = ?", [session_id])
    conn.execute("DELETE FROM mart_position WHERE session_id = ?", [session_id])
    found = []

    # 1) 同号冲突：真正需要看的是同一 manufacturer 内同 serial 出现多个完整键。
    collisions = conn.execute(
        "SELECT manufacturer, serial, count(DISTINCT component_key) n, "
        "string_agg(DISTINCT component_key, ',') keys FROM ("
        "  SELECT l.manufacturer, l.serial, l.component_key FROM landing_component l "
        "  JOIN meta_receive r ON r.receive_id = l.receive_id "
        "  WHERE r.session_id IN " + load.SESSION_UP_TO + " "
        "  AND l.manufacturer IS NOT NULL AND l.serial IS NOT NULL "
        "  QUALIFY row_number() OVER (PARTITION BY l.component_key ORDER BY l.receive_id DESC) = 1"
        ") latest GROUP BY manufacturer, serial HAVING count(DISTINCT component_key) > 1",
        [*load._session_bound(conn, session_id)],
    ).fetchall()
    for manufacturer, serial, n, keys in collisions:
        found.append({
            "kind": "serial_collision", "severity": "review",
            "subject": f"{manufacturer}/{serial}",
            "evidence": {"manufacturer": manufacturer, "serial": serial,
                         "component_keys": sorted(keys.split(","))},
        })

    # 2) 重叠装配：同一件同一时刻装在两套。
    overlaps = conn.execute(
        "SELECT a.component_key, a.assembly_id, b.assembly_id, a.kit_key, b.kit_key, "
        "a.start, b.start FROM fact_assembly a JOIN fact_assembly b "
        "ON a.session_id = b.session_id AND a.component_key = b.component_key "
        "AND a.assembly_id < b.assembly_id "
        "AND a.start < coalesce(b.\"end\", TIMESTAMP '9999-12-31 23:59:59') "
        "AND b.start < coalesce(a.\"end\", TIMESTAMP '9999-12-31 23:59:59') "
        "WHERE a.session_id = ?",
        [session_id],
    ).fetchall()
    for component_key, left, right, left_kit, right_kit, left_start, right_start in overlaps:
        found.append({
            "kind": "overlap_assembly", "severity": "review",
            "subject": f"{component_key}/{left}~{right}",
            "evidence": {"component_key": component_key, "assembly_ids": [left, right],
                         "kits": [left_kit, right_kit],
                         "starts": [left_start.isoformat(), right_start.isoformat()]},
        })

    # 3) 借出仍在现场：借入件尚未见返回，保留临时使用权，不计销量。
    on_loan = conn.execute(
        f"SELECT d.shipment_id, d.kit_key, s.scan_id, s.to_location, s.{_AT} "
        f"FROM fact_delivery d JOIN fact_movement s "
        f"ON s.session_id = d.session_id AND s.shipment_id = d.shipment_id "
        f"WHERE d.session_id = ? AND d.kind = 'loan' AND s.{_AT} <= ?",
        [session_id, as_of],
    ).fetchall()
    for shipment_id, kit_key, scan_id, to_location, at in on_loan:
        found.append({
            "kind": "loan_in_field", "severity": "info",
            "subject": f"{shipment_id}/{scan_id}",
            "evidence": {"shipment_id": shipment_id, "kit_key": kit_key,
                         "scan_id": scan_id, "location": to_location, "at": at.isoformat()},
        })

    for item in found:
        conn.execute(
            "INSERT INTO mart_conflict VALUES (?,?,?,?,?)",
            [session_id, item["kind"], item["severity"], item["subject"],
             json.dumps(item["evidence"], ensure_ascii=False)],
        )
    components = conn.execute(
        "SELECT l.component_key FROM landing_component l "
        "JOIN meta_receive r ON r.receive_id = l.receive_id "
        "WHERE r.session_id IN " + load.SESSION_UP_TO + " "
        "QUALIFY row_number() OVER (PARTITION BY l.component_key ORDER BY l.receive_id DESC) = 1",
        [*load._session_bound(conn, session_id)],
    ).fetchall()
    for (component_key,) in components:
        pos = query.position_at(conn, component_key, as_of, session_id)
        if not pos:
            continue
        conn.execute(
            "INSERT INTO mart_position VALUES (?,?,?,?,?)",
            [session_id, pos["component_key"], pos["location"], pos["quality_state"],
             pos["quality_at"]],
        )

    physical = _physical_consistency(conn, session_id, as_of)
    found.extend(physical)
    for item in physical:
        conn.execute(
            "INSERT OR REPLACE INTO mart_conflict VALUES (?,?,?,?,?)",
            [session_id, item["kind"], item["severity"], item["subject"],
             json.dumps(item["evidence"], ensure_ascii=False)],
        )
    return {
        "session_id": session_id,
        "as_of": as_of.isoformat(),
        "conflicts": found,
        "review_count": sum(1 for f in found if f["severity"] == "review"),
    }


def _physical_consistency(conn, session_id: str, as_of: _dt.datetime) -> list[dict]:
    """当前所在位置、质量状态与是否承担另一客户服务要彼此一致。

    可解释的不一致进入待核；不猜测哪个字段对。
    """
    issues = []
    # 1) 在套件应位于其套的客户处，否则当前位置与装配经历不一致。
    rows = conn.execute(
        "SELECT a.component_key, a.kit_key, d.customer FROM fact_assembly a "
        f"JOIN fact_delivery d ON d.session_id = a.session_id AND d.kit_key = a.kit_key "
        f"WHERE a.session_id = ? AND a.start <= ? AND (a.\"end\" IS NULL OR a.\"end\" > ?) "
        f"AND d.{_AT} <= ? "
        f"ORDER BY d.{_AT} DESC",
        [session_id, as_of, as_of, as_of],
    ).fetchall()
    customer_by_kit: dict[str, str] = {}
    for component_key, kit_key, customer in rows:
        customer_by_kit.setdefault(component_key, customer)
    for component_key, customer in customer_by_kit.items():
        pos = query.position_at(conn, component_key, as_of, session_id)
        if not pos or pos["location"] in (customer, "repair"):
            continue
        issues.append({
            "kind": "position_mismatch", "severity": "review",
            "subject": component_key,
            "evidence": {"component_key": component_key, "expected": customer,
                         "actual": pos["location"], "scan_id": pos["location_scan"],
                         "at": pos["location_at"]},
        })
    # 2) 已归还（在库）的件不应仍处于开放装配中。
    returned_open = conn.execute(
        "SELECT a.component_key, a.kit_key, a.assembly_id FROM fact_assembly a "
        f"WHERE a.session_id = ? AND a.start <= ? AND a.\"end\" IS NULL "
        "AND EXISTS (SELECT 1 FROM mart_position p WHERE p.session_id = a.session_id "
        "            AND p.item_key = a.component_key AND p.location = 'store')",
        [session_id, as_of],
    ).fetchall()
    for component_key, kit_key, assembly_id in returned_open:
        issues.append({
            "kind": "returned_but_assembled", "severity": "review",
            "subject": f"{component_key}/{assembly_id}",
            "evidence": {"component_key": component_key, "kit_key": kit_key,
                         "assembly_id": assembly_id, "location": "store"},
        })
    return issues
