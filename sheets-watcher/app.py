import json
import os
import re
import sys
import time
import uuid
import webbrowser
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from flask import Flask, render_template_string, redirect, url_for, request, jsonify

from gspread.utils import rowcol_to_a1

from watcher import get_client

app = Flask(__name__)

# При сборке PyInstaller: sys.executable указывает на .exe,
# данные храним рядом с ним. При обычном запуске — рядом со скриптом.
if getattr(sys, 'frozen', False):
    PROJECT_DIR = Path(sys.executable).parent
else:
    PROJECT_DIR = Path(__file__).parent

DATA_PATH = PROJECT_DIR / "data.json"

# Пути старых файлов — для автомиграции
_OLD_FILES = {
    "config": PROJECT_DIR / "config.json",
    "snapshot": PROJECT_DIR / "snapshot.json",
    "processed": PROJECT_DIR / "processed.json",
    "actualized": PROJECT_DIR / "actualized.json",
    "changes": PROJECT_DIR / "changes.json",
    "hidden_cols": PROJECT_DIR / "hidden_cols.json",
    "last_check": PROJECT_DIR / "last_check.json",
    "cache": PROJECT_DIR / "cache.json",
}

_DATA_DEFAULTS = {
    "config": {"projects": []},
    "snapshot": {},
    "processed": {},
    "actualized": {},
    "changes": {},
    "hidden_cols": {},
    "last_check": {},
    "cache": {},
}


def _migrate_to_single_file() -> None:
    """При первом запуске: собирает старые JSON-файлы в единый data.json."""
    if DATA_PATH.exists():
        return
    data = {}
    for key, path in _OLD_FILES.items():
        if path.exists():
            try:
                data[key] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data[key] = _DATA_DEFAULTS[key]
        else:
            data[key] = _DATA_DEFAULTS[key]
    save_data(data)
    # Удаляем старые файлы после успешной миграции
    for key, path in _OLD_FILES.items():
        if path.exists():
            path.unlink()


def load_data() -> dict:
    if not DATA_PATH.exists():
        return {k: dict(v) if isinstance(v, dict) else v for k, v in _DATA_DEFAULTS.items()}
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {k: dict(v) if isinstance(v, dict) else v for k, v in _DATA_DEFAULTS.items()}
    for key, default in _DATA_DEFAULTS.items():
        if key not in data:
            data[key] = default
    return data


def save_data(data: dict) -> None:
    DATA_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# --------------- helpers ---------------

def load_hidden_cols() -> dict:
    return load_data()["hidden_cols"]

def save_hidden_cols(data: dict) -> None:
    d = load_data(); d["hidden_cols"] = data; save_data(d)

def load_processed() -> dict:
    return load_data()["processed"]

def save_processed(data: dict) -> None:
    d = load_data(); d["processed"] = data; save_data(d)

def load_changes() -> dict:
    return load_data()["changes"]

def save_changes(data: dict) -> None:
    d = load_data(); d["changes"] = data; save_data(d)

def load_config() -> dict:
    return load_data()["config"]

def save_config(cfg: dict) -> None:
    d = load_data(); d["config"] = cfg; save_data(d)

def load_actualized() -> dict:
    return load_data()["actualized"]

def save_actualized(data: dict) -> None:
    d = load_data(); d["actualized"] = data; save_data(d)

def load_last_check() -> dict:
    return load_data()["last_check"]

def save_last_check(data: dict) -> None:
    d = load_data(); d["last_check"] = data; save_data(d)

def load_snapshot() -> dict:
    return load_data()["snapshot"]

def save_snapshot(data: dict) -> None:
    d = load_data(); d["snapshot"] = data; save_data(d)


def find_project(cfg, pid):
    for p in cfg["projects"]:
        if p["id"] == pid:
            return p
    return None


def parse_url(url: str) -> tuple[str | None, int | None]:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    sid = m.group(1) if m else None
    gid = None
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "gid" in params:
        gid = int(params["gid"][0])
    elif parsed.fragment:
        frag = parse_qs(parsed.fragment)
        if "gid" in frag:
            gid = int(frag["gid"][0])
    return sid, gid


def _cell_in_range(cell, range_str: str) -> bool:
    """Проверяет, попадает ли ячейка в заданный A1-диапазон (напр. 'A4:H24')."""
    from gspread.utils import a1_to_rowcol
    parts = range_str.split(':')
    if len(parts) != 2:
        return True
    r1, c1 = a1_to_rowcol(parts[0])
    r2, c2 = a1_to_rowcol(parts[1])
    return r1 <= cell.row <= r2 and c1 <= cell.col <= c2


def read_grid(client, spreadsheet_id, gid, range_str):
    spreadsheet = client.open_by_key(spreadsheet_id)
    sheet = spreadsheet.get_worksheet_by_id(gid) if gid is not None else spreadsheet.sheet1
    sheet_title = sheet.title

    # Поддержка нескольких диапазонов через запятую: "A4:H24, W4:W24"
    range_parts = [r.strip() for r in range_str.split(',') if r.strip()]

    all_cells = []
    for rng in range_parts:
        all_cells.extend(sheet.range(rng))

    if not all_cells:
        return sheet_title, [], [], {}

    min_row = min(c.row for c in all_cells)
    max_row = max(c.row for c in all_cells)
    # Уникальные столбцы (без пропусков между диапазонами)
    unique_cols = sorted(set(c.col for c in all_cells))

    cell_map = {}
    flat = {}
    for c in all_cells:
        addr = rowcol_to_a1(c.row, c.col)
        val = c.value if c.value is not None else ""
        cell_map[(c.row, c.col)] = val
        flat[f"{spreadsheet_id}!{addr}"] = val

    # Группируем ячейки по под-диапазонам для определения начала
    range_cell_groups = {}
    for rng in range_parts:
        range_cell_groups[rng] = [c for c in all_cells if _cell_in_range(c, rng)]

    # Извлекаем гиперссылки через Sheets API v4 (один batch-запрос)
    link_map: dict[tuple[int, int], str] = {}
    try:
        http = spreadsheet.client
        all_ranges = [f"'{sheet_title}'!{rng}" for rng in range_parts]
        response = http.request(
            'get',
            f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}',
            params={
                'ranges': all_ranges,
                'fields': 'sheets.data.rowData.values(hyperlink)',
            },
        )
        data = response.json()

        sheets_data = data.get('sheets', [])
        if sheets_data:
            grid_data_list = sheets_data[0].get('data', [])
            for i, rng in enumerate(range_parts):
                if i >= len(grid_data_list):
                    break
                sub_cells = range_cell_groups.get(rng, [])
                if not sub_cells:
                    continue
                sub_min_row = min(c.row for c in sub_cells)
                sub_min_col = min(c.col for c in sub_cells)
                for r_idx, row_data in enumerate(grid_data_list[i].get('rowData', [])):
                    for c_idx, cell_data in enumerate(row_data.get('values', [])):
                        hyperlink = cell_data.get('hyperlink')
                        if hyperlink:
                            link_map[(sub_min_row + r_idx, sub_min_col + c_idx)] = hyperlink
    except Exception as exc:
        print(f"[DEBUG hyperlinks] API ошибка: {type(exc).__name__}: {exc}")

    # Fallback: если ячейка содержит URL как текст — считаем его ссылкой
    url_re = re.compile(r'^https?://\S+$')
    for (row, col), val in cell_map.items():
        if (row, col) not in link_map and val and url_re.match(val.strip()):
            link_map[(row, col)] = val.strip()

    print(f"[DEBUG hyperlinks] Найдено ссылок: {len(link_map)}")

    # Заголовки столбцов — только фактические (без пропусков)
    col_headers = []
    for col in unique_cols:
        col_headers.append(rowcol_to_a1(1, col).rstrip("0123456789"))

    rows = []
    for row in range(min_row, max_row + 1):
        row_cells = []
        for col in unique_cols:
            addr = rowcol_to_a1(row, col)
            cell = {
                "addr": addr,
                "value": cell_map.get((row, col), ""),
                "link": link_map.get((row, col)),
            }
            row_cells.append(cell)
        rows.append({"num": row, "cells": row_cells})

    return sheet_title, col_headers, rows, flat


# Кеш результатов по секциям
def load_section_cache() -> dict:
    return load_data()["cache"]

def save_section_cache(data: dict) -> None:
    d = load_data(); d["cache"] = data; save_data(d)


# --------------- routes ---------------

@app.route("/")
def index():
    cfg = load_config()
    projects = cfg.get("projects", [])
    if projects:
        return redirect(url_for("project_view", pid=projects[0]["id"]))
    return redirect(url_for("empty"))


@app.route("/empty")
def empty():
    cfg = load_config()
    return render_template_string(
        HTML,
        projects=cfg.get("projects", []),
        current=None,
        sections_data={},
        last_check_date="",
    )


@app.route("/project/<pid>")
def project_view(pid):
    cfg = load_config()
    proj = find_project(cfg, pid)
    if not proj:
        return redirect(url_for("index"))

    sc = load_section_cache()

    # Подгружаем актуальные hidden_cols, processed и actualized для секций
    hidden = load_hidden_cols()
    processed = load_processed()
    all_changes = load_changes()
    actualized = load_actualized()
    for sec in proj.get("sections", []):
        key = f"{pid}:{sec['id']}"
        if sec["id"] in sc:
            sc[sec["id"]]["hidden_cols"] = hidden.get(key, [])
            sc[sec["id"]]["processed"] = processed.get(key, [])
            sc[sec["id"]]["actualized"] = actualized.get(key, [])
            # Восстанавливаем accumulated changes
            saved_ch = all_changes.get(key, [])
            if saved_ch and not sc[sec["id"]].get("changed"):
                sc[sec["id"]]["changed"] = saved_ch
                sc[sec["id"]]["total"] = len(saved_ch)

    lc = load_last_check()
    last_check_date = lc.get(pid, "")

    return render_template_string(
        HTML,
        projects=cfg.get("projects", []),
        current=proj,
        sections_data=sc,
        last_check_date=last_check_date,
    )


@app.route("/project/add", methods=["POST"])
def project_add():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("index"))
    cfg = load_config()
    pid = uuid.uuid4().hex[:8]
    cfg["projects"].append({"id": pid, "name": name, "sections": []})
    save_config(cfg)
    return redirect(url_for("project_view", pid=pid))


@app.route("/project/<pid>/rename", methods=["POST"])
def project_rename(pid):
    new_name = request.form.get("name", "").strip()
    if not new_name:
        return redirect(url_for("project_view", pid=pid))
    cfg = load_config()
    for p in cfg["projects"]:
        if p["id"] == pid:
            p["name"] = new_name
            break
    save_config(cfg)
    return redirect(url_for("project_view", pid=pid))


@app.route("/project/<pid>/delete", methods=["POST"])
def project_delete(pid):
    cfg = load_config()
    cfg["projects"] = [p for p in cfg["projects"] if p["id"] != pid]
    save_config(cfg)
    return redirect(url_for("index"))


@app.route("/project/<pid>/section/add", methods=["POST"])
def section_add(pid):
    cfg = load_config()
    proj = find_project(cfg, pid)
    if not proj:
        return redirect(url_for("index"))

    name = request.form.get("name", "").strip() or "Без названия"
    url = request.form.get("url", "").strip()
    rng = request.form.get("range", "").strip()

    sid = uuid.uuid4().hex[:8]
    proj["sections"].append({"id": sid, "name": name, "url": url, "range": rng})
    save_config(cfg)
    return redirect(url_for("project_view", pid=pid))


@app.route("/project/<pid>/section/<sid>/delete", methods=["POST"])
def section_delete(pid, sid):
    cfg = load_config()
    proj = find_project(cfg, pid)
    if proj:
        proj["sections"] = [s for s in proj["sections"] if s["id"] != sid]
        save_config(cfg)
    return redirect(url_for("project_view", pid=pid))


@app.route("/project/<pid>/section/<sid>/edit", methods=["POST"])
def section_edit(pid, sid):
    cfg = load_config()
    proj = find_project(cfg, pid)
    if not proj:
        return redirect(url_for("index"))
    for sec in proj["sections"]:
        if sec["id"] == sid:
            name = request.form.get("name", "").strip()
            rng = request.form.get("range", "").strip()
            url_val = request.form.get("url", "").strip()
            if name:
                sec["name"] = name
            if rng:
                sec["range"] = rng
            if url_val:
                sec["url"] = url_val
            break
    save_config(cfg)
    return redirect(url_for("project_view", pid=pid))


@app.route("/project/<pid>/check", methods=["POST"])
def project_check(pid):
    cfg = load_config()
    proj = find_project(cfg, pid)
    if not proj:
        return redirect(url_for("index"))

    old_snapshot = load_snapshot()
    first_run = not old_snapshot
    new_snapshot = dict(old_snapshot)

    sc = load_section_cache()

    try:
        client = get_client()
    except Exception as e:
        sc[f"_error_{pid}"] = {"error": str(e)}
        save_section_cache(sc)
        return redirect(url_for("project_view", pid=pid))

    sc.pop(f"_error_{pid}", None)

    all_changes = load_changes()

    for idx, sec in enumerate(proj["sections"]):
        if idx > 0:
            time.sleep(3)  # задержка между разделами для API лимита
        try:
            sp_id, gid = parse_url(sec["url"])
            if not sp_id:
                sc[sec["id"]] = {"error": "Невалидная ссылка"}
                continue

            sheet_title, col_headers, rows, flat = read_grid(
                client, sp_id, gid, sec["range"]
            )

            # Ключ для хранения накопленных изменений
            changes_key = f"{pid}:{sec['id']}"
            saved_changes = set(all_changes.get(changes_key, []))

            # Находим новые изменения (сравниваем со snapshot)
            if not first_run:
                for key, new_val in flat.items():
                    old_val = old_snapshot.get(key)
                    if old_val is not None and old_val != new_val:
                        addr = key.split("!", 1)[1]
                        saved_changes.add(addr)

            # Сохраняем накопленные изменения
            all_changes[changes_key] = list(saved_changes)

            # Список changed = все накопленные изменения
            changed = list(saved_changes)

            new_snapshot.update(flat)

            processed = load_processed()
            proc_key = f"{pid}:{sec['id']}"
            proc_list = processed.get(proc_key, [])

            hidden = load_hidden_cols()
            hidden_key = f"{pid}:{sec['id']}"
            hidden_list = hidden.get(hidden_key, [])

            act = load_actualized()
            act_list = act.get(f"{pid}:{sec['id']}", [])

            sc[sec["id"]] = {
                "sheet_title": sheet_title,
                "col_headers": col_headers,
                "rows": rows,
                "changed": changed,
                "processed": proc_list,
                "actualized": act_list,
                "hidden_cols": hidden_list,
                "total": len(changed),
                "checked_at": datetime.now().strftime("%H:%M:%S"),
                "first_run": first_run,
                "error": None,
            }
        except Exception as e:
            sc[sec["id"]] = {"error": str(e)}

    save_changes(all_changes)
    save_snapshot(new_snapshot)
    save_section_cache(sc)

    # Сохраняем дату последней проверки
    lc = load_last_check()
    lc[pid] = datetime.now().strftime("%d.%m.%Y %H:%M")
    save_last_check(lc)

    return redirect(url_for("project_view", pid=pid))


@app.route("/project/<pid>/section/<sid>/mark-processed", methods=["POST"])
def mark_processed(pid, sid):
    data = request.get_json(force=True)
    cells = data.get("cells", [])
    if not cells:
        return jsonify(ok=False)

    processed = load_processed()
    key = f"{pid}:{sid}"
    if key not in processed:
        processed[key] = []
    for addr in cells:
        if addr not in processed[key]:
            processed[key].append(addr)
    save_processed(processed)

    # Обновим кеш: перенести ячейки из changed в processed
    sc = load_section_cache()
    if sid in sc:
        if "processed" not in sc[sid]:
            sc[sid]["processed"] = []
        for addr in cells:
            if addr not in sc[sid]["processed"]:
                sc[sid]["processed"].append(addr)
        save_section_cache(sc)

    return jsonify(ok=True)


@app.route("/project/<pid>/section/<sid>/mark-actualized", methods=["POST"])
def mark_actualized(pid, sid):
    data = request.get_json(force=True)
    cells = data.get("cells", [])
    if not cells:
        return jsonify(ok=False)

    actualized = load_actualized()
    key = f"{pid}:{sid}"
    if key not in actualized:
        actualized[key] = []
    for addr in cells:
        if addr not in actualized[key]:
            actualized[key].append(addr)
    save_actualized(actualized)

    # Обновим кеш
    sc = load_section_cache()
    if sid in sc:
        if "actualized" not in sc[sid]:
            sc[sid]["actualized"] = []
        for addr in cells:
            if addr not in sc[sid]["actualized"]:
                sc[sid]["actualized"].append(addr)
        save_section_cache(sc)

    return jsonify(ok=True)


@app.route("/project/<pid>/section/<sid>/hide-cols", methods=["POST"])
def hide_cols(pid, sid):
    data = request.get_json(force=True)
    cols = data.get("cols", [])
    if not cols:
        return jsonify(ok=False)

    hidden = load_hidden_cols()
    key = f"{pid}:{sid}"
    if key not in hidden:
        hidden[key] = []
    for c in cols:
        if c not in hidden[key]:
            hidden[key].append(c)
    save_hidden_cols(hidden)

    sc = load_section_cache()
    if sid in sc:
        sc[sid]["hidden_cols"] = hidden[key]
        save_section_cache(sc)

    return jsonify(ok=True)


@app.route("/project/<pid>/section/<sid>/unhide-cols", methods=["POST"])
def unhide_cols(pid, sid):
    data = request.get_json(force=True)
    cols = data.get("cols", [])

    hidden = load_hidden_cols()
    key = f"{pid}:{sid}"
    if key in hidden:
        if cols:
            hidden[key] = [c for c in hidden[key] if c not in cols]
        else:
            hidden[key] = []
        save_hidden_cols(hidden)

    sc = load_section_cache()
    if sid in sc:
        sc[sid]["hidden_cols"] = hidden.get(key, [])
        save_section_cache(sc)

    return jsonify(ok=True)


# --------------- template ---------------

HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sheets Watcher</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect x='2' y='2' width='20' height='24' rx='2' fill='%23185a30' stroke='%234caf50' stroke-width='1.5'/><line x1='8' y1='8' x2='18' y2='8' stroke='%234caf50' stroke-width='1.5'/><line x1='8' y1='13' x2='18' y2='13' stroke='%234caf50' stroke-width='1.5'/><line x1='8' y1='18' x2='14' y2='18' stroke='%234caf50' stroke-width='1.5'/><line x1='5' y1='8' x2='6' y2='8' stroke='%2381c784' stroke-width='1.5'/><line x1='5' y1='13' x2='6' y2='13' stroke='%2381c784' stroke-width='1.5'/><line x1='5' y1='18' x2='6' y2='18' stroke='%2381c784' stroke-width='1.5'/><circle cx='22' cy='22' r='6' fill='%230d1b2a' stroke='%237b68ee' stroke-width='1.5'/><line x1='26.5' y1='26.5' x2='30' y2='30' stroke='%237b68ee' stroke-width='2' stroke-linecap='round'/><circle cx='22' cy='22' r='2' fill='none' stroke='%237b68ee' stroke-width='1'/></svg>">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
    font-family: 'Segoe UI', Tahoma, sans-serif;
    background: #0f0f1a;
    color: #d0d0d0;
    display: flex;
    overflow: hidden;
}

/* ---- sidebar ---- */
.sidebar {
    width: 260px;
    min-width: 260px;
    background: #12122a;
    border-right: 1px solid #1e1e3a;
    display: flex;
    flex-direction: column;
    height: 100vh;
}
.sidebar-head {
    padding: 20px;
    border-bottom: 1px solid #1e1e3a;
}
.sidebar-head h1 { font-size: 18px; color: #fff; }
.sidebar-head span { font-size: 11px; color: #555; }
.project-list {
    flex: 1;
    overflow-y: auto;
    padding: 8px 0;
    list-style: none;
}
.project-list li a {
    display: flex;
    align-items: center;
    padding: 10px 20px;
    color: #aaa;
    text-decoration: none;
    font-size: 14px;
    border-left: 3px solid transparent;
    transition: all 0.15s;
}
.project-list li a:hover { background: #1a1a35; color: #ddd; }
.project-list li.active a {
    background: #1a1a3a;
    color: #7b68ee;
    border-left-color: #7b68ee;
    font-weight: 600;
}
.project-list li .cnt {
    margin-left: auto;
    background: #1e1e3a;
    color: #666;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
}
.sidebar-add {
    padding: 12px 16px;
    border-top: 1px solid #1e1e3a;
}
.sidebar-add form { display: flex; gap: 6px; }
.sidebar-add input {
    flex: 1;
    padding: 8px 10px;
    background: #1a1a35;
    border: 1px solid #2a2a4a;
    border-radius: 6px;
    color: #ccc;
    font-size: 13px;
    outline: none;
}
.sidebar-add input:focus { border-color: #533483; }
.btn-add {
    background: #533483;
    color: #fff;
    border: none;
    width: 36px;
    height: 36px;
    border-radius: 6px;
    font-size: 20px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.15s;
}
.btn-add:hover { background: #7b68ee; }

/* ---- main ---- */
.main {
    flex: 1;
    overflow-y: auto;
    height: 100vh;
    padding: 28px 32px;
}
.main-head {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 24px;
}
.main-head h2 { font-size: 22px; color: #fff; margin: 0; }
.project-title { display: flex; align-items: center; gap: 8px; }
.btn-rename {
    background: none; border: none; color: #666; cursor: pointer;
    font-size: 14px; padding: 2px 6px; transition: color 0.15s;
}
.btn-rename:hover { color: #7b68ee; }
.rename-form { display: flex; align-items: center; gap: 8px; }
.rename-input {
    background: #1a1a35; border: 1px solid #2a2a5a; color: #fff;
    padding: 6px 12px; border-radius: 6px; font-size: 16px; width: 300px;
}
.rename-input:focus { border-color: #7b68ee; outline: none; }
.btn-small { padding: 5px 14px; font-size: 12px; }
.last-check-info {
    color: #90caf9;
    font-size: 13px;
    white-space: nowrap;
}
.btn {
    padding: 9px 22px;
    border: none;
    border-radius: 6px;
    font-size: 14px;
    cursor: pointer;
    transition: all 0.15s;
}
.btn-primary { background: #533483; color: #fff; }
.btn-primary:hover { background: #7b68ee; }
.btn-danger { background: transparent; color: #666; font-size: 12px; }
.btn-danger:hover { color: #f44336; }
.btn-sm { padding: 6px 14px; font-size: 12px; }
.btn:disabled { opacity: 0.5; cursor: wait; }

/* ---- section add form ---- */
.add-section {
    background: #151530;
    border: 1px dashed #2a2a4a;
    border-radius: 10px;
    padding: 18px 20px;
    margin-bottom: 20px;
}
.add-section summary {
    cursor: pointer;
    color: #7b68ee;
    font-size: 14px;
    user-select: none;
}
.add-section summary:hover { color: #9b88ff; }
.add-section .form-grid {
    display: grid;
    grid-template-columns: 1fr 2fr 120px;
    gap: 10px;
    margin-top: 14px;
    align-items: end;
}
.add-section label {
    font-size: 11px;
    color: #666;
    text-transform: uppercase;
    display: block;
    margin-bottom: 4px;
}
.add-section input {
    width: 100%;
    padding: 9px 12px;
    background: #1a1a35;
    border: 1px solid #2a2a4a;
    border-radius: 6px;
    color: #ccc;
    font-size: 13px;
    outline: none;
}
.add-section input:focus { border-color: #533483; }

/* ---- section card ---- */
.section-card {
    background: #151530;
    border: 1px solid #1e1e3a;
    border-radius: 10px;
    margin-bottom: 6px;
    overflow: hidden;
}
.section-header {
    display: flex;
    align-items: center;
    padding: 14px 18px;
    background: #1a1a35;
    border-bottom: 1px solid #1e1e3a;
    gap: 12px;
}
.section-header .sec-name {
    font-weight: 600;
    font-size: 14px;
    color: #ccc;
}
.section-header .sec-meta {
    font-size: 11px;
    color: #555;
    margin-left: 8px;
}
.section-header .badges { margin-left: auto; display: flex; gap: 6px; align-items: center; }
.badge {
    font-size: 11px;
    padding: 3px 10px;
    border-radius: 12px;
    font-weight: 600;
}
.badge-ok { background: #0a3d2a; color: #4caf50; }
.badge-changes { background: #3d0a0a; color: #ff6b6b; }
.badge-first { background: #3d2e0a; color: #ff9800; }
.badge-actual { background: #0a1a3d; color: #64b5f6; }
.badge-time { background: #1a1a3a; color: #666; }
.badge-processed-btn {
    background: #2e7d32; color: #fff; border: none; cursor: pointer;
    font-size: 11px; padding: 3px 10px; border-radius: 12px; font-weight: 600;
    display: none; align-items: center; gap: 4px;
    transition: background 0.15s;
}
.badge-processed-btn:hover { background: #388e3c; }
.badge-processed-btn.visible { display: inline-flex; }
.badge-actualized-btn {
    background: #1565c0; color: #fff; border: none; cursor: pointer;
    font-size: 11px; padding: 3px 10px; border-radius: 12px; font-weight: 600;
    display: none; align-items: center; gap: 4px;
    transition: background 0.15s;
}
.badge-actualized-btn:hover { background: #1976d2; }
.badge-actualized-btn.visible { display: inline-flex; }
.badge-cancel-btn {
    background: #616161; color: #fff; border: none; cursor: pointer;
    font-size: 11px; padding: 3px 10px; border-radius: 12px; font-weight: 600;
    display: none; align-items: center; gap: 4px;
    transition: background 0.15s;
}
.badge-cancel-btn:hover { background: #757575; }
.badge-cancel-btn.visible { display: inline-flex; }

.section-body { padding: 0; }

.alert {
    padding: 12px 18px;
    margin: 12px;
    border-radius: 6px;
    font-size: 13px;
}
.alert-error { background: #3d0a0a; color: #ef9a9a; border-left: 3px solid #f44336; }
.alert-warn  { background: #3d2e0a; color: #ffb74d; border-left: 3px solid #ff9800; }
.alert-ok    { background: #0a3d2a; color: #81c784; border-left: 3px solid #4caf50; }

/* ---- toggle ---- */
.toggle-btn {
    background: none;
    border: 1px solid #2a2a4a;
    color: #888;
    padding: 3px 10px;
    border-radius: 4px;
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
}
.toggle-btn:hover { border-color: #7b68ee; color: #ccc; }
.section-body.collapsed { display: none; }

/* ---- grid table ---- */
.grid-wrap { overflow-x: auto; }
.grid {
    border-collapse: collapse;
    font-size: 13px;
    table-layout: fixed;
    width: 0;
    border: 2px solid #2a2a5a;
}
.grid th {
    background: #12122a;
    color: #8899bb;
    padding: 9px 14px;
    text-align: center;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    border: 2px solid #2a2a5a;
    position: relative;
    min-width: 30px;
}
.grid th .resize-handle {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    width: 5px;
    cursor: col-resize;
    background: transparent;
}
.grid th .resize-handle:hover,
.grid th .resize-handle.resizing { background: #7b68ee; }
.grid .row-num {
    background: #12122a;
    color: #8899bb;
    text-align: center;
    font-size: 12px;
    font-weight: 700;
    padding: 9px 10px;
    min-width: 40px;
    border: 2px solid #2a2a5a;
}
.grid td {
    padding: 8px 14px;
    border: 1px solid #2a2a5a;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.grid tr:hover td:not(.row-num) { background: #1a1a40; }
.grid td.has-link {
    padding: 8px 6px;
}
.grid td.has-link .cell-link-wrap {
    display: flex;
    align-items: center;
    gap: 4px;
}
.grid td.has-link .cell-text {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.grid td.has-link .cell-link-icon {
    flex-shrink: 0;
    color: #7b9bff;
    font-size: 13px;
    text-decoration: none;
    opacity: 0.7;
    transition: opacity 0.15s;
}
.grid td.has-link .cell-link-icon:hover {
    opacity: 1;
    text-decoration: underline;
}
/* Явная ширина первых столбцов */
.grid th:first-child { width: 40px; }
.grid th[data-col="A"] { width: 80px; }
.grid th[data-col="B"] { width: 200px; }
.grid th[data-col="C"] { width: 300px; }
/* Узкие столбцы после C */
.grid th.col-narrow { width: 30px; max-width: 50px; }
.grid td.col-narrow {
    max-width: 50px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    padding: 8px 4px;
    text-align: center;
    font-size: 11px;
}
.cell-changed {
    background: #3d0a0a !important;
    color: #ff6b6b !important;
    font-weight: 700;
    border-left: 4px solid #f44336 !important;
    cursor: pointer;
}
.cell-changed:hover { opacity: 0.85; }
.cell-selected {
    background: #5a1a1a !important;
    color: #ff9b9b !important;
    font-weight: 700;
    border-left: 4px solid #ff9800 !important;
    outline: 2px solid #ff9800;
    outline-offset: -2px;
    cursor: pointer;
}
.cell-processed {
    background: #0a3d1a !important;
    color: #4caf50 !important;
    font-weight: 700;
    border-left: 4px solid #4caf50 !important;
}
.cell-actualized {
    background: #0a1a3d !important;
    color: #64b5f6 !important;
    font-weight: 700;
    border-left: 4px solid #42a5f5 !important;
}
.cell-linkable { cursor: pointer; }
.cell-linkable:hover { background: rgba(66, 165, 245, 0.15); }

/* ---- column selection & hiding ---- */
.grid th.col-header {
    cursor: pointer;
    user-select: none;
    transition: background 0.15s;
}
.grid th.col-header:hover { background: #1a1a50; }
.grid th.col-selected {
    background: #2a1a50 !important;
    color: #b388ff !important;
    border-bottom: 3px solid #7b68ee;
}
.col-hidden { display: none !important; }
.col-hidden.show-hidden-col {
    display: table-cell !important;
    opacity: 0.35;
}
.col-hidden.show-hidden-col:hover { opacity: 0.6; }

/* ---- hide bar ---- */
.hide-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 18px;
    background: #12122a;
    border-bottom: 1px solid #1e1e3a;
    font-size: 13px;
}
.btn-hide {
    padding: 5px 14px;
    background: #533483;
    color: #fff;
    border: none;
    border-radius: 5px;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
}
.btn-hide:hover { background: #7b68ee; }
.btn-hide.danger { background: #6b2020; }
.btn-hide.danger:hover { background: #c0392b; }
.btn-hide-toggle {
    padding: 5px 14px;
    background: none;
    color: #888;
    border: 1px solid #2a2a4a;
    border-radius: 5px;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
}
.btn-hide-toggle:hover { border-color: #7b68ee; color: #ccc; }
.btn-hide-toggle.active { border-color: #7b68ee; color: #7b68ee; }
.hide-bar .spacer { flex: 1; }
.hide-bar .info { color: #666; font-size: 12px; }

/* ---- changes indicator ---- */
.changes-bar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 12px 20px;
    margin-bottom: 20px;
    background: #1a1a35;
    border: 1px solid #2a2a4a;
    border-radius: 10px;
}
.changes-bar .stat {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 14px;
}
.changes-bar .stat-num {
    font-size: 22px;
    font-weight: 700;
}
.changes-bar .stat-num.red { color: #ff6b6b; }
.changes-bar .stat-num.green { color: #4caf50; }
.changes-bar .stat-num.blue { color: #64b5f6; }
.changes-bar .stat-num.gray { color: #666; }
.changes-bar .stat-label { color: #888; font-size: 12px; }
.changes-bar .divider {
    width: 1px;
    height: 30px;
    background: #2a2a4a;
}

/* ---- section edit form ---- */
.sec-edit-btn {
    background: none;
    border: 1px solid #2a2a4a;
    color: #666;
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
}
.sec-edit-btn:hover { border-color: #7b68ee; color: #aaa; }
.sec-edit-form {
    display: none;
    padding: 12px 18px;
    background: #12122a;
    border-bottom: 1px solid #1e1e3a;
    gap: 10px;
    align-items: end;
}
.sec-edit-form.open { display: flex; }
.sec-edit-form input {
    padding: 7px 10px;
    background: #1a1a35;
    border: 1px solid #2a2a4a;
    border-radius: 5px;
    color: #ccc;
    font-size: 13px;
    outline: none;
}
.sec-edit-form input:focus { border-color: #533483; }
.sec-edit-form .ef-name { width: 160px; }
.sec-edit-form .ef-url { flex: 1; min-width: 200px; }
.sec-edit-form .ef-range { width: 100px; }
.sec-edit-form button {
    padding: 7px 14px;
    border: none;
    border-radius: 5px;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
}
.sec-edit-form .ef-save { background: #533483; color: #fff; }
.sec-edit-form .ef-save:hover { background: #7b68ee; }
.sec-edit-form .ef-cancel { background: #2a2a4a; color: #aaa; }
.sec-edit-form .ef-cancel:hover { background: #3a3a5a; }


/* ---- empty state ---- */
.empty-state {
    text-align: center;
    padding: 80px 20px;
    color: #444;
}
.empty-state h3 { font-size: 20px; color: #555; margin-bottom: 8px; }
.empty-state p { font-size: 14px; }
</style>
</head>
<body>

<!-- SIDEBAR -->
<aside class="sidebar">
    <div class="sidebar-head">
        <h1>Sheets Watcher</h1>
        <span>Мониторинг таблиц</span>
    </div>
    <ul class="project-list">
        {% for p in projects %}
        <li class="{{ 'active' if current and current.id == p.id else '' }}">
            <a href="/project/{{ p.id }}">
                {{ p.name }}
                <span class="cnt">{{ p.sections|length }}</span>
            </a>
        </li>
        {% endfor %}
    </ul>
    <div class="sidebar-add">
        <form method="POST" action="/project/add">
            <input name="name" placeholder="Новый проект..." required>
            <button type="submit" class="btn-add">+</button>
        </form>
    </div>
</aside>

<!-- MAIN -->
<main class="main">
{% if not current %}
    <div class="empty-state">
        <h3>Добавьте проект</h3>
        <p>Введите название в поле слева и нажмите +</p>
    </div>
{% else %}
    <div class="main-head">
        <h2 class="project-title" id="projectTitle">
            <span class="project-name" ondblclick="showRenameForm()">{{ current.name }}</span>
            <button class="btn-rename" onclick="showRenameForm()" title="Переименовать">&#9998;</button>
        </h2>
        <form method="POST" action="/project/{{ current.id }}/rename" id="renameForm" class="rename-form" style="display:none">
            <input name="name" value="{{ current.name }}" class="rename-input" required>
            <button type="submit" class="btn btn-small btn-primary">OK</button>
            <button type="button" class="btn btn-small" onclick="hideRenameForm()">Отмена</button>
        </form>
        {% if last_check_date %}
        <span class="last-check-info">Дата обновления таблицы: <strong>{{ last_check_date }}</strong></span>
        {% endif %}
        <form method="POST" action="/project/{{ current.id }}/check" id="checkForm">
            <button class="btn btn-primary" id="btnCheckAll" type="button"
                onclick="confirmCheck(this)">
                Проверить все
            </button>
        </form>
        <form method="POST" action="/project/{{ current.id }}/delete"
              onsubmit="return confirm('Удалить проект?')">
            <button class="btn btn-danger">удалить проект</button>
        </form>
    </div>

    {% set ns = namespace(total_changed=0, total_processed=0, total_actualized=0) %}
    {% for sec in current.sections %}
        {% set d = sections_data.get(sec.id, {}) %}
        {% if d.get('changed') %}
            {% set ns.total_changed = ns.total_changed + d.changed|length %}
        {% endif %}
        {% if d.get('processed') %}
            {% set ns.total_processed = ns.total_processed + d.processed|length %}
        {% endif %}
        {% if d.get('actualized') %}
            {% set ns.total_actualized = ns.total_actualized + d.actualized|length %}
        {% endif %}
    {% endfor %}
    {% set unprocessed = ns.total_changed - ns.total_processed - ns.total_actualized %}

    {% if ns.total_changed > 0 %}
    <div class="changes-bar" id="changesBar">
        <div class="stat">
            <span class="stat-num red" id="unprocessedCount">{{ unprocessed if unprocessed > 0 else 0 }}</span>
            <span class="stat-label">не отработано</span>
        </div>
        <div class="divider"></div>
        <div class="stat">
            <span class="stat-num green" id="processedCount">{{ ns.total_processed }}</span>
            <span class="stat-label">отработано</span>
        </div>
        <div class="divider"></div>
        <div class="stat">
            <span class="stat-num blue" id="actualizedCount">{{ ns.total_actualized }}</span>
            <span class="stat-label">актуализировано</span>
        </div>
        <div class="divider"></div>
        <div class="stat">
            <span class="stat-num gray">{{ ns.total_changed }}</span>
            <span class="stat-label">всего изменений</span>
        </div>
    </div>
    {% endif %}

    {% if sections_data.get('_error_' + current.id) %}
        <div class="alert alert-error">
            {{ sections_data['_error_' + current.id].error }}
        </div>
    {% endif %}

    <!-- Add section -->
    <details class="add-section" {{ 'open' if current.sections|length == 0 else '' }}>
        <summary>+ Добавить раздел</summary>
        <form method="POST" action="/project/{{ current.id }}/section/add">
            <div class="form-grid">
                <div>
                    <label>Название</label>
                    <input name="name" placeholder="Напр: Битрикс">
                </div>
                <div>
                    <label>Ссылка на таблицу</label>
                    <input name="url" placeholder="https://docs.google.com/spreadsheets/d/..." required>
                </div>
                <div>
                    <label>Диапазон</label>
                    <input name="range" placeholder="A1:E20 или A1:C10, F1:F10" required>
                </div>
            </div>
            <div style="margin-top: 12px;">
                <button class="btn btn-primary btn-sm">Добавить</button>
            </div>
        </form>
    </details>

    <!-- Sections -->
    {% for sec in current.sections %}
    {% set data = sections_data.get(sec.id, {}) %}
    {% set proc = data.get('processed', []) %}
    {% set act = data.get('actualized', []) %}
    {% set hidden_cols = data.get('hidden_cols', []) %}
    <div class="section-card" data-sid="{{ sec.id }}" data-pid="{{ current.id }}">
        <div class="section-header">
            <button class="toggle-btn" onclick="toggleSection(this)"
                title="Свернуть/развернуть">{{ ('&#9654;' if data.get('rows') or data.get('error') else '&#9660;')|safe }}</button>
            <span class="sec-name">{{ sec.name }}</span>
            <span class="sec-meta">{{ sec.range }}</span>
            <button class="sec-edit-btn" onclick="toggleEditForm(this)" title="Редактировать">&#9998;</button>
            <div class="badges">
                <button class="badge-processed-btn" data-sid="{{ sec.id }}"
                    onclick="markSectionProcessed(this)">&#10003; Отработано (<span class="sec-sel-cnt">0</span>)</button>
                <button class="badge-actualized-btn" data-sid="{{ sec.id }}"
                    onclick="markSectionActualized(this)">&#9733; Актуально (<span class="sec-act-cnt">0</span>)</button>
                <button class="badge-cancel-btn" data-sid="{{ sec.id }}"
                    onclick="cancelSelection(this)">&#10005; Отмена (<span class="sec-cancel-cnt">0</span>)</button>
                {% if data.get('error') %}
                    <span class="badge badge-changes">ошибка</span>
                {% elif data.get('first_run') %}
                    <span class="badge badge-first">сохранено</span>
                {% elif data.get('total', 0) > 0 %}
                    {% set unproc = data.changed|reject('in', proc)|reject('in', act)|list|length %}
                    {% if unproc > 0 %}
                        <span class="badge badge-changes">{{ unproc }} изм.</span>
                    {% else %}
                        <span class="badge badge-ok">OK</span>
                    {% endif %}
                    {% if proc|length > 0 %}
                        <span class="badge badge-ok">{{ proc|length }} отр.</span>
                    {% endif %}
                    {% if act|length > 0 %}
                        <span class="badge badge-actual">{{ act|length }} акт.</span>
                    {% endif %}
                {% elif data.get('checked_at') %}
                    <span class="badge badge-ok">OK</span>
                {% endif %}
                {% if data.get('checked_at') %}
                    <span class="badge badge-time">{{ data.checked_at }}</span>
                {% endif %}
                <form method="POST" action="/project/{{ current.id }}/section/{{ sec.id }}/delete"
                      onsubmit="return confirm('Удалить раздел?')" style="margin:0">
                    <button class="btn btn-danger btn-sm">x</button>
                </form>
            </div>
        </div>
        <form class="sec-edit-form" method="POST"
              action="/project/{{ current.id }}/section/{{ sec.id }}/edit">
            <input class="ef-name" name="name" value="{{ sec.name }}" placeholder="Название">
            <input class="ef-url" name="url" value="{{ sec.url }}" placeholder="Ссылка на таблицу">
            <input class="ef-range" name="range" value="{{ sec.range }}" placeholder="Диапазон">
            <button type="submit" class="ef-save">Сохранить</button>
            <button type="button" class="ef-cancel" onclick="toggleEditForm(this)">Отмена</button>
        </form>
        <div class="section-body{{ ' collapsed' if data.get('rows') or data.get('error') else '' }}">
            {% if data.get('error') %}
                <div class="alert alert-error">{{ data.error }}</div>
            {% elif data.get('first_run') %}
                <div class="alert alert-warn">Первый запуск — состояние сохранено</div>
            {% endif %}

            {% if data.get('rows') %}
            <div class="hide-bar">
                <button class="btn-hide" onclick="hideSelectedCols(this)" style="display:none"
                    data-sid="{{ sec.id }}">
                    Скрыть выделенные (<span class="col-sel-cnt">0</span>)
                </button>
                {% if hidden_cols|length > 0 %}
                <button class="btn-hide-toggle" onclick="toggleHiddenCols(this)"
                    data-sid="{{ sec.id }}">
                    Показать скрытые ({{ hidden_cols|length }})
                </button>
                <button class="btn-hide danger" onclick="unhideAllCols(this)" style="display:none"
                    data-sid="{{ sec.id }}">
                    Вернуть все скрытые
                </button>
                {% endif %}
                <span class="spacer"></span>
                <span class="info">Клик по заголовку столбца — выделить</span>
            </div>
            <div class="grid-wrap">
                <table class="grid">
                    <thead>
                        <tr>
                            <th></th>
                            {% for col in data.col_headers %}
                            {% set is_narrow = col > 'C' and col != '' %}
                            <th class="col-header{{ ' col-narrow' if is_narrow else '' }}{{ ' col-hidden' if col in hidden_cols else '' }}"
                                data-col="{{ col }}" onclick="toggleColSelect(this)">{{ col }}<div class="resize-handle" onmousedown="startResize(event, this)"></div></th>
                            {% endfor %}
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in data.rows %}
                        <tr>
                            <td class="row-num">{{ row.num }}</td>
                            {% for cell in row.cells %}
                            {% set col_letter = data.col_headers[loop.index0] %}
                            {% set is_col_hidden = col_letter in hidden_cols %}
                            {% set is_narrow = col_letter > 'C' and col_letter != '' %}
                            {% if cell.addr in act %}
                            <td class="cell-actualized{{ ' col-narrow' if is_narrow else '' }}{{ ' has-link' if cell.link else '' }}{{ ' col-hidden' if is_col_hidden else '' }}"
                                data-col="{{ col_letter }}"
                                data-addr="{{ cell.addr }}" data-sid="{{ sec.id }}"
                                onclick="toggleCellSelect(this, event)"
                                title="{{ cell.addr }} (актуализировано, кликните для смены статуса)">
                            {% elif cell.addr in data.changed and cell.addr in proc %}
                            <td class="cell-processed{{ ' col-narrow' if is_narrow else '' }}{{ ' has-link' if cell.link else '' }}{{ ' col-hidden' if is_col_hidden else '' }}"
                                data-col="{{ col_letter }}"
                                data-addr="{{ cell.addr }}" data-sid="{{ sec.id }}"
                                onclick="toggleCellSelect(this, event)"
                                title="{{ cell.addr }} (отработано, кликните для актуализации)">
                            {% elif cell.addr in data.changed %}
                            <td class="cell-changed{{ ' col-narrow' if is_narrow else '' }}{{ ' has-link' if cell.link else '' }}{{ ' col-hidden' if is_col_hidden else '' }}"
                                data-col="{{ col_letter }}"
                                data-addr="{{ cell.addr }}" data-sid="{{ sec.id }}"
                                onclick="toggleCellSelect(this, event)"
                                title="{{ cell.addr }} (кликните для выделения)">
                            {% else %}
                            <td class="{{ 'col-narrow ' if is_narrow else '' }}{{ 'has-link ' if cell.link else '' }}{{ 'cell-linkable ' if cell.link else '' }}{{ 'col-hidden' if is_col_hidden else '' }}"
                                data-col="{{ col_letter }}"
                                {% if cell.link %}data-addr="{{ cell.addr }}" data-sid="{{ sec.id }}"
                                onclick="toggleCellSelect(this, event)"{% endif %}
                                title="{{ cell.addr }}">
                            {% endif %}
                                {% if cell.link %}
                                    <a href="{{ cell.link }}" target="_blank"
                                       onclick="event.stopPropagation()"
                                       class="cell-link-icon"
                                       title="{{ cell.link }}">&#8599;</a>
                                {% else %}
                                    {{ cell.value }}
                                {% endif %}
                            </td>
                            {% endfor %}
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            {% endif %}
        </div>
    </div>
    {% endfor %}

    {% if current.sections|length == 0 %}
        <div class="empty-state" style="padding: 40px;">
            <p>Нет разделов — добавьте первый выше</p>
        </div>
    {% endif %}
{% endif %}
</main>

<script>
var selectedCells = [];
var projectId = '{{ current.id if current else "" }}';

function showRenameForm() {
    document.getElementById('projectTitle').style.display = 'none';
    var form = document.getElementById('renameForm');
    form.style.display = 'flex';
    form.querySelector('input').focus();
    form.querySelector('input').select();
}
function hideRenameForm() {
    document.getElementById('projectTitle').style.display = 'flex';
    document.getElementById('renameForm').style.display = 'none';
}

function confirmCheck(btn) {
    var lastCheck = '{{ last_check_date }}';
    if (lastCheck) {
        var today = new Date().toLocaleDateString('ru-RU', {day:'2-digit', month:'2-digit', year:'numeric'});
        var checkDate = lastCheck.split(' ')[0];
        if (checkDate === today) {
            if (!confirm('Таблица уже была обновлена сегодня (' + lastCheck + '). Обновить повторно?')) {
                return;
            }
        }
    }
    btn.disabled = true;
    btn.innerText = 'Загрузка...';
    document.getElementById('checkForm').submit();
}

function toggleSection(btn) {
    var body = btn.closest('.section-card').querySelector('.section-body');
    body.classList.toggle('collapsed');
    btn.innerHTML = body.classList.contains('collapsed') ? '&#9654;' : '&#9660;';
}

function toggleEditForm(btn) {
    var card = btn.closest('.section-card');
    var form = card.querySelector('.sec-edit-form');
    form.classList.toggle('open');
}

// ---- Column resize ----
var resizeState = null;
function startResize(e, handle) {
    e.stopPropagation();
    e.preventDefault();
    var th = handle.parentElement;
    resizeState = { th: th, startX: e.pageX, startW: th.offsetWidth };
    handle.classList.add('resizing');
    document.addEventListener('mousemove', doResize);
    document.addEventListener('mouseup', stopResize);
}
function doResize(e) {
    if (!resizeState) return;
    var newW = Math.max(30, resizeState.startW + (e.pageX - resizeState.startX));
    resizeState.th.style.width = newW + 'px';
}
function stopResize() {
    if (resizeState) {
        var handle = resizeState.th.querySelector('.resize-handle');
        if (handle) handle.classList.remove('resizing');
        resizeState = null;
    }
    document.removeEventListener('mousemove', doResize);
    document.removeEventListener('mouseup', stopResize);
}

// ---- Column selection & hiding ----

function toggleColSelect(th) {
    th.classList.toggle('col-selected');
    updateColSelectionUI(th.closest('.section-card'));
}

function updateColSelectionUI(card) {
    var selected = card.querySelectorAll('th.col-selected');
    var hideBtn = card.querySelector('.btn-hide');
    var cntSpan = card.querySelector('.col-sel-cnt');
    if (hideBtn && cntSpan) {
        cntSpan.textContent = selected.length;
        hideBtn.style.display = selected.length > 0 ? '' : 'none';
    }
}

function getColCells(card, colName) {
    return card.querySelectorAll('[data-col="' + colName + '"]');
}

function hideSelectedCols(btn) {
    var sid = btn.getAttribute('data-sid');
    var card = btn.closest('.section-card');
    var pid = card.getAttribute('data-pid');
    var selectedThs = card.querySelectorAll('th.col-selected');
    var colNames = [];

    selectedThs.forEach(function(th) {
        colNames.push(th.getAttribute('data-col'));
    });

    if (colNames.length === 0) return;

    fetch('/project/' + pid + '/section/' + sid + '/hide-cols', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cols: colNames})
    }).then(function(resp) { return resp.json(); }).then(function(data) {
        if (!data.ok) return;
        colNames.forEach(function(col) {
            getColCells(card, col).forEach(function(el) {
                el.classList.remove('col-selected');
                el.classList.add('col-hidden');
            });
        });
        updateColSelectionUI(card);
        updateColHideBar(card, sid, pid);
    });
}

function toggleHiddenCols(btn) {
    var card = btn.closest('.section-card');
    var hiddenEls = card.querySelectorAll('.col-hidden');
    var showing = btn.classList.toggle('active');
    var unhideBtn = card.querySelector('.btn-hide.danger');

    hiddenEls.forEach(function(el) {
        if (showing) {
            el.classList.add('show-hidden-col');
        } else {
            el.classList.remove('show-hidden-col');
        }
    });

    if (unhideBtn) {
        unhideBtn.style.display = showing ? '' : 'none';
    }
}

function unhideAllCols(btn) {
    var sid = btn.getAttribute('data-sid');
    var card = btn.closest('.section-card');
    var pid = card.getAttribute('data-pid');

    fetch('/project/' + pid + '/section/' + sid + '/unhide-cols', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cols: []})
    }).then(function(resp) { return resp.json(); }).then(function(data) {
        if (!data.ok) return;
        card.querySelectorAll('.col-hidden').forEach(function(el) {
            el.classList.remove('col-hidden', 'show-hidden-col');
        });
        updateColHideBar(card, sid, pid);
    });
}

function updateColHideBar(card, sid, pid) {
    var hideBar = card.querySelector('.hide-bar');
    var hiddenCols = new Set();
    card.querySelectorAll('th.col-hidden').forEach(function(th) {
        hiddenCols.add(th.getAttribute('data-col'));
    });
    var hiddenCount = hiddenCols.size;

    var toggleBtn = hideBar.querySelector('.btn-hide-toggle');
    var unhideBtn = hideBar.querySelector('.btn-hide.danger');

    if (hiddenCount > 0) {
        if (!toggleBtn) {
            toggleBtn = document.createElement('button');
            toggleBtn.className = 'btn-hide-toggle';
            toggleBtn.setAttribute('data-sid', sid);
            toggleBtn.onclick = function() { toggleHiddenCols(toggleBtn); };
            hideBar.querySelector('.spacer').before(toggleBtn);
        }
        toggleBtn.textContent = 'Показать скрытые (' + hiddenCount + ')';
        toggleBtn.style.display = '';

        if (!unhideBtn) {
            unhideBtn = document.createElement('button');
            unhideBtn.className = 'btn-hide danger';
            unhideBtn.setAttribute('data-sid', sid);
            unhideBtn.style.display = 'none';
            unhideBtn.textContent = 'Вернуть все скрытые';
            unhideBtn.onclick = function() { unhideAllCols(unhideBtn); };
            toggleBtn.after(unhideBtn);
        }
    } else {
        if (toggleBtn) toggleBtn.style.display = 'none';
        if (unhideBtn) unhideBtn.style.display = 'none';
    }
}

function toggleCellSelect(td, ev) {
    if (ev) ev.preventDefault();
    var addr = td.getAttribute('data-addr');
    var sid = td.getAttribute('data-sid');
    var wasProcessed = td.classList.contains('cell-processed');
    var wasActualized = td.classList.contains('cell-actualized');
    var wasChanged = td.classList.contains('cell-changed');
    var wasNormal = !wasProcessed && !wasChanged && !wasActualized;
    var idx = selectedCells.findIndex(function(c) { return c.addr === addr && c.sid === sid; });
    if (idx >= 0) {
        selectedCells.splice(idx, 1);
        td.classList.remove('cell-selected');
        if (td._wasProcessed) {
            td.classList.add('cell-processed');
        } else if (td._wasActualized) {
            td.classList.add('cell-actualized');
        } else if (td._wasNormal) {
            // Обычная ячейка — не добавляем ничего
        } else {
            td.classList.add('cell-changed');
        }
        td._wasProcessed = false;
        td._wasActualized = false;
        td._wasNormal = false;
    } else {
        td._wasProcessed = wasProcessed;
        td._wasActualized = wasActualized;
        td._wasNormal = wasNormal;
        selectedCells.push({addr: addr, sid: sid, el: td});
        td.classList.remove('cell-changed', 'cell-processed', 'cell-actualized', 'cell-linkable');
        td.classList.add('cell-selected');
    }
    updateSelectedUI();
}

function updateSelectedUI() {
    // Кнопки "Отработано" в заголовках разделов
    var countBySid = {};
    selectedCells.forEach(function(c) {
        countBySid[c.sid] = (countBySid[c.sid] || 0) + 1;
    });
    document.querySelectorAll('.badge-processed-btn').forEach(function(b) {
        var sid = b.getAttribute('data-sid');
        var n = countBySid[sid] || 0;
        var span = b.querySelector('.sec-sel-cnt');
        if (span) span.textContent = n;
        if (n > 0) {
            b.classList.add('visible');
        } else {
            b.classList.remove('visible');
        }
    });
    // Кнопки "Актуально" в заголовках разделов
    document.querySelectorAll('.badge-actualized-btn').forEach(function(b) {
        var sid = b.getAttribute('data-sid');
        var n = countBySid[sid] || 0;
        var span = b.querySelector('.sec-act-cnt');
        if (span) span.textContent = n;
        if (n > 0) {
            b.classList.add('visible');
        } else {
            b.classList.remove('visible');
        }
    });
    // Кнопки "Отмена" в заголовках разделов
    document.querySelectorAll('.badge-cancel-btn').forEach(function(b) {
        var sid = b.getAttribute('data-sid');
        var n = countBySid[sid] || 0;
        var span = b.querySelector('.sec-cancel-cnt');
        if (span) span.textContent = n;
        if (n > 0) {
            b.classList.add('visible');
        } else {
            b.classList.remove('visible');
        }
    });
}

function cancelSelection(btn) {
    var sid = btn.getAttribute('data-sid');
    var sectionCells = selectedCells.filter(function(c) { return c.sid === sid; });
    if (sectionCells.length === 0) return;
    sectionCells.forEach(function(c) {
        c.el.classList.remove('cell-selected', 'cell-changed', 'cell-processed', 'cell-actualized', 'cell-linkable');
        c.el._wasProcessed = false;
        c.el._wasActualized = false;
        c.el._wasNormal = false;
    });
    selectedCells = selectedCells.filter(function(c) { return c.sid !== sid; });
    updateSelectedUI();
}

function markSectionProcessed(btn) {
    var sid = btn.getAttribute('data-sid');
    var sectionCells = selectedCells.filter(function(c) { return c.sid === sid; });
    if (sectionCells.length === 0) return;

    var addrs = sectionCells.map(function(c) { return c.addr; });
    fetch('/project/' + projectId + '/section/' + sid + '/mark-processed', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cells: addrs})
    }).then(function() {
        sectionCells.forEach(function(c) {
            c.el.classList.remove('cell-selected', 'cell-changed', 'cell-actualized');
            c.el.classList.add('cell-processed');
            c.el.setAttribute('onclick', 'toggleCellSelect(this, event)');
            c.el.title = c.addr + ' (отработано, кликните для смены статуса)';
        });

        // Убираем из selectedCells
        selectedCells = selectedCells.filter(function(c) { return c.sid !== sid; });

        // Обновляем общие счётчики
        var processedEl = document.getElementById('processedCount');
        var unprocessedEl = document.getElementById('unprocessedCount');
        if (processedEl && unprocessedEl) {
            processedEl.textContent = parseInt(processedEl.textContent) + sectionCells.length;
            unprocessedEl.textContent = Math.max(0, parseInt(unprocessedEl.textContent) - sectionCells.length);
        }

        _updateSectionBadges(sid);
        updateSelectedUI();
    });
}

function markSectionActualized(btn) {
    var sid = btn.getAttribute('data-sid');
    var sectionCells = selectedCells.filter(function(c) { return c.sid === sid; });
    if (sectionCells.length === 0) return;

    var addrs = sectionCells.map(function(c) { return c.addr; });
    fetch('/project/' + projectId + '/section/' + sid + '/mark-actualized', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cells: addrs})
    }).then(function() {
        sectionCells.forEach(function(c) {
            c.el.classList.remove('cell-selected', 'cell-changed', 'cell-processed', 'cell-linkable');
            c.el.classList.add('cell-actualized');
            c.el.setAttribute('onclick', 'toggleCellSelect(this, event)');
            c.el.title = c.addr + ' (актуализировано, кликните для смены статуса)';
        });

        selectedCells = selectedCells.filter(function(c) { return c.sid !== sid; });

        // Обновляем общий счётчик актуализированных
        var actualizedEl = document.getElementById('actualizedCount');
        if (actualizedEl) {
            actualizedEl.textContent = parseInt(actualizedEl.textContent) + sectionCells.length;
        }

        _updateSectionBadges(sid);
        updateSelectedUI();
    });
}

function _updateSectionBadges(sid) {
    var card = document.querySelector('.section-card[data-sid="' + sid + '"]');
    if (!card) return;
    var badges = card.querySelector('.badges');
    var grid = card.querySelector('.grid');
    if (!badges || !grid) return;

    var changedCount = grid.querySelectorAll('.cell-changed').length;
    var processedCount = grid.querySelectorAll('.cell-processed').length;
    var actualizedCount = grid.querySelectorAll('.cell-actualized').length;

    var oldBadges = badges.querySelectorAll('.badge-changes, .badge-ok, .badge-actual');
    oldBadges.forEach(function(b) { b.remove(); });

    var timeBadge = badges.querySelector('.badge-time');
    var insertBefore = timeBadge || badges.querySelector('form');

    if (changedCount > 0) {
        var b = document.createElement('span');
        b.className = 'badge badge-changes';
        b.textContent = changedCount + ' изм.';
        badges.insertBefore(b, insertBefore);
    }
    if (processedCount > 0) {
        var b2 = document.createElement('span');
        b2.className = 'badge badge-ok';
        b2.textContent = processedCount + ' отр.';
        badges.insertBefore(b2, insertBefore);
    }
    if (actualizedCount > 0) {
        var b3 = document.createElement('span');
        b3.className = 'badge badge-actual';
        b3.textContent = actualizedCount + ' акт.';
        badges.insertBefore(b3, insertBefore);
    }
    if (changedCount === 0) {
        var b4 = document.createElement('span');
        b4.className = 'badge badge-ok';
        b4.textContent = 'OK';
        badges.insertBefore(b4, insertBefore);
    }
}

function markSelectedProcessed() {
    if (selectedCells.length === 0) return;

    var bySid = {};
    selectedCells.forEach(function(c) {
        if (!bySid[c.sid]) bySid[c.sid] = [];
        bySid[c.sid].push(c);
    });

    var promises = Object.keys(bySid).map(function(sid) {
        var cells = bySid[sid].map(function(c) { return c.addr; });
        return fetch('/project/' + projectId + '/section/' + sid + '/mark-processed', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({cells: cells})
        });
    });

    Promise.all(promises).then(function() {
        // Собираем sid → количество отмеченных
        var countBySid = {};
        selectedCells.forEach(function(c) {
            c.el.classList.remove('cell-selected', 'cell-changed');
            c.el.classList.add('cell-processed');
            c.el.removeAttribute('onclick');
            c.el.title = c.addr + ' (отработано)';
            countBySid[c.sid] = (countBySid[c.sid] || 0) + 1;
        });

        // Обновляем общие счётчики
        var processedEl = document.getElementById('processedCount');
        var unprocessedEl = document.getElementById('unprocessedCount');
        if (processedEl && unprocessedEl) {
            var newProcessed = parseInt(processedEl.textContent) + selectedCells.length;
            var newUnprocessed = parseInt(unprocessedEl.textContent) - selectedCells.length;
            processedEl.textContent = newProcessed;
            unprocessedEl.textContent = Math.max(0, newUnprocessed);
        }

        // Обновляем бейджи на каждом разделе
        Object.keys(countBySid).forEach(function(sid) {
            _updateSectionBadges(sid);
        });

        selectedCells = [];
        updateSelectedUI();
    });
}

// При загрузке: свернуть все секции, у которых есть данные
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.section-card').forEach(function(card) {
        var body = card.querySelector('.section-body');
        var grid = body && body.querySelector('.grid');
        var alertEl = body && body.querySelector('.alert');
        if (grid || alertEl) {
            body.classList.add('collapsed');
            var btn = card.querySelector('.toggle-btn');
            if (btn) btn.innerHTML = '&#9654;';
        }
    });
});
</script>
</body>
</html>
"""

# --------------- scheduled auto-check at 06:00 daily ---------------

def _do_auto_check():
    """Проверяет все проекты (аналог project_check) в фоне."""
    try:
        cfg = load_config()
        for proj in cfg.get("projects", []):
            pid = proj["id"]
            old_snapshot = load_snapshot()
            first_run = not old_snapshot
            new_snapshot = dict(old_snapshot)
            sc = load_section_cache()

            try:
                client = get_client()
            except Exception:
                continue

            all_changes = load_changes()

            for idx, sec in enumerate(proj["sections"]):
                if idx > 0:
                    time.sleep(3)
                try:
                    sp_id, gid = parse_url(sec["url"])
                    if not sp_id:
                        continue

                    sheet_title, col_headers, rows, flat = read_grid(
                        client, sp_id, gid, sec["range"]
                    )

                    changes_key = f"{pid}:{sec['id']}"
                    saved_changes = set(all_changes.get(changes_key, []))

                    if not first_run:
                        for key, new_val in flat.items():
                            old_val = old_snapshot.get(key)
                            if old_val is not None and old_val != new_val:
                                addr = key.split("!", 1)[1]
                                saved_changes.add(addr)

                    all_changes[changes_key] = list(saved_changes)
                    changed = list(saved_changes)
                    new_snapshot.update(flat)

                    processed = load_processed()
                    proc_list = processed.get(f"{pid}:{sec['id']}", [])
                    hidden = load_hidden_cols()
                    hidden_list = hidden.get(f"{pid}:{sec['id']}", [])
                    act = load_actualized()
                    act_list = act.get(f"{pid}:{sec['id']}", [])

                    sc[sec["id"]] = {
                        "sheet_title": sheet_title,
                        "col_headers": col_headers,
                        "rows": rows,
                        "changed": changed,
                        "processed": proc_list,
                        "actualized": act_list,
                        "hidden_cols": hidden_list,
                        "total": len(changed),
                        "checked_at": datetime.now().strftime("%H:%M:%S"),
                        "first_run": first_run,
                        "error": None,
                    }
                except Exception:
                    pass

            save_changes(all_changes)
            save_snapshot(new_snapshot)
            save_section_cache(sc)

            lc = load_last_check()
            lc[pid] = datetime.now().strftime("%d.%m.%Y %H:%M")
            save_last_check(lc)

            # Пауза между проектами
            time.sleep(5)
    except Exception:
        pass


def _schedule_loop():
    """Фоновый цикл: запускает проверку ежедневно в 06:00."""
    while True:
        now = datetime.now()
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= target:
            # Сегодня 06:00 уже прошло — следующий запуск завтра
            from datetime import timedelta
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        time.sleep(wait_seconds)
        _do_auto_check()


# Запускаем планировщик при импорте (для gunicorn) и при прямом запуске
_scheduler_thread = threading.Thread(target=_schedule_loop, daemon=True)
_scheduler_thread.start()


if __name__ == "__main__":
    _migrate_to_single_file()
    # Открываем браузер через 1.5 сек после старта сервера
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000)
