"""增量去重（SHA-256 内容指纹）与三阶段任务队列（断点续爬）。

参考 gaokao-vault 的采集思路：
  - 三阶段编排：discover（发现目标）→ fetch（抓取原文）→ extract（结构化/大模型抽取）
  - 每个任务以 (stage, target, task_key) 为幂等键落库，进程被杀后重跑只会
    继续 pending/failed 的任务，已完成的不会重复抓。
  - 内容指纹变化才算「新内容」；只有新内容才落快照、才可能触发大模型调用。

线程安全：采集走线程池，所有线程共用一条 SQLite 连接（check_same_thread=False），
因此每个方法内部都用 db.write_lock() 把读写串行化。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from config import settings
from crawler.base import content_fingerprint, sha256_bytes
from db import write_lock


@dataclass
class ChangeResult:
    url: str
    changed: bool
    fingerprint: str
    first_seen: bool
    change_count: int


class ContentStore:
    """内容指纹台账 + 原始快照落盘。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        settings.ensure_dirs()

    # ------------------------------------------------------------------
    def check(
        self,
        url: str,
        html: str,
        http_status: int = 200,
        extra_noise: tuple[tuple[str, str], ...] = (),
    ) -> ChangeResult:
        """比较指纹，登记变更。不落快照。"""
        fp = content_fingerprint(html, extra_noise)
        size = len(html.encode("utf-8", "ignore"))

        with write_lock():
            row = self.conn.execute(
                "SELECT content_hash, change_count FROM crawl_hashes WHERE url = ?", (url,)
            ).fetchone()

            if row is None:
                self.conn.execute(
                    """INSERT INTO crawl_hashes
                       (url, content_hash, byte_size, http_status, change_count, last_changed)
                       VALUES (?, ?, ?, ?, 0, datetime('now','localtime'))""",
                    (url, fp, size, http_status),
                )
                self.conn.commit()
                return ChangeResult(url, True, fp, True, 0)

            if row["content_hash"] != fp:
                count = int(row["change_count"]) + 1
                self.conn.execute(
                    """UPDATE crawl_hashes
                          SET content_hash = ?, byte_size = ?, http_status = ?,
                              change_count = ?, last_seen = datetime('now','localtime'),
                              last_changed = datetime('now','localtime')
                        WHERE url = ?""",
                    (fp, size, http_status, count, url),
                )
                self.conn.commit()
                return ChangeResult(url, True, fp, False, count)

            self.conn.execute(
                """UPDATE crawl_hashes
                      SET last_seen = datetime('now','localtime'), http_status = ?
                    WHERE url = ?""",
                (http_status, url),
            )
            self.conn.commit()
            return ChangeResult(url, False, fp, False, int(row["change_count"]))

    # ------------------------------------------------------------------
    def save_raw(self, url: str, html: str, fingerprint: str) -> Path:
        """按内容哈希落盘原始快照（相同内容只存一份）。"""
        path = settings.RAW_DIR / f"{fingerprint[:2]}" / f"{fingerprint}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(html, encoding="utf-8")
        with write_lock():
            self.conn.execute(
                """INSERT OR IGNORE INTO raw_documents (url, content_hash, path)
                   VALUES (?, ?, ?)""",
                (url, fingerprint, str(path.relative_to(settings.ROOT))),
            )
            self.conn.commit()
        return path

    # ------------------------------------------------------------------
    def ingest(
        self,
        url: str,
        html: str,
        http_status: int = 200,
        *,
        save: bool = True,
        extra_noise: tuple[tuple[str, str], ...] = (),
    ) -> ChangeResult:
        """check + 落快照 的合并入口。"""
        result = self.check(url, html, http_status, extra_noise)
        if result.changed and save:
            self.save_raw(url, html, result.fingerprint)
        return result

    def known(self, url: str) -> str | None:
        with write_lock():
            row = self.conn.execute(
                "SELECT content_hash FROM crawl_hashes WHERE url = ?", (url,)
            ).fetchone()
        return row["content_hash"] if row else None


# --------------------------------------------------------------------------- 任务队列

@dataclass
class Task:
    task_id: int
    stage: str
    target: str
    task_key: str
    payload: dict[str, Any]
    attempts: int


class TaskQueue:
    """三阶段任务队列，支持断点续爬。"""

    def __init__(self, conn: sqlite3.Connection, max_attempts: int = 3) -> None:
        self.conn = conn
        self.max_attempts = max_attempts

    def enqueue(
        self,
        stage: str,
        target: str,
        task_key: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """登记任务。已存在则保留原状态（不重置已完成的进度）。"""
        with write_lock():
            self.conn.execute(
                """INSERT INTO crawl_tasks (stage, target, task_key, payload)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (stage, target, task_key) DO UPDATE SET
                       payload = COALESCE(excluded.payload, crawl_tasks.payload),
                       updated_at = datetime('now','localtime')""",
                (stage, target, task_key, json.dumps(payload or {}, ensure_ascii=False)),
            )
            self.conn.commit()

    def recover_running(self, stage: str | None = None, target: str | None = None) -> int:
        """把残留的 running 任务放回 pending。

        进程被 kill / 异常退出时，已 claim 的任务会停在 running；
        重跑时若不清理，这些任务永远不会再被 pickup（排队会慢慢卡死）。
        每次 run 开头调用一次。
        """
        sql = ("UPDATE crawl_tasks SET status='pending', "
               "updated_at=datetime('now','localtime') WHERE status='running'")
        params: list[Any] = []
        if stage:
            sql += " AND stage = ?"
            params.append(stage)
        if target:
            sql += " AND target = ?"
            params.append(target)
        with write_lock():
            cur = self.conn.execute(sql, params)
            self.conn.commit()
        return cur.rowcount

    def reset(self, stage: str | None = None, target: str | None = None) -> int:
        sql = ("UPDATE crawl_tasks SET status='pending', attempts=0, last_error=NULL "
               "WHERE status IN ('failed','running','done','skipped')")
        params: list[Any] = []
        if stage:
            sql += " AND stage = ?"
            params.append(stage)
        if target:
            sql += " AND target = ?"
            params.append(target)
        with write_lock():
            cur = self.conn.execute(sql, params)
            self.conn.commit()
        return cur.rowcount

    def claim(self, stage: str, target: str, limit: int = 100) -> list[Task]:
        """取出一批待办任务并置为 running。"""
        with write_lock():
            rows = self.conn.execute(
                """SELECT * FROM crawl_tasks
                    WHERE stage = ? AND target = ?
                      AND status IN ('pending', 'failed')
                      AND attempts < ?
                    ORDER BY task_id LIMIT ?""",
                (stage, target, self.max_attempts, limit),
            ).fetchall()
            tasks = [
                Task(
                    task_id=r["task_id"],
                    stage=r["stage"],
                    target=r["target"],
                    task_key=r["task_key"],
                    payload=json.loads(r["payload"] or "{}"),
                    attempts=r["attempts"],
                )
                for r in rows
            ]
            for task in tasks:
                self.conn.execute(
                    """UPDATE crawl_tasks
                          SET status='running', attempts = attempts + 1,
                              updated_at = datetime('now','localtime')
                        WHERE task_id = ?""",
                    (task.task_id,),
                )
            self.conn.commit()
            return tasks

    def mark(self, task_id: int, status: str, error: str | None = None) -> None:
        with write_lock():
            self.conn.execute(
                """UPDATE crawl_tasks SET status = ?, last_error = ?,
                          updated_at = datetime('now','localtime')
                    WHERE task_id = ?""",
                (status, error, task_id),
            )
            self.conn.commit()

    def release(self, tasks: "list[Task]") -> int:
        """把已 claim 但本轮不处理的任务放回 pending。

        claim() 会立刻把状态置为 running；如果调用方随后按条件筛掉一部分
        （例如「本次只抓这几所学校」），被筛掉的任务会永远卡在 running 而不再
        被 pickup。所以筛完必须显式放回，并把 attempts 退回一次。
        """
        if not tasks:
            return 0
        with write_lock():
            for task in tasks:
                self.conn.execute(
                    """UPDATE crawl_tasks
                          SET status = 'pending',
                              attempts = MAX(attempts - 1, 0),
                              updated_at = datetime('now','localtime')
                        WHERE task_id = ? AND status = 'running'""",
                    (task.task_id,),
                )
            self.conn.commit()
        return len(tasks)

    def finish(self, task: Task, status: str = "done", error: str | None = None) -> None:
        self.mark(task.task_id, status, error)

    def stats(self) -> list[sqlite3.Row]:
        with write_lock():
            return list(
                self.conn.execute(
                    """SELECT stage, target, status, COUNT(*) AS n
                         FROM crawl_tasks GROUP BY stage, target, status
                        ORDER BY stage, target, status"""
                )
            )


def snapshot_hash(path: Path) -> str:
    return sha256_bytes(path.read_bytes())
