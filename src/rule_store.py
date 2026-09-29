"""规则版本的SQLite持久化。

与计划仓储物理分开：规则有自己的版本表和审计表，
乐观并发也独立计数，规则修订不会改动任何在用计划。
"""
import json
import sqlite3
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


class RuleStore:
    def __init__(self, db_path: str, policy: Any) -> None:
        self.db_path = db_path
        self.policy = policy
        self._init_schema()
        self.ensure_baseline()

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
                CREATE TABLE IF NOT EXISTS rule_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version INTEGER NOT NULL UNIQUE,
                    revision INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    service_cap INTEGER NOT NULL,
                    review_cycle_days INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    expected_by TEXT NOT NULL DEFAULT '',
                    effective_at TEXT,
                    created_by TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_version INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rule_status ON rule_versions(status);
                CREATE INDEX IF NOT EXISTS idx_rule_audit ON rule_audit_events(id);
                """
            )
            self._migrate(connection)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(rule_versions)").fetchall()}
        if "revision" not in columns:
            connection.execute("ALTER TABLE rule_versions ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")

    def ensure_baseline(self) -> None:
        """全新库插入v1生效基线；旧库已有数据时幂等跳过。"""
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS total FROM rule_versions").fetchone()["total"]
            if int(count) > 0:
                return
            baseline = self.policy.baseline()
            connection.execute(
                "INSERT INTO rule_versions(version,status,service_cap,review_cycle_days,note,created_by,updated_by,created_at,updated_at,effective_at)"
                " VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
                (
                    baseline["version"],
                    "effective",
                    baseline["service_cap"],
                    baseline["review_cycle_days"],
                    baseline["note"],
                    "system",
                    "system",
                ),
            )
            connection.execute(
                "INSERT INTO rule_audit_events(rule_version,action,actor_id,details,created_at)"
                " VALUES(?,?,?,?,datetime('now'))",
                (
                    baseline["version"],
                    "rule_effective",
                    "system",
                    json.dumps({"effective_at": None, "baseline": True}, ensure_ascii=False, sort_keys=True),
                ),
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["service_cap"] = int(item["service_cap"])
        item["review_cycle_days"] = int(item["review_cycle_days"])
        item["version"] = int(item["version"])
        item["revision"] = int(item["revision"])
        return item

    def list_versions(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM rule_versions ORDER BY version").fetchall()
        return [self._row(row) for row in rows]

    def get_version(self, version: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM rule_versions WHERE version=?", (version,)).fetchone()
        if row is None:
            raise NotFound("规则版本不存在")
        return self._row(row)

    def _audit(self, connection: sqlite3.Connection, version: int, action: str, actor_id: str, details: Dict[str, Any], created_at: str) -> None:
        connection.execute(
            "INSERT INTO rule_audit_events(rule_version,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
            (version, action, actor_id, json.dumps(details, ensure_ascii=False, sort_keys=True), created_at),
        )

    def activate_due(self, now: str) -> List[Dict[str, Any]]:
        """到点生效：把到期的待生效规则按顺序切换为生效，旧生效版本转为被取代。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            due = connection.execute(
                "SELECT * FROM rule_versions WHERE status='scheduled' AND effective_at<=? ORDER BY effective_at, version",
                (now,),
            ).fetchall()
            activated: List[Dict[str, Any]] = []
            for row in due:
                new_rule = self._row(row)
                current = connection.execute("SELECT * FROM rule_versions WHERE status='effective'").fetchone()
                if current is not None:
                    old_rule = self._row(current)
                    connection.execute(
                        "UPDATE rule_versions SET status='superseded',updated_at=? WHERE version=?",
                        (now, old_rule["version"]),
                    )
                    self._audit(
                        connection,
                        old_rule["version"],
                        "rule_superseded",
                        "system",
                        {"by_version": new_rule["version"]},
                        now,
                    )
                connection.execute(
                    "UPDATE rule_versions SET status='effective',updated_at=? WHERE version=?",
                    (now, new_rule["version"]),
                )
                self._audit(
                    connection,
                    new_rule["version"],
                    "rule_effective",
                    "system",
                    {"effective_at": new_rule["effective_at"], "scheduled": True},
                    now,
                )
                activated.append(new_rule)
            connection.commit()
        return activated

    def current(self, now: str) -> Dict[str, Any]:
        self.activate_due(now)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM rule_versions WHERE status='effective'").fetchone()
        if row is None:
            raise NotFound("当前没有生效的规则版本")
        return self._row(row)

    def pending_draft(self, connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM rule_versions WHERE status IN ('draft','scheduled') ORDER BY version DESC LIMIT 1"
        ).fetchone()

    def create_draft(self, config: Dict[str, Any], actor_id: str, now: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self.pending_draft(connection) is not None:
                connection.rollback()
                raise Conflict("已有待发布的规则草稿或待生效版本，请先处理后再新建")
            version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM rule_versions").fetchone()["v"])
            connection.execute(
                "INSERT INTO rule_versions(version,status,service_cap,review_cycle_days,note,created_by,updated_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (version, "draft", config["service_cap"], config["review_cycle_days"], config.get("note", ""), actor_id, actor_id, now, now),
            )
            self._audit(connection, version, "rule_draft_created", actor_id, {"config": config}, now)
            row = connection.execute("SELECT * FROM rule_versions WHERE version=?", (version,)).fetchone()
            connection.commit()
        return self._row(row)

    def revise_draft(self, expected_version: int, expected_revision: int, config: Dict[str, Any], actor_id: str, now: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM rule_versions WHERE status='draft' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                connection.rollback()
                raise Conflict("没有可修订的规则草稿")
            draft = self._row(row)
            if draft["version"] != int(expected_version):
                connection.rollback()
                raise Conflict("规则草稿版本不匹配，请先刷新")
            if draft["revision"] != int(expected_revision):
                connection.rollback()
                raise Conflict("规则草稿已有新修订，请先刷新")
            previous = {"service_cap": draft["service_cap"], "review_cycle_days": draft["review_cycle_days"], "note": draft["note"]}
            connection.execute(
                "UPDATE rule_versions SET service_cap=?,review_cycle_days=?,note=?,revision=revision+1,updated_by=?,updated_at=? WHERE version=?",
                (config["service_cap"], config["review_cycle_days"], config.get("note", ""), actor_id, now, draft["version"]),
            )
            self._audit(
                connection,
                draft["version"],
                "rule_revised",
                actor_id,
                {"revision": draft["revision"] + 1, "diff": self.policy.diff(previous, config)},
                now,
            )
            result = connection.execute("SELECT * FROM rule_versions WHERE version=?", (draft["version"],)).fetchone()
            connection.commit()
        return self._row(result)

    def publish_draft(self, expected_version: int, expected_revision: int, effective_at: Optional[str], actor_id: str, now: str) -> Dict[str, Any]:
        """发布草稿。effective_at为空表示立即生效，否则到点生效。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # 到点的待生效版本先就位，保证状态判断基于最新时间线。
            due = connection.execute(
                "SELECT version FROM rule_versions WHERE status='scheduled' AND effective_at<=? ORDER BY effective_at, version",
                (now,),
            ).fetchall()
            for item in due:
                self._activate_row(connection, int(item["version"]), now, actor_id="system", scheduled=True)

            row = connection.execute(
                "SELECT * FROM rule_versions WHERE status='draft' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                connection.rollback()
                raise Conflict("没有可发布的规则草稿")
            draft = self._row(row)
            if draft["version"] != int(expected_version):
                connection.rollback()
                raise Conflict("规则草稿版本不匹配，请先刷新")
            if draft["revision"] != int(expected_revision):
                connection.rollback()
                raise Conflict("规则已有更新的修订版本，请先刷新后再发布")

            if effective_at is None or effective_at <= now:
                self._activate_row(connection, draft["version"], now, actor_id=actor_id, scheduled=False, publish_details={"effective_at": now})
            else:
                connection.execute(
                    "UPDATE rule_versions SET status='scheduled',effective_at=?,updated_by=?,updated_at=? WHERE version=?",
                    (effective_at, actor_id, now, draft["version"]),
                )
                self._audit(
                    connection,
                    draft["version"],
                    "rule_published",
                    actor_id,
                    {"effective_at": effective_at, "scheduled": True},
                    now,
                )
            result = connection.execute("SELECT * FROM rule_versions WHERE version=?", (draft["version"],)).fetchone()
            connection.commit()
        return self._row(result)

    def _activate_row(
        self,
        connection: sqlite3.Connection,
        version: int,
        now: str,
        actor_id: str,
        scheduled: bool,
        publish_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        draft_row = connection.execute("SELECT * FROM rule_versions WHERE version=?", (version,)).fetchone()
        if draft_row is None:
            raise NotFound("规则版本不存在")
        draft = self._row(draft_row)
        current = connection.execute("SELECT * FROM rule_versions WHERE status='effective'").fetchone()
        if current is not None:
            old_rule = self._row(current)
            connection.execute(
                "UPDATE rule_versions SET status='superseded',updated_at=? WHERE version=?",
                (now, old_rule["version"]),
            )
            self._audit(connection, old_rule["version"], "rule_superseded", actor_id, {"by_version": version}, now)
        if draft["status"] == "draft":
            details = dict(publish_details or {"effective_at": now})
            self._audit(connection, version, "rule_published", actor_id, details, now)
        connection.execute(
            "UPDATE rule_versions SET status='effective',updated_by=?,updated_at=?,"
            "effective_at=COALESCE(effective_at,?) WHERE version=?",
            (actor_id, now, now, version),
        )
        self._audit(
            connection,
            version,
            "rule_effective",
            actor_id,
            {"effective_at": now, "scheduled": scheduled},
            now,
        )

    def discard_draft(self, expected_version: int, expected_revision: int, actor_id: str, now: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM rule_versions WHERE status='draft' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                connection.rollback()
                raise Conflict("没有可放弃的规则草稿")
            draft = self._row(row)
            if draft["version"] != int(expected_version) or draft["revision"] != int(expected_revision):
                connection.rollback()
                raise Conflict("规则草稿已有新修订，请先刷新")
            connection.execute(
                "UPDATE rule_versions SET status='discarded',updated_by=?,updated_at=? WHERE version=?",
                (actor_id, now, draft["version"]),
            )
            self._audit(connection, draft["version"], "rule_discarded", actor_id, {}, now)
            result = connection.execute("SELECT * FROM rule_versions WHERE version=?", (draft["version"],)).fetchone()
            connection.commit()
        return self._row(result)

    def rollback(self, target_version: int, actor_id: str, now: str) -> Dict[str, Any]:
        """回滚不复活旧版本，而是按旧参数生成一个新的生效版本。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target_row = connection.execute("SELECT * FROM rule_versions WHERE version=?", (target_version,)).fetchone()
            if target_row is None:
                connection.rollback()
                raise NotFound("规则版本不存在")
            target = self._row(target_row)
            if target["status"] != "superseded":
                connection.rollback()
                raise Conflict("只能回滚已被取代的历史版本")
            current_row = connection.execute("SELECT * FROM rule_versions WHERE status='effective'").fetchone()
            if current_row is None:
                connection.rollback()
                raise Conflict("当前没有生效规则，无法回滚")
            current = self._row(current_row)
            new_version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM rule_versions").fetchone()["v"])
            connection.execute(
                "INSERT INTO rule_versions(version,status,service_cap,review_cycle_days,note,created_by,updated_by,created_at,updated_at,effective_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    new_version,
                    "effective",
                    target["service_cap"],
                    target["review_cycle_days"],
                    target["note"],
                    actor_id,
                    actor_id,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE rule_versions SET status='superseded',updated_at=? WHERE version=?",
                (now, current["version"]),
            )
            restored = {"service_cap": target["service_cap"], "review_cycle_days": target["review_cycle_days"], "note": target["note"]}
            self._audit(
                connection,
                new_version,
                "rule_rolled_back",
                actor_id,
                {
                    "source_version": target["version"],
                    "from_version": current["version"],
                    "diff_from_current": self.policy.diff(current, restored),
                },
                now,
            )
            self._audit(connection, current["version"], "rule_superseded", actor_id, {"by_version": new_version}, now)
            self._audit(connection, new_version, "rule_effective", actor_id, {"effective_at": now, "rollback": True}, now)
            result = connection.execute("SELECT * FROM rule_versions WHERE version=?", (new_version,)).fetchone()
            connection.commit()
        return self._row(result)

    def audit_timeline(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM rule_audit_events ORDER BY id").fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            items.append(item)
        return items
