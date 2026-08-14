# ArchitectOS — руководство пользователя (RU)

Как установить и пользоваться тремя поставками: **Full** (полное приложение / сервер),
**MCP** (память для Cursor / Claude / Copilot) и **IDE plugin** (панель памяти в IntelliJ).

---

## 1. Что это такое

ArchitectOS — локальный AI-workspace для инженеров: проектная память, поиск,
контекст для агентов, провайдеры моделей, чат и интеграции. Данные лежат у вас
на диске (`data/architectos.db`), сервер слушает только `127.0.0.1`.

| Поставка | Назначение | Нужен HTTP-сервер? |
| --- | --- | --- |
| **Full** | Веб-UI + HTTP API; можно поставить автозапуск при входе в ОС | Это и есть сервер |
| **MCP** | Инструменты памяти в Cursor / Claude / Copilot по stdio | Нет (открывает БД напрямую) |
| **IDE plugin** | Панель Search / Sources в IntelliJ | Да (Detect читает runtime.json) |

Общий корень данных (`ARCHITECTOS_ROOT`):

| Контекст | Путь по умолчанию |
| --- | --- |
| macOS / Linux (пакет / бинарники) | `~/ArchitectOS` |
| Windows | `%LOCALAPPDATA%\ArchitectOS` |
| Запуск из исходников репозитория | корень репозитория |
| Переопределение | переменная окружения `ARCHITECTOS_ROOT` |

Внутри корня: `data/architectos.db`, при работающем сервере —
`data/architectos.runtime.json` (URL, порт, `auth_token`).

---

## 2. Full — полное приложение / сервер

### 2.1. Пакет для передачи коллегам (рекомендуется)

Сборка у вас:

```bash
# только сервер + автозапуск
python3 scripts/build_share_package.py

# сервер + MCP + IDE zip (если уже собраны в dist/)
python3 scripts/build_share_package.py --with-mcp --with-ide
```

Артефакт: `dist/share/ArchitectOS_Full_<версия>_<macos|windows>.zip`.

Получатель:

1. Распаковывает архив.
2. Запускает установку:
   - **macOS:** `chmod +x install.sh architectos-server && ./install.sh`  
     (первый раз: правый клик → Открыть, если Gatekeeper ругается).
   - **Windows:** `install.bat` или `powershell -File .\install.ps1`.
3. Открывает UI: [http://127.0.0.1:8765/](http://127.0.0.1:8765/).

Установка копирует `architectos-server` в `$ARCHITECTOS_ROOT/bin` и регистрирует
**автозапуск при входе в систему**:

| ОС | Механизм |
| --- | --- |
| macOS | LaunchAgent `com.architectos.server` (`~/Library/LaunchAgents/…`) |
| Windows | Scheduled Task «ArchitectOS Server» + ключ `HKCU\…\Run\ArchitectOSServer` |

Сервер стартует с `--no-browser` (фоновый HTTP). Логи: `$ARCHITECTOS_ROOT/logs/`.

Отмена автозапуска: `./uninstall.sh` / `uninstall.bat` (данные не удаляются).

### 2.2. Нативный установщик Desktop (Tauri)

Если собран DMG / MSI:

```bash
make dist-desktop
# → dist/desktop/ArchitectOS_<ver>_macos.dmg
# → dist/desktop/ArchitectOS_<ver>_windows.msi
```

Приложение открывает окно и само поднимает sidecar-сервер; при выходе sidecar
останавливается. Для «сервер всегда работает после логина» удобнее share-пакет
из §2.1.

### 2.3. Запуск из исходников

```bash
python3 run_architectos.py              # браузер
python3 run_architectos.py --app-window # окно приложения
python3 run_architectos.py --no-browser # только сервер
```

Windows: `start-architectos.ps1` / `start-architectos-app.ps1`.

Предпочтительный порт `8765`; если занят — выбирается следующий свободный
(см. `docs/STARTUP.md`).

### 2.4. Первые шаги в UI

1. Выберите язык (EN / RU / UK / HE) в настройках интерфейса.
2. **Projects** — укажите корень проекта (скан / импорт файлов в память).
3. **Memory** — поиск, избранное, review queue, inbox-папка (`data/inbox`).
4. **Chat / Context** — ответы с опорой на память и выбранные файлы.
5. **Providers** — каталог моделей (Ollama, OpenAI, Anthropic, OpenRouter, CLI).
   Секреты — в окружении / настройках, не в тексте памяти.
6. **Settings** — безопасность, embeddings, ingest, авторескан.

Готовность: пока жив процесс сервера, есть файл
`data/architectos.runtime.json` — его же читает IDE-плагин кнопкой **Detect**.

---

## 3. MCP — память для агентов (Cursor, Claude, Copilot)

MCP-сервер **не** требует запущенного Full UI: он открывает `architectos.db`
напрямую по stdio (JSON-RPC). Чтобы память совпадала с приложением, задайте
тот же `ARCHITECTOS_ROOT`.

### 3.1. Готовый бинарник

```bash
make dist-mcp
# → dist/mcp/architectos-mcp_<ver>_<platform>.zip
```

Или возьмите `architectos-mcp` из Full share-пакета, собранного с `--with-mcp`.

Проверка:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ./architectos-mcp
```

### 3.2. Из исходников

```bash
python3 mcp_memory_server.py
# или: npm run mcp
# или после pip install -e .: architectos-mcp
```

### 3.3. Регистрация в Cursor

`~/.cursor/mcp.json` или `.cursor/mcp.json` (абсолютные пути):

```json
{
  "mcpServers": {
    "architectos-memory": {
      "command": "/ABSOLUTE/PATH/TO/architectos-mcp",
      "env": {
        "ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS"
      }
    }
  }
}
```

Из репозитория:

```json
{
  "mcpServers": {
    "architectos-memory": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/ArchitectOS/mcp_memory_server.py"],
      "env": {
        "ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS"
      }
    }
  }
}
```

Перезагрузите MCP в Cursor. В чате агент сможет вызывать `memory_search`,
`memory_add`, `memory_get`, `memory_feedback` и др.

### 3.4. Claude Desktop / Claude Code / Copilot

- **Claude Desktop:**  
  macOS `~/Library/Application Support/Claude/claude_desktop_config.json`  
  Windows `%APPDATA%\Claude\claude_desktop_config.json` — тот же блок `mcpServers`.
- **Claude Code:** `.mcp.json` или `~/.claude.json`.
- **GitHub Copilot (VS Code, Agent):** `.vscode/mcp.json` со схемой `servers`.

Подробные сниппеты: `docs/MCP_MEMORY_SERVER.md` и `README-mcp.txt` в zip MCP.

### 3.5. Основные инструменты

| Инструмент | Зачем |
| --- | --- |
| `memory_search` | Поиск по памяти |
| `memory_context` | Готовый бриф (память + задачи + провайдеры) |
| `memory_add` | Записать урок/решение (секреты редактируются) |
| `memory_get` | Полный узел по id |
| `memory_feedback` | Оценка хитов (±1) для ранжирования |
| `memory_list_projects` | Список проектов для scope |

Типичный цикл агента: search → get → ответ → feedback → add новых уроков.

---

## 4. IDE plugin — IntelliJ

Тонкая панель памяти внутри IntelliJ IDEA (и совместимых IDE). Не заменяет
Copilot/Junie: только Search / Add / Feedback / Sources.

### 4.1. Установка

Сборка:

```bash
make dist-ide
# → dist/ide/ArchitectOS-Memory-<ver>.zip
```

В IDE: **Settings → Plugins → ⚙ → Install Plugin from Disk** → выбрать zip
(из `dist/ide/` или папки `ide/` внутри Full share-пакета).

### 4.2. Подключение к серверу

1. Запустите Full / share-сервер (§2), чтобы появился
   `data/architectos.runtime.json`.
2. **Settings → Tools → ArchitectOS Memory → Detect** — подставит URL и
   `auth_token`.
3. Либо укажите URL вручную (`http://127.0.0.1:8765`) и токен из runtime.json.

Плагин ходит по HTTP с заголовком `X-ArchitectOS-Token`.

### 4.3. Как пользоваться

- **Tool Window «ArchitectOS Memory» → Search** — запрос, результаты, полный
  текст, Useful / Not useful, счётчик review.
- **Sources** — статус источников ingest, путь к inbox, Rescan.
- В редакторе (ПКМ): **Add Selection to ArchitectOS Memory**,
  **Search ArchitectOS Memory for Selection**.

Inbox по умолчанию: `$ARCHITECTOS_ROOT/data/inbox` — положили файл → подхватится
при rescan/ingest.

Подробности: `ide/intellij/README.md`.

---

## 5. Совместная работа Full + MCP + IDE

Одна машина, одна БД:

1. Поставьте Full share (`install.sh` / `install.bat`) — сервер на логине.
2. MCP-клиенты укажите на тот же `ARCHITECTOS_ROOT`.
3. В IntelliJ — Detect к тому же серверу.

Команда / общий диск: синхронизируйте каталог с `data/architectos.db` или
задайте общий `ARCHITECTOS_ROOT`. SQLite сериализует записи — для локального
single-user сценария UI, MCP и плагин могут работать параллельно.

---

## 6. Частые вопросы

**Порт занят / UI не открывается**  
Посмотрите `data/architectos.runtime.json` — там фактический `url` и `port`.
Логи: `$ARCHITECTOS_ROOT/logs/`.

**macOS: «повреждено» / не открывается**  
Сборка без нотаризации: правый клик → Открыть, или  
`xattr -dr com.apple.quarantine ./ArchitectOS_Full_*`.

**Windows: SmartScreen**  
«Подробнее → Выполнить в любом случае» для `install.bat` / `.exe`.

**Нужен ли Python получателю?**  
Нет, если передан PyInstaller-бинарник (`architectos-server` / `architectos-mcp`).
Python нужен только при запуске из исходников.

**Как полностью удалить**  
`uninstall` → затем удалите каталог `~/ArchitectOS` или
`%LOCALAPPDATA%\ArchitectOS` (там БД и логи).

**Где английская техническая документация**  
`docs/INSTALLERS.md`, `docs/STARTUP.md`, `docs/CONFIGURATION.md`,
`docs/MCP_MEMORY_SERVER.md`, `docs/PRODUCTION.md`.

---

## 7. Сводка команд сборки

```bash
make dist-desktop          # Tauri DMG/MSI
make dist-mcp              # zip MCP
make dist-ide              # zip IntelliJ
make dist-share            # Full zip + автозапуск ОС
make dist-share-all        # Full + вложить MCP/IDE если уже собраны
make dist-all              # desktop + mcp + ide (без share)
```

Windows: соответствующие `.ps1` в `scripts/` или
`python scripts/build_all_installers.py` /
`python scripts/build_share_package.py`.
