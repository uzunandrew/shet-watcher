"""Слой доступа к данным через Supabase.

Экспортирует функции с теми же сигнатурами, что и старые хелперы
load_*/save_* из app.py, чтобы route handlers работали без изменений.
"""

import os
import logging
from datetime import datetime, timezone

from supabase import create_client, Client

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "Переменные окружения SUPABASE_URL и SUPABASE_KEY должны быть заданы.\n"
                "Пример:\n"
                "  export SUPABASE_URL=https://xxxx.supabase.co\n"
                "  export SUPABASE_KEY=eyJ..."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# --------------- config (projects + sections) ---------------

def load_config() -> dict:
    """Возвращает {"projects": [{id, name, sections: [{id, name, url, range}]}]}."""
    sb = _get_client()
    projects = sb.table("projects").select("*").order("created_at").execute().data
    sections = sb.table("sections").select("*").order("created_at").execute().data

    sec_by_project: dict[str, list] = {}
    for s in sections:
        sec_by_project.setdefault(s["project_id"], []).append({
            "id": s["id"],
            "name": s["name"],
            "url": s["url"],
            "range": s["range"],
        })

    return {
        "projects": [
            {
                "id": p["id"],
                "name": p["name"],
                "sections": sec_by_project.get(p["id"], []),
            }
            for p in projects
        ]
    }


def save_config(cfg: dict) -> None:
    """Синхронизирует projects/sections с БД: upsert + удаление отсутствующих."""
    sb = _get_client()
    incoming_projects = cfg.get("projects", [])
    incoming_pids = set()
    incoming_sids = set()

    for proj in incoming_projects:
        pid = proj["id"]
        incoming_pids.add(pid)
        sb.table("projects").upsert({
            "id": pid,
            "name": proj["name"],
        }).execute()

        for sec in proj.get("sections", []):
            sid = sec["id"]
            incoming_sids.add(sid)
            sb.table("sections").upsert({
                "id": sid,
                "project_id": pid,
                "name": sec["name"],
                "url": sec["url"],
                "range": sec["range"],
            }).execute()

    # Удаляем разделы, которых нет в новом конфиге
    existing_sections = sb.table("sections").select("id").execute().data
    for s in existing_sections:
        if s["id"] not in incoming_sids:
            sb.table("sections").delete().eq("id", s["id"]).execute()

    # Удаляем проекты, которых нет в новом конфиге (CASCADE удалит sections)
    existing_projects = sb.table("projects").select("id").execute().data
    for p in existing_projects:
        if p["id"] not in incoming_pids:
            sb.table("projects").delete().eq("id", p["id"]).execute()


def find_project(cfg: dict, pid: str) -> dict | None:
    """Находит проект по ID в загруженном конфиге (без обращения к БД)."""
    for p in cfg.get("projects", []):
        if p["id"] == pid:
            return p
    return None


# --------------- snapshots ---------------

def load_snapshot() -> dict:
    """Возвращает {cell_key: value}. Пагинация для >1000 строк."""
    sb = _get_client()
    result = {}
    offset = 0
    page_size = 1000
    while True:
        rows = (
            sb.table("snapshots")
            .select("cell_key, value")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        for r in rows:
            result[r["cell_key"]] = r["value"]
        if len(rows) < page_size:
            break
        offset += page_size
    return result


def save_snapshot(data: dict) -> None:
    """UPSERT снимков по cell_key. Чанки по 500."""
    sb = _get_client()
    records = [
        {"cell_key": k, "value": v, "updated_at": datetime.now(timezone.utc).isoformat()}
        for k, v in data.items()
    ]
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        sb.table("snapshots").upsert(chunk, on_conflict="cell_key").execute()


# --------------- cell_marks (processed / actualized / changed) ---------------

def _load_marks(mark_type: str) -> dict:
    """Загружает метки одного типа, возвращает {pid:sid: [cell_addrs]}."""
    sb = _get_client()
    result: dict[str, list[str]] = {}
    offset = 0
    page_size = 1000
    while True:
        rows = (
            sb.table("cell_marks")
            .select("project_id, section_id, cell_address")
            .eq("mark_type", mark_type)
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        for r in rows:
            key = f"{r['project_id']}:{r['section_id']}"
            result.setdefault(key, []).append(r["cell_address"])
        if len(rows) < page_size:
            break
        offset += page_size
    return result


def _save_marks(data: dict, mark_type: str) -> None:
    """Полная перезапись меток одного типа."""
    sb = _get_client()
    # Удаляем все старые метки этого типа
    sb.table("cell_marks").delete().eq("mark_type", mark_type).execute()
    # Вставляем новые
    records = []
    for composite_key, cells in data.items():
        pid, sid = composite_key.split(":", 1)
        for addr in cells:
            records.append({
                "project_id": pid,
                "section_id": sid,
                "cell_address": addr,
                "mark_type": mark_type,
            })
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        sb.table("cell_marks").insert(chunk).execute()


def load_processed() -> dict:
    return _load_marks("processed")

def save_processed(data: dict) -> None:
    _save_marks(data, "processed")

def load_changes() -> dict:
    return _load_marks("changed")

def save_changes(data: dict) -> None:
    _save_marks(data, "changed")

def load_actualized() -> dict:
    return _load_marks("actualized")

def save_actualized(data: dict) -> None:
    _save_marks(data, "actualized")


# --------------- hidden_columns ---------------

def load_hidden_cols() -> dict:
    """Возвращает {pid:sid: [col_letters]}."""
    sb = _get_client()
    rows = sb.table("hidden_columns").select("project_id, section_id, column_letter").execute().data
    result: dict[str, list[str]] = {}
    for r in rows:
        key = f"{r['project_id']}:{r['section_id']}"
        result.setdefault(key, []).append(r["column_letter"])
    return result


def save_hidden_cols(data: dict) -> None:
    """Полная перезапись скрытых столбцов."""
    sb = _get_client()
    sb.table("hidden_columns").delete().gt("id", 0).execute()
    records = []
    for composite_key, cols in data.items():
        pid, sid = composite_key.split(":", 1)
        for col in cols:
            records.append({
                "project_id": pid,
                "section_id": sid,
                "column_letter": col,
            })
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        sb.table("hidden_columns").insert(chunk).execute()


# --------------- last_check ---------------

def load_last_check() -> dict:
    """Возвращает {pid: "DD.MM.YYYY HH:MM"}."""
    sb = _get_client()
    rows = sb.table("projects").select("id, last_check").execute().data
    result: dict[str, str] = {}
    for r in rows:
        if r["last_check"]:
            try:
                dt = datetime.fromisoformat(r["last_check"])
                result[r["id"]] = dt.strftime("%d.%m.%Y %H:%M")
            except (ValueError, TypeError):
                result[r["id"]] = r["last_check"]
    return result


def save_last_check(data: dict) -> None:
    """Обновляет last_check для каждого проекта."""
    sb = _get_client()
    for pid, dt_str in data.items():
        # Парсим "DD.MM.YYYY HH:MM" → ISO для Postgres
        try:
            dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M")
            iso = dt.isoformat()
        except ValueError:
            iso = dt_str
        sb.table("projects").update({"last_check": iso}).eq("id", pid).execute()


# --------------- section_cache ---------------

def load_section_cache() -> dict:
    """Возвращает {section_id: {data}}."""
    sb = _get_client()
    rows = sb.table("section_cache").select("section_id, data").execute().data
    return {r["section_id"]: r["data"] for r in rows}


def save_section_cache(data: dict) -> None:
    """Полная перезапись кеша разделов."""
    sb = _get_client()
    for sid, cache_data in data.items():
        sb.table("section_cache").upsert({
            "section_id": sid,
            "data": cache_data,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
