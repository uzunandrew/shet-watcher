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

# Деплой на сервер (Windows bat-скрипт, загружает через SCP + перезапускает systemd)
sheets-watcher/deploy.bat
```

## Архитектура

Два режима работы: веб-интерфейс и CLI. `app.py` импортирует `get_client` из `watcher.py` для авторизации Google API.

- **`app.py`** — Flask single-page приложение с inline Jinja2-шаблоном (HTML/CSS/JS в одном файле, ~2200 строк). Управление проектами и разделами через UI. При "Проверить все" читает ячейки через Sheets API v4, сравнивает со снимком, подсвечивает изменённые ячейки красным. Поддерживает отметку обработанных (зелёный), актуализированных ячеек, скрытие столбцов и сброс изменений. Включает фоновый планировщик (`_schedule_loop`) — автопроверку всех проектов ежедневно в 06:00.

- **`watcher.py`** — CLI-режим и библиотека. Авторизация через сервисный аккаунт (`get_client`), чтение диапазонов (`read_ranges`), снимки (`load_snapshot`/`save_snapshot`), сравнение (`compare`), отчёт (`print_report`).

### Хранение данных: единый `data.json`

Всё состояние хранится в `sheets-watcher/data.json` — один JSON-файл со следующими ключами:

| Ключ | Назначение |
|---|---|
| `config` | Конфигурация проектов/разделов (`{projects: [...]}`) |
| `snapshot` | Последний снимок ячеек, ключи: `spreadsheet_id!A1-адрес` |
| `processed` | Отработанные ячейки, ключи: `project_id:section_id` → `[адреса]` |
| `actualized` | Актуализированные ячейки |
| `changes` | Сохранённые изменения между проверками |
| `hidden_cols` | Скрытые столбцы, ключи: `project_id:section_id` → `[буквы]` |
| `last_check` | Время последней проверки по разделам |
| `cache` | Кеш результатов проверки |

При первом запуске `app.py` автоматически мигрирует старые отдельные JSON-файлы (`config.json`, `snapshot.json` и т.д.) в единый `data.json` через `_migrate_to_single_file()`.

Оба модуля используют `PROJECT_DIR` с поддержкой PyInstaller (`sys.frozen`): при сборке в .exe данные лежат рядом с исполняемым файлом.

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
| `/project/<pid>/section/<sid>/mark-actualized` | POST | Отметить ячейки актуализированными (JSON) |
| `/project/<pid>/section/<sid>/dismiss-changes` | POST | Сбросить изменения для раздела (JSON) |
| `/project/<pid>/rename` | POST | Переименовать проект |

### Особенности

- **Два формата конфига**: `app.py` использует `projects[].sections[].{url, range}` с парсингом URL, а `watcher.py` CLI — `spreadsheets[].{id, ranges, sheet}`. Они не взаимозаменяемы.
- `app.py` извлекает `HYPERLINK` формулы из ячеек и рендерит их как кликабельные ссылки.
- Поддержка нескольких диапазонов через запятую в поле `range`: `"A4:C24, G4:Y24"`.
- ID проектов/разделов генерируются как `uuid.uuid4().hex[:8]`.
- Python 3.12+ (используется синтаксис `type | None`).
- `credentials.json` — ключ сервисного аккаунта Google, НЕ коммитить.
- **Безопасная запись `data.json`**: `save_data()` в `app.py` делает атомарную запись через tmp-файл + `Path.replace()`, плюс автобэкап в `data.backup.json` и ежедневный бэкап в `backups/`.
- Между запросами к разным разделам Google API — пауза `time.sleep(3)`, между проектами — `time.sleep(5)` (защита от rate-limit).
- На сервере приложение работает как systemd-сервис `sheets-watcher` (порт 5000).
