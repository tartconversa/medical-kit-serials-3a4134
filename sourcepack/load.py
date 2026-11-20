"""装载：把一份资料接收进仓，生成会话，不破坏已确认的对账。

关键约定
--------
1. 每次接收生成会话（session）；旧会话的事实仍然可查，不被后来的修订改写。
2. 前区 landing_* 只追加，保存每次接收的原始行和 receive_id。
3. 同一来源记录键的新文件是整行修订，按接收次序覆盖“当时的最新版本”。
4. 事实表按会话物化：查询某会话时，只看该会话及之前已接收的版本。
5. 业务时点（at / start / end）与接收时点（received_at）分开。
6. 相同文件摘要不增加事实；整份资料都没有新摘要时不生成新会话。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path

import duckdb

from .catalog import read_table
from .schema import DDL

BUSINESS_TZ = _dt.timezone(_dt.timedelta(hours=8))

TABLES = {
    "components": {
        "landing": "landing_component",
        "columns": ["component_key", "manufacturer", "serial", "class", "lot"],
        "key": "component_key",
        "types": {},
    },
    "kits": {
        "landing": "landing_kit",
        "columns": ["kit_key", "model", "version"],
        "key": "kit_key",
        "types": {},
    },
    "composition_rules": {
        "landing": "landing_composition_rule",
        "columns": ["rule_id", "model", "version", "class", "required", "quantity", "choice_group"],
        "key": "rule_id",
        "types": {"required": "bool", "quantity": "int"},
    },
    "assembly": {
        "landing": "landing_assembly",
        "columns": ["assembly_id", "kit_key", "component_key", "start", "end", "basis"],
        "key": "assembly_id",
        "types": {"start": "ts", "end": "ts"},
    },
    "shipments": {
        "landing": "landing_shipment",
        "columns": ["shipment_id", "kit_key", "customer", "kind", "at"],
        "key": "shipment_id",
        "types": {"at": "ts"},
    },
    "scans": {
        "landing": "landing_scan",
        "columns": ["scan_id", "component_key", "shipment_id", "from_location", "to_location", "at"],
        "key": "scan_id",
        "types": {"at": "ts"},
    },
    "quality_events": {
        "landing": "landing_quality_event",
        "columns": ["event_id", "component_key", "state", "at"],
        "key": "event_id",
        "types": {"at": "ts"},
    },
}


class LoadError(ValueError):
    """资料无法按口径装载。"""


_RESERVED = {"end", "at"}


def _quote(name: str) -> str:
    return f'"{name}"' if name in _RESERVED else name


# 会话可见范围：该会话及其之前创建的会话。按 created_at + session_id 排序，
# 不依赖会话编号的字符串大小（编号只是展示用）。
SESSION_UP_TO = (
    "(SELECT session_id FROM meta_session s WHERE s.created_at < "
    "(SELECT created_at FROM meta_session WHERE session_id = ?) OR ("
    "s.created_at = (SELECT created_at FROM meta_session WHERE session_id = ?) "
    "AND s.session_id <= ?))"
)


def _session_bound(conn, session_id: str) -> tuple:
    """SESSION_UP_TO 的三个占位参数：同一会话互为边界。"""
    return (session_id, session_id, session_id)


def _parse_ts(value: str) -> _dt.datetime:
    stamp = _dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise LoadError("日期时刻缺少时区")
    return stamp.astimezone(BUSINESS_TZ).replace(tzinfo=None)


def _coerce(column: str, value: str, kind: str):
    if value == "":
        return None
    if kind == "bool":
        if value not in {"true", "false"}:
            raise LoadError(f"布尔格式未知：{column}")
        return value == "true"
    if kind == "int":
        try:
            return int(value)
        except ValueError:
            raise LoadError(f"整数格式未知：{column}") from None
    if kind == "ts":
        return _parse_ts(value)
    return value


def _coerce_row(table: str, row: dict) -> tuple:
    spec = TABLES[table]
    return tuple(_coerce(c, row[c], spec["types"].get(c, "str")) for c in spec["columns"])


# ------------------------------------------------------------------ 资料读取

def load_source(path: Path, table_name: str) -> tuple[list[dict], str]:
    """读取一份资料文件，返回 (行, sha256)，并按目录核对行数。"""
    path = Path(path)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    catalog_file = path.parent / "catalog.json"
    if not catalog_file.is_file():
        raise LoadError("缺少目录：data/catalog.json")
    catalog = json.loads(catalog_file.read_text(encoding="utf-8"))
    entry = next((t for t in catalog["tables"] if t["name"] == table_name), None)
    if entry is None:
        raise LoadError("目录中没有该表：" + table_name)
    rows = read_table(path, entry)
    if len(rows) != entry["row_count"]:
        raise LoadError("记录数与目录不一致：" + table_name)
    return rows, digest


def _catalog_digest(root: Path) -> str:
    return hashlib.sha256((root / "data" / "catalog.json").read_bytes()).hexdigest()


# ------------------------------------------------------------------ 会话

def session_order(conn) -> list[str]:
    rows = conn.execute(
        "SELECT session_id FROM meta_session ORDER BY created_at, session_id"
    ).fetchall()
    return [r[0] for r in rows]


def latest_session(conn) -> str | None:
    order = session_order(conn)
    return order[-1] if order else None


def _create_session(conn, now, label, catalog_sha256) -> str:
    order = session_order(conn)
    index = len(order) + 1
    existing = set(order)
    session_id = f"session-{index:04d}"
    while session_id in existing:
        index += 1
        session_id = f"session-{index:04d}"
    conn.execute(
        "INSERT INTO meta_session VALUES (?, ?, ?, ?, ?, NULL)",
        [session_id, now, label, catalog_sha256, order[-1] if order else None],
    )
    return session_id


def _known_digest(conn, table_name: str, digest: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM meta_receive WHERE table_name = ? AND sha256 = ? LIMIT 1",
        [table_name, digest],
    ).fetchone() is not None


def _insert_receive(conn, session_id, table_name, source_path, digest, row_count, now) -> int:
    seq = conn.execute("SELECT coalesce(max(receive_id), 0) + 1 FROM meta_receive").fetchone()[0]
    conn.execute(
        "INSERT INTO meta_receive VALUES (?, ?, ?, ?, ?, ?, ?)",
        [seq, session_id, table_name, source_path, digest, row_count, now],
    )
    conn.execute(
        "INSERT INTO meta_source_pack VALUES (?, ?, ?, ?, ?)",
        [session_id, table_name, source_path, digest, row_count],
    )
    return seq


def _apply_rows(conn, table_name, rows, source_path, digest, receive_id):
    """前区只追加；金区 current_* 覆盖为最新接收版本。"""
    spec = TABLES[table_name]
    landing_cols = spec["columns"]
    select_list = ", ".join(_quote(c) for c in landing_cols)
    insert_sql = (
        f"INSERT INTO {spec['landing']} ({select_list}, source_path, source_sha256, receive_id) "
        f"VALUES ({', '.join('?' for _ in landing_cols)}, ?, ?, ?)"
    )
    delete_sql = f"DELETE FROM current_{_suffix(table_name)} WHERE {spec['key']} = ?"
    current_cols = landing_cols + ["receive_id"]
    current_sql = (
        f"INSERT INTO current_{_suffix(table_name)} "
        f"({', '.join(_quote(c) for c in current_cols)}) "
        f"VALUES ({', '.join('?' for _ in current_cols)})"
    )
    for row in rows:
        values = _coerce_row(table_name, row)
        if values[0] in (None, ""):
            raise LoadError(f"记录键为空：{table_name}")
        conn.execute(delete_sql, [values[0]])
        conn.execute(insert_sql, list(values) + [source_path, digest, receive_id])
        conn.execute(current_sql, list(values) + [receive_id])
        if table_name == "composition_rules":
            version_id = conn.execute(
                "SELECT coalesce(max(version_row_id), 0) + 1 FROM version_composition_rule"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO version_composition_rule VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [version_id, receive_id] + list(values),
            )


# ------------------------------------------------------------------ 会话快照

def snapshot(conn, session_id: str) -> dict[str, dict[tuple, tuple]]:
    """返回该会话“已知”的每张表：{key: 行}。

    对每个记录键，取该会话及之前最后一次接收的版本。
    """
    order = session_order(conn)
    if session_id not in order:
        raise ValueError("未知会话：" + session_id)
    allowed = order[: order.index(session_id) + 1]
    placeholders = ", ".join("?" for _ in allowed)
    result = {}
    for table_name, spec in TABLES.items():
        key = spec["key"]
        columns = ", ".join(f"l.{_quote(c)}" for c in spec["columns"])
        sql = (
            f"SELECT {columns}, l.receive_id FROM {spec['landing']} l "
            f"JOIN meta_receive r ON r.receive_id = l.receive_id "
            f"WHERE r.session_id IN ({placeholders}) "
            f"QUALIFY row_number() OVER (PARTITION BY l.{key} ORDER BY l.receive_id DESC) = 1"
        )
        rows = conn.execute(sql, allowed).fetchall()
        result[table_name] = {row[0]: row for row in rows}
    return result


# ------------------------------------------------------------------ 事实物化

def _rebuild_deliveries(conn, session_id, state):
    conn.execute("DELETE FROM fact_delivery WHERE session_id = ?", [session_id])
    conn.execute("DELETE FROM fact_delivery_scan WHERE session_id = ?", [session_id])
    scans_by_shipment: dict[str, list] = {}
    for scan in state["scans"].values():
        scan_id, component_key, shipment_id, from_loc, to_loc, at, receive_id = scan
        if shipment_id:
            scans_by_shipment.setdefault(shipment_id, []).append(scan)
    for shipment in state["shipments"].values():
        shipment_id, kit_key, customer, kind, at, receive_id = shipment
        if None in (shipment_id, kit_key, customer, kind, at) or kind not in {"sale", "loan"}:
            continue
        evidence_scans = sorted(scans_by_shipment.get(shipment_id, []), key=lambda s: (s[5], s[0]))
        evidence = {
            "shipment_id": shipment_id,
            "kit_key": kit_key,
            "kind": kind,
            "part_scans": [s[0] for s in evidence_scans],
            "scan_count": len(evidence_scans),
            "billed_as_set": kind == "sale",
        }
        conn.execute(
            "INSERT INTO fact_delivery VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [session_id, shipment_id, kit_key, customer, kind, at, receive_id,
             json.dumps(evidence, ensure_ascii=False)],
        )
        for scan in evidence_scans:
            conn.execute(
                "INSERT INTO fact_delivery_scan VALUES (?, ?, ?, ?, ?)",
                [session_id, shipment_id, scan[0], scan[1], scan[5]],
            )


def _rebuild_movements(conn, session_id, state):
    conn.execute("DELETE FROM fact_movement WHERE session_id = ?", [session_id])
    for scan in state["scans"].values():
        scan_id, component_key, shipment_id, from_loc, to_loc, at, receive_id = scan
        if scan_id is None or component_key is None or at is None:
            continue
        conn.execute(
            "INSERT INTO fact_movement VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [session_id, scan_id, component_key, shipment_id, from_loc, to_loc, at, receive_id],
        )


def _rebuild_assemblies(conn, session_id, state):
    conn.execute("DELETE FROM fact_assembly WHERE session_id = ?", [session_id])
    for row in state["assembly"].values():
        assembly_id, kit_key, component_key, start, end, basis, receive_id = row
        if None in (assembly_id, kit_key, component_key, start, basis):
            continue
        conn.execute(
            "INSERT INTO fact_assembly VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [session_id, assembly_id, kit_key, component_key, start, end, basis, receive_id],
        )


def _rebuild_quality(conn, session_id, state):
    conn.execute("DELETE FROM fact_quality_event WHERE session_id = ?", [session_id])
    for row in state["quality_events"].values():
        event_id, component_key, state_value, at, receive_id = row
        if None in (event_id, component_key, state_value, at):
            continue
        conn.execute(
            "INSERT INTO fact_quality_event VALUES (?, ?, ?, ?, ?, ?)",
            [session_id, event_id, component_key, state_value, at, receive_id],
        )


def _rebuild_rule_periods(conn, session_id, state, now):
    """组成规则的版本史与生效范围。

    composition_rules 没有独立的生效日期字段：
    - 某规则首次被接收时，从资料中的最早业务时刻起生效（覆盖历史交付）；
    - 之后接收的新版本，从本次接收时刻起生效；
    - 每个会话物化自己“当时已知”的完整版本史，旧会话看不到后来的修订。

    同一记录键的整行修订只改字段，不改变 rule_id 的身份。
    """
    conn.execute("DELETE FROM rule_period WHERE session_id = ?", [session_id])
    business_floor = conn.execute(
        "SELECT min(moment) FROM ("
        "  SELECT min(\"at\") moment FROM fact_movement WHERE session_id = ?"
        "  UNION ALL SELECT min(start) FROM fact_assembly WHERE session_id = ?"
        "  UNION ALL SELECT min(\"at\") moment FROM fact_delivery WHERE session_id = ?"
        "  UNION ALL SELECT min(\"at\") moment FROM fact_quality_event WHERE session_id = ?"
        ")",
        [session_id] * 4,
    ).fetchone()[0]
    floor = business_floor or now
    visible_receives = _received_by(conn, session_id, "composition_rules")
    for row in state["composition_rules"].values():
        rule_id, model, version, cls, required, quantity, choice_group, receive_id = row
        if rule_id is None:
            continue
        # 该会话已知的版本史：只含本会话及之前接收过的版本。
        history = sorted(
            receive for receive in (
                r[0] for r in conn.execute(
                    "SELECT receive_id FROM version_composition_rule WHERE rule_id = ?",
                    [rule_id],
                ).fetchall()
            )
            if receive in visible_receives
        )
        if not history:
            continue
        first_receive = history[0]
        for version_receive in history:
            decider = conn.execute(
                "SELECT decided_at FROM meta_receive WHERE receive_id = ?", [version_receive]
            ).fetchone()[0]
            start_at = floor if version_receive == first_receive else decider
            later = conn.execute(
                "SELECT r.decided_at FROM version_composition_rule v "
                "JOIN meta_receive r ON r.receive_id = v.receive_id "
                "WHERE v.rule_id = ? AND v.receive_id > ? "
                "AND v.receive_id IN (" + ", ".join("?" for _ in visible_receives) + ") "
                "ORDER BY v.receive_id LIMIT 1",
                [rule_id, version_receive, *visible_receives],
            ).fetchone()
            end_at = later[0] if later else None
            version_row = conn.execute(
                "SELECT model, version, class, required, quantity, choice_group "
                "FROM version_composition_rule WHERE receive_id = ? AND rule_id = ?",
                [version_receive, rule_id],
            ).fetchone()
            conn.execute(
                "INSERT INTO rule_period VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [session_id, rule_id, version_receive, start_at, end_at,
                 version_row[0], version_row[1], version_row[2], version_row[3],
                 version_row[4], version_row[5]],
            )


def _received_by(conn, session_id: str, table_name: str) -> set[int]:
    """该会话及其之前所有会话中，该表已经接收过的 receive_id。"""
    rows = conn.execute(
        "SELECT r.receive_id FROM meta_receive r "
        "WHERE r.table_name = ? AND r.session_id IN " + SESSION_UP_TO,
        [table_name, *_session_bound(conn, session_id)],
    ).fetchall()
    return {r[0] for r in rows}


def rebuild_facts(conn, session_id, now):
    state = snapshot(conn, session_id)
    _validate_references(state)
    _rebuild_movements(conn, session_id, state)
    _rebuild_deliveries(conn, session_id, state)
    _rebuild_assemblies(conn, session_id, state)
    _rebuild_quality(conn, session_id, state)
    _rebuild_rule_periods(conn, session_id, state, now)


def _validate_references(state):
    checks = [
        ("assembly", 1, "kits", 0, "assembly.kit_key"),
        ("assembly", 2, "components", 0, "assembly.component_key"),
        ("shipments", 1, "kits", 0, "shipments.kit_key"),
        ("scans", 1, "components", 0, "scans.component_key"),
        ("scans", 2, "shipments", 0, "scans.shipment_id"),
        ("quality_events", 1, "components", 0, "quality_events.component_key"),
    ]
    for source, index, target, target_index, label in checks:
        allowed = set(state[target].keys())
        missing = sorted({
            row[index] for row in state[source].values()
            if row[index] not in (None, "") and row[index] not in allowed
        })
        if missing:
            raise LoadError(f"{label} 引用了不存在的记录：" + ", ".join(missing[:5]))
    _validate_domains(state)
    _validate_assembly_intervals(state)


def _validate_domains(state):
    """枚举字段只接受口径声明的取值；未知值不静默当成其他含义。"""
    domains = {
        "assembly": (5, {"permanent", "loan"}, "assembly.basis"),
        "shipments": (3, {"sale", "loan"}, "shipments.kind"),
        "quality_events": (2, {"working", "nonworking"}, "quality_events.state"),
    }
    for table_name, (index, allowed, label) in domains.items():
        bad = sorted({
            row[index] for row in state[table_name].values()
            if row[index] not in (None, "") and row[index] not in allowed
        })
        if bad:
            raise LoadError(f"{label} 取值未知：" + ", ".join(bad[:5]))
    # 互斥组必须与“同组最多 quantity 件”的口径一致：组内 quantity 必须为正。
    bad_rules = sorted({
        row[0] for row in state["composition_rules"].values()
        if row[6] and (row[5] is None or row[5] < 1)
    })
    if bad_rules:
        raise LoadError("互斥组数量无效：" + ", ".join(bad_rules[:5]))


def _validate_assembly_intervals(state):
    """装配区间必须左闭右开且非空：end <= start 无法表达“装入过”。"""
    bad = sorted({
        row[0] for row in state["assembly"].values()
        if row[3] is not None and row[4] is not None and row[4] <= row[3]
    })
    if bad:
        raise LoadError("装配结束不晚于开始：" + ", ".join(bad[:5]))


# ------------------------------------------------------------------ 装载入口

def open_database(path: Path | str) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(path))
    conn.execute("SET TimeZone = 'Asia/Shanghai'")
    for statement in DDL:
        conn.execute(statement)
    return conn


def open_existing(path: Path | str) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(path))
    conn.execute("SET TimeZone = 'Asia/Shanghai'")
    return conn


def receive(conn, root: Path, label: str, now: _dt.datetime | None = None) -> dict:
    """装载一份资料目录，生成新会话；没有新摘要时不生成新会话。"""
    root = Path(root)
    now = now or _dt.datetime.now(BUSINESS_TZ).replace(tzinfo=None)

    pending = {}
    skipped = []
    for table_name in TABLES:
        source = root / "data" / f"{table_name}.csv"
        if not source.is_file():
            continue
        rows, digest = load_source(source, table_name)
        if _known_digest(conn, table_name, digest):
            skipped.append({"name": table_name, "status": "unchanged", "sha256": digest})
            continue
        pending[table_name] = (source, rows, digest)

    if not pending:
        previous = latest_session(conn)
        return {
            "session_id": previous,
            "status": "unchanged",
            "note": "所有文件摘要均已接收，未生成新会话，也未增加事实。",
            "tables": skipped,
        }

    conn.execute("BEGIN TRANSACTION")
    try:
        session_id = _create_session(conn, now, label, _catalog_digest(root))
        report = {"session_id": session_id, "status": "received",
                  "created_at": now.isoformat(), "tables": []}
        for table_name, (source, rows, digest) in pending.items():
            receive_id = _insert_receive(conn, session_id, table_name, str(source),
                                         digest, len(rows), now)
            _apply_rows(conn, table_name, rows, str(source), digest, receive_id)
            report["tables"].append({"name": table_name, "status": "received",
                                     "receive_id": receive_id, "rows": len(rows),
                                     "sha256": digest})
        report["tables"].extend(skipped)
        rebuild_facts(conn, session_id, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return report


def _suffix(table_name: str) -> str:
    return {
        "components": "component",
        "kits": "kit",
        "composition_rules": "composition_rule",
        "assembly": "assembly",
        "shipments": "shipment",
        "scans": "scan",
        "quality_events": "quality_event",
    }[table_name]
