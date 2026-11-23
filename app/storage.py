"""草案的 SQLite 持久化。

只保存「有候选」的草案（已证最优或仅找到可行纹样）。已证无解/待定不建草案。
线程模型：FastAPI 的同步端点跑在线程池里，因此用一把进程内锁保护写操作，
SQLite 自身打开 ``check_same_thread=False`` 并启用 WAL。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .images import RGB
from .solver import SolveResult

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "drafts.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    unit_w INTEGER NOT NULL,
    unit_h INTEGER NOT NULL,
    status TEXT NOT NULL,
    score INTEGER,
    upper_bound INTEGER,
    elapsed_seconds REAL NOT NULL,
    solver_name TEXT NOT NULL,
    -- 参考图调色板（有序）与 2×2 图块频次
    palette TEXT NOT NULL,
    tile_counts TEXT NOT NULL,
    -- 当前全部色点约束：{"x,y": [r,g,b]}
    fixed_points TEXT NOT NULL,
    -- 最近一次求解得到的候选纹版网格（无解/待定时为旧网格）
    grid TEXT,
    tile_usage TEXT NOT NULL
);
"""


class DraftStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _encode_points(points: dict[tuple[int, int], RGB]) -> str:
        return json.dumps(
            {f"{x},{y}": list(color) for (x, y), color in sorted(points.items())},
            separators=(",", ":"),
        )

    @staticmethod
    def decode_points(raw: str) -> dict[tuple[int, int], RGB]:
        data = json.loads(raw)
        return {
            (int(key.split(",", 1)[0]), int(key.split(",", 1)[1])): tuple(value)  # type: ignore[misc]
            for key, value in data.items()
        }

    @staticmethod
    def _encode_grid(
        grid: Optional[tuple[tuple[RGB, ...], ...]],
    ) -> Optional[str]:
        if grid is None:
            return None
        return json.dumps([[list(c) for c in row] for row in grid],
                          separators=(",", ":"))

    @staticmethod
    def decode_grid(raw: Optional[str]) -> Optional[tuple[tuple[RGB, ...], ...]]:
        if raw is None:
            return None
        data = json.loads(raw)
        return tuple(tuple(tuple(c) for c in row) for row in data)  # type: ignore[return-value]

    def create_draft(
        self,
        *,
        unit_w: int,
        unit_h: int,
        result: SolveResult,
        palette: tuple[RGB, ...],
        tile_counts: dict,
        fixed_points: dict[tuple[int, int], RGB],
    ) -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO drafts (
                    created_at, updated_at, unit_w, unit_h, status, score,
                    upper_bound, elapsed_seconds, solver_name, palette,
                    tile_counts, fixed_points, grid, tile_usage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now, now, unit_w, unit_h, result.status, result.score,
                    result.upper_bound, result.elapsed_seconds, result.solver_name,
                    json.dumps([list(c) for c in palette]),
                    json.dumps(
                        [{"tile": [list(c) for c in k], "count": v}
                         for k, v in sorted(tile_counts.items())],
                        separators=(",", ":")),
                    self._encode_points(fixed_points),
                    self._encode_grid(result.grid),
                    json.dumps(result.tile_usage, separators=(",", ":")),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_draft(self, draft_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        return row

    def update_draft(
        self,
        draft_id: int,
        *,
        result: SolveResult,
        fixed_points: dict[tuple[int, int], RGB],
    ) -> None:
        """用新一轮求解结果更新草案。

        新状态为已证无解/待定时保留旧网格，仅刷新状态与约束；
        有候选时网格一并替换。
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE drafts SET
                    updated_at = ?, status = ?, score = ?, upper_bound = ?,
                    elapsed_seconds = ?, solver_name = ?, fixed_points = ?,
                    grid = COALESCE(?, grid), tile_usage = ?
                WHERE id = ?
                """,
                (
                    now, result.status, result.score, result.upper_bound,
                    result.elapsed_seconds, result.solver_name,
                    self._encode_points(fixed_points),
                    self._encode_grid(result.grid),
                    json.dumps(result.tile_usage, separators=(",", ":")),
                    draft_id,
                ),
            )
            self._conn.commit()

    def row_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "unit_w": row["unit_w"],
            "unit_h": row["unit_h"],
            "status": row["status"],
            "status_label": _status_label(row["status"]),
            "score": row["score"],
            "upper_bound": row["upper_bound"],
            "elapsed_seconds": row["elapsed_seconds"],
            "solver_name": row["solver_name"],
            "palette": json.loads(row["palette"]),
            "tile_counts": json.loads(row["tile_counts"]),
            "fixed_points": json.loads(row["fixed_points"]),
            "grid": json.loads(row["grid"]) if row["grid"] else None,
            "tile_usage": json.loads(row["tile_usage"]),
        }


def _status_label(status: str) -> str:
    from .solver import STATUS_LABELS

    return STATUS_LABELS.get(status, status)
