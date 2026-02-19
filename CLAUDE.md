# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Описание проекта

**Sheets Watcher** — инструмент мониторинга Google Sheets. Считывает указанные диапазоны ячеек, сохраняет снимок (snapshot) и при повторном запуске показывает изменения по сравнению с предыдущим снимком.

## Команды

```bash
# Установка зависимостей
pip install -r sheets-watcher/requirements.txt

# Запуск веб-интерфейса (Flask, порт 5000)
python sheets-watcher/app.py

# Запуск CLI-проверки (вывод изменений в консоль)
python sheets-watcher/watcher.py
```

## Архитектура

Два режима работы: веб-интерфейс и CLI. `app.py` импортирует `get_client`, `load_snapshot`, `save_snapshot` из `watcher.py`.

- **`app.py`** (~1400 строк) — Flask single-page приложение с inline Jinja2-шаблоном (HTML/CSS/JS в одном файле). Управление проектами и разделами через UI. При "Проверить все" читает ячейки через Sheets API v4, сравнивает со снимком, подсвечивает изменённые ячейки красным. Поддерживает отметку обработанных ячеек (зелёный) и скрытие столбцов.

- **`watcher.py`** (~215 строк) — CLI-режим и библиотека. Авторизация через сервисный аккаунт (`get_client`), чтение диапазонов (`read_ranges`), снимки (`load_snapshot`/`save_snapshot`), сравнение (`compare`), отчёт (`print_report`).

### REST API маршруты (app.py)

| Маршрут | Метод | Назначение |
|---|---|---|
| `/project/<pid>` | GET | Просмотр проекта |
| `/project/add` | POST | Создать проект |
| `/project/<pid>/delete` | POST | Удалить проект |
| `/project/<pid>/section/add` | POST | Добавить раздел |
| `/project/<pid>/section/<sid>/edit` | POST | Редактировать раздел |
| `/project/<pid>/section/<sid>/delete` | POST | Удалить раздел |
| `/project/<pid>/check` | POST | Проверить все разделы |
| `/project/<pid>/section/<sid>/mark-processed` | POST | Отметить ячейки обработанными (JSON) |
| `/project/<pid>/section/<sid>/hide-cols` | POST | Скрыть столбцы (JSON) |
| `/project/<pid>/section/<sid>/unhide-cols` | POST | Показать скрытые столбцы (JSON) |

### Ключевые файлы данных (в `sheets-watcher/`)

| Файл | Назначение |
|---|---|
| `config.json` | Конфигурация проектов/разделов (читается/пишется app.py) |
| `snapshot.json` | Последний снимок ячеек, ключи: `spreadsheet_id!A1-адрес` |
| `processed.json` | Отработанные ячейки, ключи: `project_id:section_id` → `[адреса]` |
| `hidden_cols.json` | Скрытые столбцы, ключи: `project_id:section_id` → `[буквы]` |
| `credentials.json` | JSON-ключ сервисного аккаунта Google (НЕ коммитить) |

### Особенности

- **Два формата конфига**: `app.py` использует `projects[].sections[].{url, range}` с парсингом URL, а `watcher.py` CLI — `spreadsheets[].{id, ranges, sheet}`. Они не взаимозаменяемы.
- Веб-интерфейс хранит результаты проверки в in-memory кеше `section_cache` (сбрасывается при перезапуске).
- `app.py` извлекает `HYPERLINK` формулы из ячеек и рендерит их как кликабельные ссылки.
- Поддержка нескольких диапазонов через запятую в поле `range`: `"A4:C24, G4:Y24"`.
- ID проектов/разделов генерируются как `uuid.uuid4().hex[:8]`.
- Python 3.12+ (используется синтаксис `type | None`).
