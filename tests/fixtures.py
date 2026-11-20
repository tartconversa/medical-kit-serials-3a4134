"""测试辅助：在临时目录里生成一份可装载的合成资料包。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

BASE_COLUMNS = {
    "components": [
        ("component_key", "string"), ("manufacturer", "string"), ("serial", "string"),
        ("class", "string"), ("lot", "string"),
    ],
    "kits": [("kit_key", "string"), ("model", "string"), ("version", "string")],
    "composition_rules": [
        ("rule_id", "string"), ("model", "string"), ("version", "string"), ("class", "string"),
        ("required", "boolean"), ("quantity", "integer"), ("choice_group", "string"),
    ],
    "assembly": [
        ("assembly_id", "string"), ("kit_key", "string"), ("component_key", "string"),
        ("start", "datetime"), ("end", "datetime"), ("basis", "string"),
    ],
    "shipments": [
        ("shipment_id", "string"), ("kit_key", "string"), ("customer", "string"),
        ("kind", "string"), ("at", "datetime"),
    ],
    "scans": [
        ("scan_id", "string"), ("component_key", "string"), ("shipment_id", "string"),
        ("from_location", "string"), ("to_location", "string"), ("at", "datetime"),
    ],
    "quality_events": [
        ("event_id", "string"), ("component_key", "string"), ("state", "string"),
        ("at", "datetime"),
    ],
}

REFERENCES = {
    "assembly": [("kit_key", "kits", "kit_key"), ("component_key", "components", "component_key")],
    "shipments": [("kit_key", "kits", "kit_key")],
    "scans": [("component_key", "components", "component_key"),
              ("shipment_id", "shipments", "shipment_id")],
    "quality_events": [("component_key", "components", "component_key")],
}

KEYS = {
    "components": ["component_key"], "kits": ["kit_key"],
    "composition_rules": ["rule_id"], "assembly": ["assembly_id"],
    "shipments": ["shipment_id"], "scans": ["scan_id"], "quality_events": ["event_id"],
}


def _csv(columns, rows):
    lines = [",".join(c for c, _ in columns)]
    for row in rows:
        lines.append(",".join("" if row.get(c) is None else str(row[c]) for c, _ in columns))
    return "\n".join(lines) + "\n"


def write_pack(root: Path, tables: dict[str, list[dict]]) -> Path:
    """写出资料文件与 catalog.json，返回资料目录。"""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, rows in tables.items():
        columns = BASE_COLUMNS[name]
        path = data / f"{name}.csv"
        text = _csv(columns, rows)
        path.write_text(text, encoding="utf-8", newline="")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entry = {
            "name": name,
            "description": name,
            "columns": [{"name": c, "type": t, "description": c, "nullable": True}
                        for c, t in columns],
            "key": KEYS[name],
            "references": [],
            "path": f"data/{name}.csv",
            "row_count": len(rows),
            "sha256": digest,
        }
        for column, target_table, target_column in REFERENCES.get(name, []):
            entry["references"].append({"column": column, "target_table": target_table,
                                        "target_column": target_column})
        entries.append(entry)
    catalog = {"title": "test", "synthetic": True, "tables": entries}
    (data / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return root


def base_tables() -> dict[str, list[dict]]:
    return {
        "components": [
            {"component_key": "A:1", "manufacturer": "A", "serial": "1", "class": "controller", "lot": "L1"},
            {"component_key": "A:2", "manufacturer": "A", "serial": "2", "class": "pump", "lot": "L2"},
            {"component_key": "A:3", "manufacturer": "A", "serial": "3", "class": "pump", "lot": "L2"},
            {"component_key": "B:1", "manufacturer": "B", "serial": "1", "class": "controller", "lot": "L3"},
            {"component_key": "B:2", "manufacturer": "B", "serial": "2", "class": "pump", "lot": "L4"},
        ],
        "kits": [
            {"kit_key": "A:K1", "model": "M1", "version": "V1"},
            {"kit_key": "B:K1", "model": "M1", "version": "V1"},
        ],
        "composition_rules": [
            {"rule_id": "R1", "model": "M1", "version": "V1", "class": "controller",
             "required": "true", "quantity": "1", "choice_group": ""},
            {"rule_id": "R2", "model": "M1", "version": "V1", "class": "pump",
             "required": "true", "quantity": "1", "choice_group": "pump"},
            {"rule_id": "R3", "model": "M1", "version": "V1", "class": "sensor",
             "required": "false", "quantity": "1", "choice_group": ""},
        ],
        "assembly": [
            {"assembly_id": "AS1", "kit_key": "A:K1", "component_key": "A:1",
             "start": "2026-10-01T08:00:00+08:00", "end": "", "basis": "permanent"},
            {"assembly_id": "AS2", "kit_key": "A:K1", "component_key": "A:2",
             "start": "2026-10-01T08:00:00+08:00", "end": "2026-10-12T08:00:00+08:00",
             "basis": "permanent"},
            {"assembly_id": "AS3", "kit_key": "A:K1", "component_key": "A:3",
             "start": "2026-10-13T08:00:00+08:00", "end": "2026-10-20T08:00:00+08:00",
             "basis": "loan"},
            {"assembly_id": "AS4", "kit_key": "A:K1", "component_key": "A:2",
             "start": "2026-10-21T08:00:00+08:00", "end": "", "basis": "permanent"},
            {"assembly_id": "AS5", "kit_key": "B:K1", "component_key": "B:1",
             "start": "2026-10-01T08:00:00+08:00", "end": "", "basis": "permanent"},
            {"assembly_id": "AS6", "kit_key": "B:K1", "component_key": "B:2",
             "start": "2026-10-01T08:00:00+08:00", "end": "", "basis": "permanent"},
        ],
        "shipments": [
            {"shipment_id": "S1", "kit_key": "A:K1", "customer": "C1", "kind": "sale",
             "at": "2026-10-05T08:00:00+08:00"},
            {"shipment_id": "S2", "kit_key": "B:K1", "customer": "C2", "kind": "sale",
             "at": "2026-10-06T08:00:00+08:00"},
        ],
        "scans": [
            {"scan_id": "X1", "component_key": "A:1", "shipment_id": "S1",
             "from_location": "store", "to_location": "C1", "at": "2026-10-05T08:00:00+08:00"},
            {"scan_id": "X2", "component_key": "A:2", "shipment_id": "S1",
             "from_location": "store", "to_location": "C1", "at": "2026-10-05T08:00:00+08:00"},
            {"scan_id": "X3", "component_key": "B:1", "shipment_id": "S2",
             "from_location": "store", "to_location": "C2", "at": "2026-10-06T08:00:00+08:00"},
            {"scan_id": "X4", "component_key": "B:2", "shipment_id": "S2",
             "from_location": "store", "to_location": "C2", "at": "2026-10-06T08:00:00+08:00"},
            {"scan_id": "X5", "component_key": "A:2", "shipment_id": "",
             "from_location": "C1", "to_location": "repair", "at": "2026-10-12T08:00:00+08:00"},
            {"scan_id": "X6", "component_key": "A:3", "shipment_id": "",
             "from_location": "store", "to_location": "C1", "at": "2026-10-13T08:00:00+08:00"},
            {"scan_id": "X7", "component_key": "A:2", "shipment_id": "",
             "from_location": "repair", "to_location": "store", "at": "2026-10-19T08:00:00+08:00"},
            {"scan_id": "X8", "component_key": "A:3", "shipment_id": "",
             "from_location": "C1", "to_location": "store", "at": "2026-10-20T08:00:00+08:00"},
            {"scan_id": "X9", "component_key": "A:2", "shipment_id": "",
             "from_location": "store", "to_location": "C1", "at": "2026-10-21T08:00:00+08:00"},
        ],
        "quality_events": [
            {"event_id": "Q0", "component_key": "A:1", "state": "working", "at": "2026-10-01T08:00:00+08:00"},
            {"event_id": "Q1", "component_key": "A:2", "state": "working", "at": "2026-10-01T08:00:00+08:00"},
            {"event_id": "Q2", "component_key": "A:3", "state": "working", "at": "2026-10-01T08:00:00+08:00"},
            {"event_id": "Q3", "component_key": "B:1", "state": "working", "at": "2026-10-01T08:00:00+08:00"},
            {"event_id": "Q4", "component_key": "B:2", "state": "working", "at": "2026-10-01T08:00:00+08:00"},
            {"event_id": "Q5", "component_key": "A:2", "state": "nonworking", "at": "2026-10-12T08:00:00+08:00"},
            {"event_id": "Q6", "component_key": "A:2", "state": "working", "at": "2026-10-19T08:00:00+08:00"},
        ],
    }
