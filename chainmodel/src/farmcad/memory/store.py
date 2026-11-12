from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from farmcad.data.schema import ROLES, UPDATES, label, unknown_value, validate_design
from farmcad.utils import object_hash, require_keys

VERSION = "farmcad-memory-v1"


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class MemoryStore:
    """One design and one append-only plan branch per database; no tensor is authoritative."""

    def __init__(self, path: Path):
        if not path.is_file():
            raise FileNotFoundError(path)
        self.connection = sqlite3.connect(str(path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if self.meta("version") != VERSION:
            self.close()
            raise ValueError("Unsupported memory schema")

    @classmethod
    def create(cls, path: Path, design: dict) -> MemoryStore:
        validate_design(design)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE operations (id TEXT PRIMARY KEY, sequence INTEGER UNIQUE, name TEXT NOT NULL);
                CREATE TABLE states (
                    id TEXT PRIMARY KEY, operation_id TEXT REFERENCES operations(id),
                    parameter TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE bindings (
                    state_id TEXT REFERENCES states(id), feature_id TEXT NOT NULL,
                    PRIMARY KEY(state_id, feature_id));
                CREATE TABLE sources (
                    child TEXT REFERENCES states(id), parent TEXT REFERENCES states(id),
                    PRIMARY KEY(child, parent));
                CREATE TABLE audit (
                    id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE requests (
                    id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, packet TEXT NOT NULL,
                    response TEXT, result TEXT);
            """)
            values = {
                "version": VERSION, "design": design, "design_hash": object_hash(design),
                "plan_id": str(uuid.uuid4()), "revision": 0, "closed": False,
            }
            connection.executemany("INSERT INTO meta VALUES (?, ?)", [(k, dump(v)) for k, v in values.items()])
            connection.commit()
        finally:
            connection.close()
        return cls(path)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            yield
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def meta(self, key: str):
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise ValueError(f"Missing memory metadata: {key}")
        return json.loads(row["value"])

    def set_meta(self, key: str, value) -> None:
        self.connection.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, dump(value)))

    def audit(self, kind: str, value: dict) -> None:
        self.connection.execute("INSERT INTO audit(revision, kind, payload) VALUES (?, ?, ?)",
                                (self.meta("revision"), kind, dump(value)))

    def check_revision(self, revision: int) -> None:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision != self.meta("revision"):
            raise ValueError("Stale or invalid plan revision; refresh the request")
        if self.meta("closed"):
            raise ValueError("This plan is closed; start a separate plan for a new branch")

    def operations(self) -> list[dict]:
        return [dict(r) for r in self.connection.execute("SELECT * FROM operations ORDER BY sequence")]

    def states(self, feature_id: str = None, parameter: str = None) -> list[dict]:
        clauses, args = [], []
        if feature_id is not None:
            if not isinstance(feature_id, str):
                raise ValueError("feature_id must be a string or null")
            if feature_id not in {f["id"] for f in self.meta("design")["features"]}:
                raise ValueError("Unknown feature query")
            clauses.append("EXISTS (SELECT 1 FROM bindings b WHERE b.state_id = s.id AND b.feature_id = ?)")
            args.append(feature_id)
        if parameter is not None:
            if not isinstance(parameter, str):
                raise ValueError("parameter must be a string or null")
            clauses.append("s.parameter = ?")
            args.append(parameter)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [json.loads(r["payload"]) for r in self.connection.execute(
            "SELECT s.payload FROM states s JOIN operations o ON s.operation_id=o.id"
            + where + " ORDER BY o.sequence, s.rowid", args)]

    def record(self, kind: str, identity: str) -> dict:
        if not isinstance(kind, str) or not isinstance(identity, str):
            raise ValueError("Record kind and ID must be strings")
        if kind in ("feature", "requirement", "entity"):
            key = {"feature": "features", "requirement": "requirements", "entity": "entities"}[kind]
            for record in self.meta("design")[key]:
                if record["id"] == identity:
                    return record
        elif kind == "state":
            row = self.connection.execute("SELECT payload FROM states WHERE id = ?", (identity,)).fetchone()
            if row:
                return json.loads(row["payload"])
        raise ValueError(f"Unknown {kind} record: {identity}")

    def current_states(self, feature_id: str) -> dict:
        current = {}
        for record in self.states(feature_id):
            if record["kind"] == "planned_change":
                current[record["parameter"]] = record["id"]
        return current

    def commit_operation(self, revision: int, operation: str, parameters: list[dict]) -> dict:
        with self.transaction():
            self.check_revision(revision)
            if not isinstance(operation, str) or not operation.strip():
                raise ValueError("Operation name is required")
            if not isinstance(parameters, list):
                raise ValueError("parameters must be a list")
            design = self.meta("design")
            feature_ids = {f["id"] for f in design["features"]}
            requirements = {r["id"]: r for r in design["requirements"]}
            sequence = len(self.operations())
            operation_id = f"op:{sequence}"
            records, changes = [], set()
            for index, p in enumerate(parameters):
                if not isinstance(p, dict):
                    raise ValueError("Each parameter must be an object")
                keys = {"parameter", "feature_ids", "requirement_ids", "source_state_ids", "roles", "update"}
                require_keys(p, keys, keys, "operation parameter")
                if not isinstance(p["parameter"], str) or not p["parameter"].strip():
                    raise ValueError("Parameter name is required")
                for key in ("feature_ids", "requirement_ids", "source_state_ids", "roles"):
                    if not isinstance(p[key], list) or any(not isinstance(v, str) for v in p[key]) or len(set(p[key])) != len(p[key]):
                        raise ValueError(f"{key} must be a unique string list")
                if any(f not in feature_ids for f in p["feature_ids"]):
                    raise ValueError("Unknown feature binding")
                if any(r not in requirements or requirements[r]["feature_id"] not in p["feature_ids"] for r in p["requirement_ids"]):
                    raise ValueError("Requirement must belong to a bound feature")
                if not p["roles"] or any(r not in ROLES for r in p["roles"]) or p["update"] not in UPDATES:
                    raise ValueError("Invalid role or update")
                parents = [self.record("state", identity) for identity in p["source_state_ids"]]
                if any(r["operation_index"] >= sequence for r in parents):
                    raise ValueError("Source must precede this operation")
                value, status = unknown_value(), "requires_rule_execution_or_review"
                if p["update"] == "design_copy":
                    if len(p["feature_ids"]) != 1 or parents:
                        raise ValueError("design_copy requires one feature and no state sources")
                    value = dict(self.record("feature", p["feature_ids"][0])["value"])
                    status = "copied_design_candidate_not_engineering_verified"
                elif p["update"] == "state_copy":
                    if len(parents) != 1 or not p["feature_ids"]:
                        raise ValueError("state_copy requires one state and a bound feature")
                    parent = parents[0]
                    if set(parent["feature_ids"]) != set(p["feature_ids"]) or parent["parameter"] != p["parameter"]:
                        raise ValueError("State copy requires matching object bindings and parameter")
                    value = dict(parent["value"])
                    status = "copied_state_not_engineering_verified"
                elif p["update"] == "external" and parents:
                    raise ValueError("External source cannot masquerade as an internal state")
                kind = "planned_change" if "machining_target" in p["roles"] else (
                    "planned_observation" if "inspection_target" in p["roles"] else "reference")
                if kind == "planned_change" and not p["feature_ids"]:
                    raise ValueError("A planned object change requires an object binding")
                if kind == "planned_change":
                    keys = {(f, p["parameter"]) for f in p["feature_ids"]}
                    if changes.intersection(keys):
                        raise ValueError("Conflicting writes to the same object attribute in one operation")
                    changes.update(keys)
                records.append({
                    **p, "id": f"state:{sequence}:{index}", "operation_id": operation_id,
                    "plan_id": self.meta("plan_id"),
                    "operation": operation, "operation_index": sequence, "kind": kind,
                    "value": value, "value_status": status, "quality": "model_proposal",
                    "engineering_verified": False,
                })
            self.connection.execute("INSERT INTO operations VALUES (?, ?, ?)", (operation_id, sequence, operation))
            for record in records:
                self.connection.execute("INSERT INTO states VALUES (?, ?, ?, ?)",
                                        (record["id"], operation_id, record["parameter"], dump(record)))
                self.connection.executemany("INSERT INTO bindings VALUES (?, ?)",
                                            [(record["id"], f) for f in record["feature_ids"]])
                self.connection.executemany("INSERT INTO sources VALUES (?, ?)",
                                            [(record["id"], s) for s in record["source_state_ids"]])
            self.set_meta("revision", revision + 1)
            result = {"operation_id": operation_id, "state_ids": [r["id"] for r in records],
                      "revision": revision + 1, "engineering_verified": False}
            self.audit("commit_operation", result)
            return result

    def close_plan(self, revision: int) -> dict:
        with self.transaction():
            self.check_revision(revision)
            if not self.operations():
                raise ValueError("Cannot close an empty route")
            self.set_meta("closed", True)
            self.set_meta("revision", revision + 1)
            result = {
                "status": "proposal_closed_pending_review", "engineering_verified": False,
                "unverified_requirement_ids": [r["id"] for r in self.meta("design")["requirements"]],
            }
            self.audit("close_plan", result)
            return result

    def reject(self, error: ValueError, response: dict) -> None:
        logging.getLogger("farmcad.hybrid").warning("Decision rejected: %s", error)
        self.audit("rejected_decision", {"error": str(error), "response": response})


def model_records(records: list[dict]) -> list[dict]:
    return [{
        **record,
        "labels": {key: label(record[field]) for key, field in (
            ("features", "feature_ids"), ("requirements", "requirement_ids"), ("sources", "source_state_ids"),
            ("roles", "roles"), ("update", "update"), ("value", "value"),
        )},
    } for record in records]
