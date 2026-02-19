import json
import sys
from datetime import datetime
from pathlib import Path

import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

PROJECT_DIR = Path(__file__).parent
CREDENTIALS_PATH = PROJECT_DIR / "credentials.json"
SNAPSHOT_PATH = PROJECT_DIR / "snapshot.json"
CONFIG_PATH = PROJECT_DIR / "config.json"


def get_client() -> gspread.Client:
    if not CREDENTIALS_PATH.exists():
        sys.exit(
            f"[ОШИБКА] Файл {CREDENTIALS_PATH} не найден.\n"
            "Скачайте JSON-ключ сервисного аккаунта из Google Cloud Console "
            "и положите его в корень проекта под именем credentials.json."
        )

    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


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


# --------------- snapshot ---------------

def load_snapshot() -> dict[str, str]:
    """Читает snapshot.json. Если файла нет или он пуст/повреждён — возвращает {}."""
    if not SNAPSHOT_PATH.exists():
        return {}

    text = SNAPSHOT_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("[ПРЕДУПРЕЖДЕНИЕ] snapshot.json повреждён, начинаем с чистого снимка.")
        return {}


def save_snapshot(data: dict[str, str]) -> None:
    """Сохраняет данные в snapshot.json с отступами для читаемости."""
    SNAPSHOT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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

def main() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now}] Запуск проверки таблиц...")

    if not CONFIG_PATH.exists():
        sys.exit(f"[ОШИБКА] Файл {CONFIG_PATH} не найден.")

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"[ОШИБКА] config.json повреждён: {e}")

    old_snapshot = load_snapshot()
    first_run = not old_snapshot

    client = get_client()

    new_snapshot: dict[str, str] = {}
    total_changes = 0
    tables_with_changes = 0

    for sheet_cfg in config["spreadsheets"]:
        name = sheet_cfg["name"]
        sid = sheet_cfg["id"]
        ranges = sheet_cfg["ranges"]
        sheet_name = sheet_cfg.get("sheet")

        data = read_ranges(client, sid, ranges, sheet_name)

        # Ключи snapshot хранятся с префиксом id таблицы,
        # чтобы не перепутать ячейки из разных таблиц.
        prefixed = {f"{sid}!{addr}": val for addr, val in data.items()}
        new_snapshot.update(prefixed)

        if first_run:
            continue

        old_for_sheet = {
            k: v for k, v in old_snapshot.items() if k.startswith(f"{sid}!")
        }

        changes = compare(old_for_sheet, prefixed)

        # В отчёте показываем адреса без префикса id
        for ch in changes:
            ch["cell"] = ch["cell"].split("!", 1)[1]

        print_report(name, changes)
        total_changes += len(changes)
        if changes:
            tables_with_changes += 1

    save_snapshot(new_snapshot)

    print()
    if first_run:
        print("Первый запуск, состояние сохранено.")
    else:
        print(f"Итого: {total_changes} изм. в {tables_with_changes} табл. "
              f"из {len(config['spreadsheets'])}.")

    print()


if __name__ == "__main__":
    main()
