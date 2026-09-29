"""SQLite 表结构与事务访问。

除计划（records）外，还持久化区级规则版本（rule_versions）与规则审计（rule_events）。
计划与规则各用一套表，互不耦合；规则草稿用 revision 做乐观并发。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound
from .rule_versioning import (
    BASELINE_VERSION_NO,
    DEFAULT_REVIEW_CYCLE_DAYS,
    DEFAULT_SERVICE_CAP,
    DRAFT,
    EFFECTIVE,
    SCHEDULED,
    SUPERSEDED,
    RuleVersioning,
    to_iso,
    utc_now,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str, clock: Callable[[], datetime] = None) -> None:
        self.db_path = db_path
        self.clock = clock or utc_now
        self.rule_logic = RuleVersioning()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_no INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    service_cap INTEGER NOT NULL,
                    review_cycle_days INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    effective_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_by TEXT,
                    published_at TEXT,
                    publish_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS rule_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_no INTEGER,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_rule_open_draft
                    ON rule_versions(status) WHERE status = 'draft';
                CREATE INDEX IF NOT EXISTS idx_rule_status ON rule_versions(status);
                CREATE INDEX IF NOT EXISTS idx_rule_events_no ON rule_events(version_no, id);
                """
            )
            self._seed_baseline(connection)
            self._backfill_rule_snapshots(connection)

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def _seed_baseline(self, connection: sqlite3.Connection) -> None:
        """空库锚定一个基线生效版本；已有库不动。"""
        exists = connection.execute("SELECT COUNT(*) AS total FROM rule_versions").fetchone()
        if int(exists["total"]) > 0:
            return
        now = to_iso(self.clock())
        connection.execute(
            "INSERT INTO rule_versions(version_no,status,service_cap,review_cycle_days,revision,effective_at,created_by,created_at,published_by,published_at,publish_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                BASELINE_VERSION_NO,
                EFFECTIVE,
                DEFAULT_SERVICE_CAP,
                DEFAULT_REVIEW_CYCLE_DAYS,
                1,
                now,
                "system",
                now,
                "system",
                now,
                "初始基线规则",
            ),
        )
        connection.execute(
            "INSERT INTO rule_events(version_no,action,actor_id,revision,details,created_at) VALUES(?,?,?,?,?,?)",
            (
                BASELINE_VERSION_NO,
                "baseline",
                "system",
                1,
                json.dumps(
                    {"service_cap": DEFAULT_SERVICE_CAP, "review_cycle_days": DEFAULT_REVIEW_CYCLE_DAYS},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )

    def _backfill_rule_snapshots(self, connection: sqlite3.Connection) -> None:
        """旧库计划没有规则快照：补一份原规则快照（以基线版本与计划自身建档值为准）。"""
        baseline = connection.execute(
            "SELECT * FROM rule_versions WHERE version_no=?", (BASELINE_VERSION_NO,)
        ).fetchone()
        rows = connection.execute("SELECT id, payload FROM records").fetchall()
        touched = 0
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("rule_snapshot"):
                continue
            payload["rule_snapshot"] = RuleVersioning().snapshot(
                dict(baseline), to_iso(self.clock()), backfilled=True
            )
            connection.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["id"]))
            touched += 1
        if touched:
            connection.execute(
                "INSERT INTO rule_events(version_no,action,actor_id,revision,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    BASELINE_VERSION_NO,
                    "snapshot_backfill",
                    "system",
                    1,
                    json.dumps({"backfilled_records": touched}, ensure_ascii=False, sort_keys=True),
                    to_iso(self.clock()),
                ),
            )

    # ----------------------------- 计划 -----------------------------

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                snapshot = payload.get("rule_snapshot") or {}
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        record_id,
                        "created",
                        actor_id,
                        1,
                        json.dumps({"state": state, "rule_version_no": snapshot.get("version_no")}, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ----------------------------- 规则版本 -----------------------------

    @staticmethod
    def _rule_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def _get_rule_row(self, connection: sqlite3.Connection, rule_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM rule_versions WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise NotFound("规则草稿不存在")
        return row

    def _insert_rule_event(
        self,
        connection: sqlite3.Connection,
        version_no: Optional[int],
        action: str,
        actor_id: str,
        revision: int,
        details: Dict[str, Any],
        created_at: str = None,
    ) -> None:
        connection.execute(
            "INSERT INTO rule_events(version_no,action,actor_id,revision,details,created_at) VALUES(?,?,?,?,?,?)",
            (
                version_no,
                action,
                actor_id,
                revision,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                created_at or to_iso(self.clock()),
            ),
        )

    def _rule_diff(self, connection: sqlite3.Connection, new_values: Dict[str, Any], exclude_version_no: int = None) -> Dict[str, Dict[str, int]]:
        query = "SELECT * FROM rule_versions WHERE status=?"
        params: List[Any] = [EFFECTIVE]
        if exclude_version_no is not None:
            query += " AND version_no<>?"
            params.append(exclude_version_no)
        query += " ORDER BY version_no DESC LIMIT 1"
        previous = connection.execute(query, params).fetchone()
        if previous is None:
            return {}
        return self.rule_logic.diff_values(dict(previous), new_values)

    def create_rule_draft(self, values: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = to_iso(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            open_draft = connection.execute("SELECT id FROM rule_versions WHERE status=?", (DRAFT,)).fetchone()
            if open_draft is not None:
                connection.rollback()
                raise Conflict("已有待发布的规则草稿，请刷新后处理")
            number_row = connection.execute("SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no FROM rule_versions").fetchone()
            version_no = int(number_row["next_no"])
            cursor = connection.execute(
                "INSERT INTO rule_versions(version_no,status,service_cap,review_cycle_days,revision,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (version_no, DRAFT, values["service_cap"], values["review_cycle_days"], 1, actor_id, now),
            )
            rule_id = int(cursor.lastrowid)
            self._insert_rule_event(
                connection,
                version_no,
                "draft_created",
                actor_id,
                1,
                {"service_cap": values["service_cap"], "review_cycle_days": values["review_cycle_days"], "reason": values.get("reason", "")},
                now,
            )
            row = self._get_rule_row(connection, rule_id)
            connection.commit()
        return self._rule_row(row)

    def revise_rule_draft(self, rule_id: int, expected_revision: int, values: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = to_iso(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_rule_row(connection, rule_id)
            if str(row["status"]) != DRAFT:
                connection.rollback()
                raise Conflict("草稿已发布，不能再修订，请刷新")
            if int(row["revision"]) != int(expected_revision):
                connection.rollback()
                raise Conflict("草稿已被他人修改，请先刷新后再提交")
            revision = int(row["revision"]) + 1
            connection.execute(
                "UPDATE rule_versions SET service_cap=?,review_cycle_days=?,revision=? WHERE id=?",
                (values["service_cap"], values["review_cycle_days"], revision, rule_id),
            )
            self._insert_rule_event(
                connection,
                int(row["version_no"]),
                "draft_revised",
                actor_id,
                revision,
                {
                    "service_cap": values["service_cap"],
                    "review_cycle_days": values["review_cycle_days"],
                    "reason": values.get("reason", ""),
                    "from_revision": int(row["revision"]),
                },
                now,
            )
            result = self._get_rule_row(connection, rule_id)
            connection.commit()
        return self._rule_row(result)

    def publish_rule_draft(self, rule_id: int, expected_revision: int, effective_at: str, reason: str, actor_id: str) -> Dict[str, Any]:
        now = to_iso(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_rule_row(connection, rule_id)
            if str(row["status"]) != DRAFT:
                connection.rollback()
                raise Conflict("草稿已发布，不能重复发布，请刷新")
            if int(row["revision"]) != int(expected_revision):
                connection.rollback()
                raise Conflict("草稿在发布前已被修订，晚到的提交失败，请先刷新")
            revision = int(row["revision"]) + 1
            new_values = {"service_cap": int(row["service_cap"]), "review_cycle_days": int(row["review_cycle_days"])}
            diff = {}
            immediate = self.rule_logic.is_due(effective_at, self.clock())
            status = EFFECTIVE if immediate else SCHEDULED
            if immediate:
                diff = self._rule_diff(connection, new_values, exclude_version_no=int(row["version_no"]))
                connection.execute("UPDATE rule_versions SET status=? WHERE status=?", (SUPERSEDED, EFFECTIVE))
            connection.execute(
                "UPDATE rule_versions SET status=?,revision=?,effective_at=?,published_by=?,published_at=?,publish_reason=? WHERE id=?",
                (status, revision, effective_at, actor_id, now, reason, rule_id),
            )
            self._insert_rule_event(
                connection,
                int(row["version_no"]),
                "published" if immediate else "scheduled",
                actor_id,
                revision,
                {"status": status, "effective_at": effective_at, "reason": reason, "diff": diff},
                now,
            )
            result = self._get_rule_row(connection, rule_id)
            connection.commit()
        return self._rule_row(result)

    def activate_due_rules(self) -> List[Dict[str, Any]]:
        """让到点的定时版本生效（惰性调度，可被任意读操作触发）。"""
        now_moment = self.clock()
        activated: List[Dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            due = connection.execute(
                "SELECT * FROM rule_versions WHERE status=? AND effective_at<=? ORDER BY effective_at, version_no",
                (SCHEDULED, to_iso(now_moment)),
            ).fetchall()
            for row in due:
                # 已有更新的生效版本时，这个迟到的定时版本不再生效，直接归档
                current = connection.execute(
                    "SELECT MAX(version_no) AS max_no FROM rule_versions WHERE status=?", (EFFECTIVE,)
                ).fetchone()
                if current["max_no"] is not None and int(current["max_no"]) > int(row["version_no"]):
                    connection.execute("UPDATE rule_versions SET status=? WHERE id=?", (SUPERSEDED, int(row["id"])))
                    self._insert_rule_event(
                        connection,
                        int(row["version_no"]),
                        "overtaken",
                        str(row["published_by"] or "system"),
                        int(row["revision"]),
                        {"effective_at": row["effective_at"], "effective_version_no": int(current["max_no"])},
                    )
                    continue
                new_values = {"service_cap": int(row["service_cap"]), "review_cycle_days": int(row["review_cycle_days"])}
                diff = self._rule_diff(connection, new_values, exclude_version_no=int(row["version_no"]))
                connection.execute("UPDATE rule_versions SET status=? WHERE status=?", (SUPERSEDED, EFFECTIVE))
                connection.execute("UPDATE rule_versions SET status=? WHERE id=?", (EFFECTIVE, int(row["id"])))
                self._insert_rule_event(
                    connection,
                    int(row["version_no"]),
                    "activated",
                    str(row["published_by"] or "system"),
                    int(row["revision"]),
                    {"effective_at": row["effective_at"], "diff": diff},
                )
                activated.append(self._rule_row(connection.execute("SELECT * FROM rule_versions WHERE id=?", (int(row["id"]),)).fetchone()))
            connection.commit()
        return activated

    def rollback_rule(self, target_version_no: int, reason: str, actor_id: str) -> Dict[str, Any]:
        """回滚：复制某个旧版本的值生成新的生效版本，旧版本原样保留。"""
        now = to_iso(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute("SELECT * FROM rule_versions WHERE version_no=?", (target_version_no,)).fetchone()
            if target is None:
                connection.rollback()
                raise NotFound("回滚的规则版本不存在")
            if str(target["status"]) not in {EFFECTIVE, SUPERSEDED}:
                connection.rollback()
                raise Conflict("只能回滚已生效过的规则版本")
            pending = connection.execute(
                "SELECT status FROM rule_versions WHERE status IN (?, ?) LIMIT 1", (DRAFT, SCHEDULED)
            ).fetchone()
            if pending is not None:
                connection.rollback()
                raise Conflict("存在未生效的草稿或定时版本，请先处理后再回滚")
            next_no = int(connection.execute("SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no FROM rule_versions").fetchone()["next_no"])
            values = {"service_cap": int(target["service_cap"]), "review_cycle_days": int(target["review_cycle_days"])}
            diff = self._rule_diff(connection, values)
            connection.execute("UPDATE rule_versions SET status=? WHERE status=?", (SUPERSEDED, EFFECTIVE))
            cursor = connection.execute(
                "INSERT INTO rule_versions(version_no,status,service_cap,review_cycle_days,revision,effective_at,created_by,created_at,published_by,published_at,publish_reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (next_no, EFFECTIVE, values["service_cap"], values["review_cycle_days"], 1, now, actor_id, now, actor_id, now, reason),
            )
            self._insert_rule_event(
                connection,
                next_no,
                "rollback",
                actor_id,
                1,
                {"rollback_from": target_version_no, "reason": reason, "diff": diff, **values},
                now,
            )
            result = self._rule_row(self._get_rule_row(connection, int(cursor.lastrowid)))
            connection.commit()
        return result

    def list_rules(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM rule_versions ORDER BY version_no DESC").fetchall()
        return [self._rule_row(row) for row in rows]

    def get_rule(self, version_no: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM rule_versions WHERE version_no=?", (version_no,)).fetchone()
        if row is None:
            raise NotFound("规则版本不存在")
        return self._rule_row(row)

    def current_rule(self) -> Dict[str, Any]:
        self.activate_due_rules()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM rule_versions WHERE status=? ORDER BY version_no DESC LIMIT 1",
                (EFFECTIVE,),
            ).fetchone()
        if row is None:
            raise NotFound("当前没有生效的规则版本")
        return self._rule_row(row)

    def rule_timeline(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM rule_events ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def combined_timeline(self) -> List[Dict[str, Any]]:
        """规则与计划的统一时间线：按时间、再按来源排序。"""
        with self._connect() as connection:
            plan_rows = connection.execute("SELECT * FROM audit_events").fetchall()
            rule_rows = connection.execute("SELECT * FROM rule_events").fetchall()
        events: List[Dict[str, Any]] = []
        for row in plan_rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            item["source"] = "plan"
            events.append(item)
        for row in rule_rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            item["source"] = "rule"
            events.append(item)
        events.sort(key=lambda item: (item["created_at"], 0 if item["source"] == "rule" else 1, item["id"]))
        return events
