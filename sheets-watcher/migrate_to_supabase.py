"""Одноразовый скрипт миграции data.json -> Supabase.

Использование:
    export SUPABASE_URL=https://xxxx.supabase.co
    export SUPABASE_KEY=eyJ...
    python migrate_to_supabase.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from supabase import create_client
import os

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("Задайте переменные окружения SUPABASE_URL и SUPABASE_KEY")

DATA_PATH = Path(__file__).parent / "data.json"

if not DATA_PATH.exists():
    sys.exit(f"Файл {DATA_PATH} не найден")

data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def migrate_projects_and_sections():
    """config.projects -> таблицы projects и sections."""
    config = data.get("config", {})
    projects = config.get("projects", [])
    last_check = data.get("last_check", {})

    print(f"Проекты: {len(projects)}")
    for proj in projects:
        pid = proj["id"]
        # Парсим last_check если есть
        lc_iso = None
        lc_str = last_check.get(pid)
        if lc_str:
            try:
                dt = datetime.strptime(lc_str, "%d.%m.%Y %H:%M")
                lc_iso = dt.isoformat()
            except ValueError:
                lc_iso = lc_str

        sb.table("projects").upsert({
            "id": pid,
            "name": proj["name"],
            "last_check": lc_iso,
        }).execute()

        sections = proj.get("sections", [])
        print(f"  {proj['name']}: {len(sections)} разделов")
        for sec in sections:
            sb.table("sections").upsert({
                "id": sec["id"],
                "project_id": pid,
                "name": sec["name"],
                "url": sec["url"],
                "range": sec["range"],
            }).execute()


def migrate_snapshots():
    """snapshot -> таблица snapshots."""
    snapshot = data.get("snapshot", {})
    print(f"Снимки: {len(snapshot)} ячеек")
    records = [
        {"cell_key": k, "value": v}
        for k, v in snapshot.items()
    ]
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        sb.table("snapshots").upsert(chunk, on_conflict="cell_key").execute()
        print(f"  ...{min(i + 500, len(records))}/{len(records)}")


def migrate_cell_marks():
    """processed, actualized, changes -> таблица cell_marks."""
    mapping = {
        "processed": "processed",
        "actualized": "actualized",
        "changes": "changed",
    }
    for data_key, mark_type in mapping.items():
        marks = data.get(data_key, {})
        records = []
        for composite_key, cells in marks.items():
            parts = composite_key.split(":", 1)
            if len(parts) != 2:
                print(f"  [ПРОПУСК] Невалидный ключ: {composite_key}")
                continue
            pid, sid = parts
            for addr in cells:
                records.append({
                    "project_id": pid,
                    "section_id": sid,
                    "cell_address": addr,
                    "mark_type": mark_type,
                })
        print(f"{data_key} ({mark_type}): {len(records)} записей")
        for i in range(0, len(records), 500):
            chunk = records[i:i + 500]
            sb.table("cell_marks").insert(chunk).execute()


def migrate_hidden_cols():
    """hidden_cols -> таблица hidden_columns."""
    hidden = data.get("hidden_cols", {})
    records = []
    for composite_key, cols in hidden.items():
        parts = composite_key.split(":", 1)
        if len(parts) != 2:
            continue
        pid, sid = parts
        for col in cols:
            records.append({
                "project_id": pid,
                "section_id": sid,
                "column_letter": col,
            })
    print(f"Скрытые столбцы: {len(records)} записей")
    if records:
        sb.table("hidden_columns").insert(records).execute()


def migrate_section_cache():
    """cache -> таблица section_cache."""
    cache = data.get("cache", {})
    count = 0
    # Собираем валидные section_id из конфига
    valid_sids = set()
    for proj in data.get("config", {}).get("projects", []):
        for sec in proj.get("sections", []):
            valid_sids.add(sec["id"])

    for sid, cache_data in cache.items():
        if sid.startswith("_") or sid not in valid_sids:
            continue
        sb.table("section_cache").upsert({
            "section_id": sid,
            "data": cache_data,
        }).execute()
        count += 1
    print(f"Кеш разделов: {count} записей")


if __name__ == "__main__":
    print("=" * 50)
    print("Миграция data.json -> Supabase")
    print("=" * 50)
    print()

    try:
        print("[1/5] Проекты и разделы...")
        migrate_projects_and_sections()

        print("\n[2/5] Снимки...")
        migrate_snapshots()

        print("\n[3/5] Метки ячеек...")
        migrate_cell_marks()

        print("\n[4/5] Скрытые столбцы...")
        migrate_hidden_cols()

        print("\n[5/5] Кеш разделов...")
        migrate_section_cache()

        print("\n" + "=" * 50)
        print("Миграция завершена успешно!")
        print("=" * 50)
    except Exception as e:
        print(f"\n[ОШИБКА] {type(e).__name__}: {e}")
        sys.exit(1)
