import json
from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).parent / "searches.db"

_CREATE = """
CREATE TABLE IF NOT EXISTS searches (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at             TEXT NOT NULL,
    address                TEXT,
    redfin_url             TEXT,
    radius_miles           REAL,
    filters                TEXT,
    target                 TEXT,
    comparables            TEXT,
    total_candidates_found INTEGER,
    scraped_at             TEXT
)
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE)
        await db.commit()


async def save_search(req: dict, resp: dict) -> int:
    filters = {
        k: v for k, v in req.items()
        if k not in ("address", "redfin_url", "radius_miles", "max_comparables") and v is not None
    }
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO searches
                (created_at, address, redfin_url, radius_miles, filters,
                 target, comparables, total_candidates_found, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resp["scraped_at"],
                req.get("address"),
                req.get("redfin_url"),
                req.get("radius_miles", 2.0),
                json.dumps(filters),
                json.dumps(resp["target"]),
                json.dumps(resp["comparables"]),
                resp["total_candidates_found"],
                resp["scraped_at"],
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def list_searches(limit: int = 50, offset: int = 0) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, created_at, address, redfin_url, radius_miles,
                   total_candidates_found, scraped_at
            FROM searches
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_search(search_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM searches WHERE id = ?", (search_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        data["filters"] = json.loads(data["filters"] or "{}")
        data["target"] = json.loads(data["target"] or "{}")
        data["comparables"] = json.loads(data["comparables"] or "[]")
        return data


async def delete_search(search_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM searches WHERE id = ?", (search_id,))
        await db.commit()
        return cursor.rowcount > 0
