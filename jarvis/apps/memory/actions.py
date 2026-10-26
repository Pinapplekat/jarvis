from datetime import datetime, timezone

from jarvis.core.api import ActionError, AppContext, Result

COLUMNS = "id, category, key, value, source, updated_at"
LIMIT = 20


def _row(ctx: AppContext, category: str, key: str) -> dict | None:
    r = ctx.db.execute(f"SELECT {COLUMNS} FROM memory_facts WHERE category=? AND key=?", (category, key)).fetchone()
    return dict(r) if r else None


def remember(ctx: AppContext, category: str, key: str, value: str) -> Result:
    previous = _row(ctx, category, key)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ctx.db.execute(
        "INSERT INTO memory_facts (category, key, value, source, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(category, key) DO UPDATE SET value=excluded.value, source=excluded.source, "
        "updated_at=excluded.updated_at",
        (category, key, value, "remember", now),
    )
    verb = "Updated" if previous else "Remembered"
    return Result(f"{verb} {category}/{key}: {value}",
                  undo={"category": category, "key": key, "previous": previous})


def undo_remember(ctx: AppContext, undo: dict) -> Result:
    prev = undo["previous"]
    if prev is None:
        ctx.db.execute("DELETE FROM memory_facts WHERE category=? AND key=?", (undo["category"], undo["key"]))
        return Result(f"Removed {undo['category']}/{undo['key']} again.")
    _restore(ctx, prev)
    return Result(f"Restored {prev['category']}/{prev['key']}: {prev['value']}")


def _restore(ctx: AppContext, row: dict) -> None:
    # Replace by (category, key) so the row comes back byte for byte, id included.
    ctx.db.execute("DELETE FROM memory_facts WHERE category=? AND key=?", (row["category"], row["key"]))
    ctx.db.execute(
        f"INSERT INTO memory_facts ({COLUMNS}) VALUES (:id, :category, :key, :value, :source, :updated_at)", row
    )


def forget(ctx: AppContext, category: str, key: str) -> Result:
    row = _row(ctx, category, key)
    if row is None:
        raise ActionError(f"I don't have a fact called {category}/{key}.")
    ctx.db.execute("DELETE FROM memory_facts WHERE category=? AND key=?", (category, key))
    return Result(f"Forgot {category}/{key}.", undo={"row": row})


def undo_forget(ctx: AppContext, undo: dict) -> Result:
    _restore(ctx, undo["row"])
    return Result(f"Restored {undo['row']['category']}/{undo['row']['key']}.")


def recall(ctx: AppContext, query: str, category: str | None = None) -> Result:
    sql = (
        f"SELECT {COLUMNS} FROM memory_facts WHERE (instr(lower(category), lower(:q)) > 0 "
        "OR instr(lower(key), lower(:q)) > 0 OR instr(lower(value), lower(:q)) > 0)"
    )
    params: dict = {"q": query}
    if category:
        sql += " AND category = :c"
        params["c"] = category
    rows = [dict(r) for r in ctx.db.execute(sql + f" ORDER BY updated_at DESC, id DESC LIMIT {LIMIT}", params)]
    if not rows:
        return Result(f"No facts match '{query}'.", data={"facts": []})
    lines = [f"{r['category']}/{r['key']}: {r['value']}" for r in rows]
    return Result("\n".join(lines), data={"facts": rows})
