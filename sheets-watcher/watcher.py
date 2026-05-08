import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# При сборке PyInstaller: данные рядом с .exe, не во временной папке
if getattr(sys, 'frozen', False):
    PROJECT_DIR = Path(sys.executable).parent
else:
    PROJECT_DIR = Path(__file__).parent

CREDENTIALS_PATH = PROJECT_DIR / "credentials.json"


def get_client() -> gspread.Client:
    """Авторизация в Google Sheets API.

    Порядок выбора источника credentials:
      1. GOOGLE_CREDENTIALS_JSON (env, JSON-строка) — основной для production/Vercel
      2. GOOGLE_CREDENTIALS (env, JSON-строка) — legacy, оставлено для обратной совместимости
      3. credentials.json (файл рядом с модулем) — для локальной разработки

    При ошибке поднимается RuntimeError с понятным сообщением, БЕЗ содержимого ключа.
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON") or os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        try:
            info = json.loads(creds_json)
        except json.JSONDecodeError as exc:
            # Не логируем сам creds_json — чтобы случайно не утёк в логи
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON не является валидным JSON. "
                f"Ошибка парсинга на позиции {exc.pos}. "
                "Проверьте, что переменная окружения содержит полный service account JSON."
            ) from None
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        logger.info("Google Sheets: используются credentials из env (%s)",
                    "GOOGLE_CREDENTIALS_JSON" if os.environ.get("GOOGLE_CREDENTIALS_JSON") else "GOOGLE_CREDENTIALS")
        return gspread.authorize(creds)

    # Локальная разработка: credentials из файла
    if CREDENTIALS_PATH.exists():
        creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
        logger.info("Google Sheets: используются credentials из файла %s", CREDENTIALS_PATH)
        return gspread.authorize(creds)

    raise RuntimeError(
        "Google Sheets credentials не найдены. "
        "На production задайте переменную окружения GOOGLE_CREDENTIALS_JSON "
        "(полный JSON service-account, одной строкой). "
        f"Для локальной разработки положите credentials.json рядом с {Path(__file__).name}."
    )


def read_ranges(
    client: gspread.Client,
    spreadsheet_id: str,
    ranges: list[str],
    sheet_name: str | None = None,
) -> dict[str, str]:
    """Считывает диапазоны и возвращает плоский словарь {адрес_ячейки: значение}."""
    result: dict[str, str] = {}

    try:
        spreadsheet = client.open_by_key(spreadsheet_id)
    except SpreadsheetNotFound:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Таблица {spreadsheet_id} не найдена или нет доступа.")
        return result
    except APIError as e:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Ошибка API при открытии таблицы: {e}")
        return result

    try:
        sheet = spreadsheet.worksheet(sheet_name) if sheet_name else spreadsheet.sheet1
    except gspread.exceptions.WorksheetNotFound:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Лист «{sheet_name}» не найден в таблице.")
        return result

    for rng in ranges:
        try:
            cells = sheet.range(rng)
        except APIError as e:
            print(f"[ПРЕДУПРЕЖДЕНИЕ] Ошибка API при чтении диапазона {rng}: {e}")
            continue

        for cell in cells:
            addr = rowcol_to_a1(cell.row, cell.col)
            result[addr] = cell.value if cell.value is not None else ""

    return result


# --------------- snapshot (Supabase) ---------------

from db import load_snapshot, save_snapshot, load_config as _db_load_config


def compare(
    old_data: dict[str, str],
    new_data: dict[str, str],
) -> list[dict[str, str | None]]:
    """Сравнивает два снимка и возвращает список изменений.

    Каждый элемент: {"cell": "E131", "old": "100", "new": "250"}
    - old = None  → ячейка появилась впервые
    - new = ""    → ячейка была, но стала пустой
    """
    changes: list[dict[str, str | None]] = []
    all_cells = sorted(set(old_data) | set(new_data))

    for cell in all_cells:
        old_val = old_data.get(cell)
        new_val = new_data.get(cell)

        if old_val == new_val:
            continue

        changes.append({"cell": cell, "old": old_val, "new": new_val})

    return changes


# --------------- report ---------------

def _format_value(val: str | None) -> str:
    if val is None:
        return "(нет)"
    if val == "":
        return "(пусто)"
    return val


def print_report(spreadsheet_name: str, changes: list[dict[str, str | None]]) -> None:
    """Выводит отчёт об изменениях для одной таблицы."""
    if not changes:
        return

    print(f"\n{'=' * 50}")
    print(f"  {spreadsheet_name}")
    print(f"{'=' * 50}")

    for ch in changes:
        old = _format_value(ch["old"])
        new = _format_value(ch["new"])
        print(f"  {ch['cell']:>6}  |  {old}  →  {new}")


# --------------- main ---------------

def _parse_spreadsheet_id(url: str) -> str | None:
    """Извлекает spreadsheet ID из URL Google Sheets."""
    import re
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def main() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now}] Запуск проверки таблиц...")

    config = _db_load_config()
    projects = config.get("projects", [])
    if not projects:
        sys.exit("[ОШИБКА] Нет проектов в конфигурации.")

    old_snapshot = load_snapshot()
    first_run = not old_snapshot

    client = get_client()

    new_snapshot: dict[str, str] = {}
    total_changes = 0
    total_sections = 0

    for proj in projects:
        for sec in proj.get("sections", []):
            total_sections += 1
            sp_id = _parse_spreadsheet_id(sec["url"])
            if not sp_id:
                print(f"[ПРЕДУПРЕЖДЕНИЕ] Невалидный URL в разделе «{sec['name']}»")
                continue

            ranges = [r.strip() for r in sec["range"].split(",") if r.strip()]
            data = read_ranges(client, sp_id, ranges)

            prefixed = {f"{sp_id}!{addr}": val for addr, val in data.items()}
            new_snapshot.update(prefixed)

            if first_run:
                continue

            old_for_sheet = {
                k: v for k, v in old_snapshot.items() if k.startswith(f"{sp_id}!")
            }

            changes = compare(old_for_sheet, prefixed)

            for ch in changes:
                ch["cell"] = ch["cell"].split("!", 1)[1]

            section_name = f"{proj['name']} / {sec['name']}"
            print_report(section_name, changes)
            total_changes += len(changes)

    save_snapshot(new_snapshot)

    print()
    if first_run:
        print("Первый запуск, состояние сохранено.")
    else:
        print(f"Итого: {total_changes} изм. в {total_sections} разделах.")

    print()


if __name__ == "__main__":
    main()
