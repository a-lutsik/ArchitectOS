# ArchitectOS — документация проекта (RU)

Полное описание проекта: зачем он нужен, из чего состоит, как всё установить,
настроить и как пользоваться приложением, MCP-сервером памяти и плагином для IDE.

Версия проекта: **1.0.0**. Требуется **Python 3.12+**.

> Быстрый старт по поставкам (Full / MCP / IDE) — [`GUIDE_RU.md`](GUIDE_RU.md).
> Этот документ шире: он покрывает и архитектуру, и повседневную работу.

---

## Содержание

1. [Что такое ArchitectOS и зачем он нужен](#1-что-такое-architectos-и-зачем-он-нужен)
2. [Из чего состоит проект](#2-из-чего-состоит-проект)
3. [Установка](#3-установка)
4. [Настройка](#4-настройка)
5. [Как пользоваться приложением](#5-как-пользоваться-приложением)
6. [MCP: память для внешних агентов и подключение чужих серверов](#6-mcp) ([хуки агентов](#65-хуки-агентов))
7. [Плагин для IntelliJ IDEA](#7-плагин-для-intellij-idea)
8. [Совместная работа приложения, MCP и плагина](#8-совместная-работа-приложения-mcp-и-плагина)
9. [Разработка, тесты и сборка](#9-разработка-тесты-и-сборка)
10. [Диагностика и частые вопросы](#10-диагностика-и-частые-вопросы)

---

## 1. Что такое ArchitectOS и зачем он нужен

ArchitectOS — **локальный AI-workspace для инженеров**. Он решает проблему
одноразового контекста: любой чат с моделью забывает всё после закрытия вкладки,
а знания о проекте (решения, договорённости, разборы инцидентов, устройство
модулей) приходится пересказывать заново каждой новой сессии и каждому новому
инструменту.

ArchitectOS превращает знания о проекте в **постоянную память**, которая:

- живёт локально на вашем диске (SQLite, `data/architectos.db`);
- переживает перезапуски, смену моделей и смену инструментов;
- доступна одновременно из веб-UI, из Cursor/Claude/Copilot (через MCP) и из IntelliJ (через плагин);
- ищется гибридно: лексика + эмбеддинги + расширение по графу связей;
- стареет «по-человечески»: свежее → стабильное → устаревшее → архив, с учётом частоты обращений.

Ключевые свойства:

| Свойство | Что это даёт |
| --- | --- |
| **Local-first** | Сервер слушает только `127.0.0.1`, данные не уходят наружу без вашего действия |
| **Без обязательных зависимостей** | Runtime — только стандартная библиотека Python; ни Flask, ни Django, ни npm-сборки фронтенда |
| **Нет секретов в БД** | Хранятся только *имена* переменных окружения; текст редактируется от токенов и ключей перед записью |
| **Мультипровайдерность** | OpenAI, Anthropic, OpenRouter, Azure OpenAI, Ollama, Codex CLI, Claude Code, Gemini CLI + авто-роутинг |
| **Три поставки** | Полное приложение, MCP-бинарник, плагин для IDE — с общей базой памяти |

### Что умеет

- **Память со скоупами**: `interface`, `project`, `shared`, `global`; узкие скоупы приоритетнее при поиске.
- **Context Builder** — собирает готовый пакет контекста (память + файлы проекта + активные провайдеры) для передачи модели.
- **Ask** — чат с моделью поверх памяти проекта (в Workspace и в отдельном разделе).
- **Доска задач** (todo / doing / blocked / done).
- **Граф знаний** в двух проекциях: **Map** (2D-карта кластеров) и **Galaxy** (3D), с авто-линковкой и LLM-подсказками по связям.
- **Multi-Agent Council** — один запрос прогоняется ролями code / architecture / docs / review, результат синтезируется.
- **MCP Hub** — ArchitectOS как *клиент* внешних MCP-серверов (Filesystem, GitHub, Azure DevOps, Jira, Slack, Confluence, Granola).
- **MCP Memory Server** — ArchitectOS как *сервер* памяти для Cursor, Copilot, Claude, Junie.
- **Хуки агентов** — захват фактов из Cursor / Claude Code / Codex, даже если агент не вызвал MCP-инструмент.
- **Code Intelligence** через LSP (Python, TypeScript, Go, Rust, Java): символы, hover, диагностика, ссылки.
- **Импорт проекта**: скан локальной папки, git-история, ADR, встречи; изоляция памяти по проектам.
- **Аналитика** по памяти, задачам, чатам, провайдерам.
- **Бэкап/восстановление** профиля через bundle-JSON.
- **Языки интерфейса**: EN, RU, UK, HE (с RTL).

---

## 2. Из чего состоит проект

### 2.1. Структура репозитория

```text
ArchitectOS/
├── backend/                 # Python-пакет architectos: HTTP-сервер, сервисы, хранилище
│   ├── app.py               # dev-сервер на фиксированном порту, без браузера
│   └── architectos/         # основной пакет
├── frontend/                # SPA на чистых ES-модулях, без npm-зависимостей
├── desktop/                 # нативная оболочка Tauri 2 (+ splash-экран)
├── ide/intellij/            # плагин IntelliJ на Kotlin (Gradle)
├── packaging/               # точка входа для PyInstaller-сайдкара
├── scripts/                 # сборка релизов, установщиков, автозапуска, бенчмарки
├── tests/                   # unittest-набор (32 модуля)
├── docs/                    # документация
├── data/                    # БД, runtime-состояние, inbox, загрузки (создаётся при работе)
├── memory/evidence/         # markdown-«доказательства» для узлов памяти
├── run_architectos.py       # основной лаунчер
├── mcp_memory_server.py     # точка входа MCP-сервера памяти (stdio)
├── architectos-server.spec  # PyInstaller: HTTP-сервер (сайдкар для desktop)
├── architectos-mcp.spec     # PyInstaller: MCP-бинарник
├── pyproject.toml           # метаданные пакета, extras, ruff/mypy
├── package.json             # npm-скрипты (только оркестрация, зависимостей нет)
└── Makefile                 # ярлыки сборки установщиков
```

### 2.2. Компоненты и как они связаны

```text
┌──────────────┐   HTTP + X-ArchitectOS-Token   ┌─────────────────────┐
│  Веб-UI      │ ─────────────────────────────► │                     │
│ (frontend/)  │                                │  HTTP-сервер        │
└──────────────┘                                │  (backend/, :8766)  │
┌──────────────┐   HTTP + X-ArchitectOS-Token   │                     │
│ IntelliJ     │ ─────────────────────────────► │  ArchitectOSService │
│ plugin       │                                │        │            │
└──────────────┘                                └────────┼────────────┘
┌──────────────┐                                         │
│ Tauri        │  запускает сайдкар architectos-server ──►│
│ desktop      │                                         ▼
└──────────────┘                                ┌─────────────────────┐
┌──────────────┐   MCP stdio (JSON-RPC)         │ SQLite              │
│ Cursor /     │ ──► architectos-mcp ──────────►│ data/architectos.db │
│ Claude /     │     (работает без сервера)     └─────────────────────┘
│ Copilot      │                                         ▲
└──────────────┘                                         │
┌──────────────┐   POST /api/memory/turn                 │
│ Хуки агентов │ ── или прямой доступ к БД ──────────────┘
│ Cursor /     │
│ Claude/Codex │
└──────────────┘
                     MCP Hub (клиент) ───► внешние MCP-серверы (npx, HTTP/SSE)
```

**Важно:** MCP-сервер памяти открывает базу **напрямую** и не требует запущенного
приложения. Плагин IntelliJ, наоборот, ходит по HTTP и требует живого сервера.

### 2.3. Бэкенд

| Параметр | Значение |
| --- | --- |
| Язык | Python 3.12+ |
| HTTP | Стандартный `http.server.ThreadingHTTPServer` + собственная таблица маршрутов |
| Хранилище | SQLite (`SQLiteMemoryRepository`), схема версии 3 |
| Адрес по умолчанию | `127.0.0.1:8766` |
| Авторизация API | Заголовок `X-ArchitectOS-Token` (токен генерируется при каждом запуске) |
| Кол-во маршрутов | ~106 (`GET` / `POST` / `PATCH` под `/api/...`) |

Основные модули `backend/architectos/`:

| Модуль | Назначение |
| --- | --- |
| `server.py` | HTTP-обработчик, таблица `ROUTES`, отдача статики фронтенда |
| `launcher.py` | Выбор порта, запуск браузера/окна, runtime-файл |
| `service.py` | Фасад `ArchitectOSService`, собранный из 13 сервис-миксинов |
| `storage*.py` | Репозиторий SQLite: миграции, поиск, кандидаты, сессии |
| `search.py`, `embeddings.py` | Гибридный поиск и векторные представления |
| `adapters*.py`, `routing.py` | Адаптеры провайдеров (HTTP и CLI) и политика авто-роутинга |
| `mcp.py`, `mcp_client.py` | MCP Hub — клиент внешних серверов (stdio и HTTP/SSE) |
| `mcp_server.py` | Входящий MCP-сервер памяти |
| `agent_hooks.py` | Хуки Cursor / Claude Code / Codex: захват хода без вызова инструмента |
| `candidate_identity.py` | Стабильный origin факта: один факт — одна карточка в очереди |
| `chat_session_service.py` | Финализация Ask-сессий и захват MCP/hook-ходов (`capture_memory_turn`) |
| `lsp.py` | Code Intelligence через языковые серверы |
| `code_graph.py`, `graph_autolinker.py` | Граф кода и авто-связи в графе знаний |
| `security.py` | Редакция секретов до сохранения и до отправки в модель |
| `paths.py`, `config.py` | Разрешение `ARCHITECTOS_ROOT`, константы |

### 2.4. Фронтенд

SPA на ванильном JavaScript (ES-модули, **без сборки и без npm-зависимостей**).
Отдаётся тем же Python-сервером; при выдаче `index.html` в него подставляется
мета-тег с токеном авторизации. Граф знаний: `graph.js` (Map) и `graph-galaxy.js`
(Galaxy). Ask: `ask-ui.js` / `chat.js`.

### 2.5. Desktop

Tauri 2 (Rust) + PyInstaller-сайдкар. Оболочка запускает бинарник
`architectos-server`, ждёт появления `data/architectos.runtime.json`, затем
переводит webview на `http://127.0.0.1:<порт>/`. При выходе сайдкар убивается.

### 2.6. Плагин IntelliJ

Kotlin + Gradle, ID `com.architectos.memory`, версия `0.2.0`, платформа IC 2024.3,
JDK 17. Тонкий HTTP-клиент: tool window с вкладками Search и Sources,
две editor-команды и страница настроек.

---

## 3. Установка

### 3.1. Требования

| Сценарий | Что нужно |
| --- | --- |
| Запуск из исходников | Python 3.12+ (больше ничего) |
| Готовый бинарник (share-пакет, MCP) | Ничего — Python вшит в бинарник |
| Сборка desktop (Tauri) | Rust, Node.js 20+, Python 3.12+, PyInstaller ≥ 6, Xcode CLT (macOS) или MSVC Build Tools + WebView2 (Windows) |
| Сборка MCP-бинарника | Python 3.12+, PyInstaller ≥ 6 |
| Сборка плагина IDE | JDK 17+ |

Поддерживаемые платформы первой волны: **macOS и Windows**. Linux работает из
исходников; нативных пакетов для него в v1 нет.

### 3.2. Запуск из исходников (самый простой путь)

```bash
git clone <репозиторий> ArchitectOS
cd ArchitectOS

python3 run_architectos.py                # откроется браузер на http://127.0.0.1:8766/
python3 run_architectos.py --app-window   # отдельное окно приложения (Edge/Chrome --app)
python3 run_architectos.py --no-browser   # только сервер
```

Windows:

```powershell
.\start-architectos.ps1        # или start-architectos.bat
.\start-architectos-app.ps1    # режим окна приложения
```

Флаги лаунчера:

| Флаг | По умолчанию | Смысл |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Адрес привязки |
| `--port` | `8766` | Предпочтительный порт |
| `--port-attempts` | `20` | Сколько портов перебрать, если занято |
| `--strict-port` | выкл. | Падать с ошибкой, если порт занят, вместо перебора |
| `--no-browser` | выкл. | Не открывать браузер |
| `--app-window` | выкл. | Открыть окно приложения вместо вкладки |

Опциональная установка пакета (даёт консольную команду `architectos-mcp`):

```bash
pip install -e .

# опциональные ускорения
pip install -e ".[vectors]"      # numpy + sqlite-vec — KNN в SQLite, numpy как fallback
# python.org для macOS не умеет load_extension: Settings → Эмбеддинги, или install.sh
# сразу проверяет бинарник. Frozen: sqlite-vec запекается при сборке на Homebrew Python.
pip install -e ".[embeddings]"   # fastembed — настоящие локальные эмбеддинги вместо hash-фолбэка
pip install -e ".[tls]"          # certifi — CA-бандл для HTTPS
pip install -e ".[packaging]"    # pyinstaller — сборка бинарников
pip install -e ".[dev]"          # ruff — линтер
```

### 3.3. Desktop-приложение (Tauri, DMG/MSI)

Сборка:

```bash
make dist-desktop          # или ./scripts/build_desktop.sh
# Windows: .\scripts\build_desktop.ps1
```

Результат: `dist/desktop/ArchitectOS_<версия>_macos.dmg` или `..._windows.msi`.
Установка обычная — перенести в Applications / запустить MSI. Python получателю
не нужен.

Разработка без полной сборки:

```bash
cd desktop && npm install && npm run tauri dev
```

Если бинарника сайдкара нет, Rust-оболочка откатится на
`python3 run_architectos.py --no-browser`.

### 3.4. Share-пакет: портативный сервер + автозапуск при входе в ОС

Это рекомендуемый способ передать ArchitectOS коллеге.

Сборка у вас:

```bash
python3 scripts/build_share_package.py                    # только сервер
python3 scripts/build_share_package.py --with-mcp --with-ide   # + MCP и плагин
# или: make dist-share / make dist-share-all
```

Артефакт: `dist/share/ArchitectOS_Full_<версия>_<macos|windows>.zip`.

Действия получателя:

1. Распаковать архив.
2. Запустить установку:
   - **macOS:** `chmod +x install.sh architectos-server && ./install.sh`
     (при ругани Gatekeeper — правый клик → «Открыть»).
   - **Windows:** `install.bat` или `powershell -File .\install.ps1`.
3. Открыть <http://127.0.0.1:8766/>.

Установщик копирует `architectos-server` в `$ARCHITECTOS_ROOT/bin`, кладёт рядом
`uninstall.sh` и `docs/`, и регистрирует автозапуск. MCP и IDE zip из пакета
тоже копируются, если они были в архиве.

| ОС | Механизм автозапуска |
| --- | --- |
| macOS | LaunchAgent `com.architectos.server` (`RunAtLoad` + `KeepAlive`) |
| Windows | Scheduled Task «ArchitectOS Server» + запасной ключ `HKCU\...\Run` |
| Linux (бонус) | systemd user unit, если доступен `systemctl --user` |

Отключение: `./uninstall.sh` / `uninstall.bat` — данные при этом сохраняются.

### 3.5. MCP-бинарник

```bash
make dist-mcp     # или ./scripts/build_mcp.sh, .\scripts\build_mcp.ps1
```

Результат в `dist/mcp/`: `architectos-mcp` (или `.exe`), `README-mcp.txt` с
готовыми сниппетами конфигов и zip-архив. Регистрация в клиентах — раздел [6](#6-mcp).

### 3.6. Плагин IntelliJ

```bash
make dist-ide     # или ./scripts/build_ide_plugin.sh
# вручную: cd ide/intellij && ./gradlew buildPlugin
```

Результат: `dist/ide/ArchitectOS-Memory-<версия>.zip`.

Установка: **Settings → Plugins → ⚙ → Install Plugin from Disk** → выбрать zip.

### 3.7. Портативный zip с исходниками

```bash
python scripts/build_release.py               # → dist/architectos-<версия>.zip
python scripts/build_release.py --check-only  # только проверка манифеста
```

Из архива исключаются `data/`, `memory/`, кэши, `.env*` и бэкапы.

---

## 4. Настройка

### 4.1. Корень данных `ARCHITECTOS_ROOT`

Это главная настройка: она определяет, **какую базу памяти** видит каждый компонент.

| Контекст | Путь по умолчанию |
| --- | --- |
| Запуск из исходников | Корень репозитория |
| Упакованный бинарник, macOS/Linux | `~/ArchitectOS` |
| Упакованный бинарник, Windows | `%LOCALAPPDATA%\ArchitectOS` |
| Переопределение | Переменная окружения `ARCHITECTOS_ROOT` (абсолютный путь) |

Что лежит внутри корня:

| Путь | Содержимое |
| --- | --- |
| `data/architectos.db` | Основная база: проекты, память, задачи, чаты, провайдеры, аудит |
| `data/architectos.runtime.json` | Существует только пока жив сервер: `url`, `port`, `pid`, `auth_token` |
| `data/inbox/` | Drop-папка: положенные файлы попадают в память при ingest/rescan |
| `data/uploads/` | Загруженные через UI файлы |
| `memory/evidence/` | Markdown-обоснования для долговременных узлов |
| `backups/` | Локальные копии БД |
| `logs/` | Логи фоновой службы (share-пакет) |

### 4.2. Переменные окружения ядра

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `ARCHITECTOS_ROOT` | см. выше | Каталог данных |
| `ARCHITECTOS_HOST` | `127.0.0.1` | Адрес привязки |
| `ARCHITECTOS_PORT` | `8766` | Предпочтительный порт |
| `ARCHITECTOS_FRONTEND` | `frontend/` | Переопределение каталога статики |
| `ARCHITECTOS_ENV` | `local` | Метка окружения в `/api/version` |
| `ARCHITECTOS_BACKUP_RETENTION` | `10` | Сколько бэкапов хранить |
| `ARCHITECTOS_ACCESS_LOG` | выкл. | `1`/`true` — писать лог HTTP-запросов |
| `ARCHITECTOS_ALLOW_LOCAL_URLS` | выкл. | Разрешить loopback-URL провайдеров (обход SSRF-защиты) |
| `ARCHITECTOS_MCP_ALLOW_LOCAL` | выкл. | Разрешить loopback-URL удалённых MCP-серверов |
| `ARCHITECTOS_SERVER_BIN` | — | Путь к готовому бинарнику сайдкара |

### 4.3. Провайдеры моделей и ключи

Ключи задаются в UI после установки: **Setup → Providers** (модели) и
**Settings → Connections** (Azure DevOps). Приложение пишет их в
`.env.local` и **не** кладёт секреты в SQLite. Окружение процесса или `.env`
в корне данных по-прежнему работает как override.

```bash
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export OPENROUTER_API_KEY="..."
export OLLAMA_BASE_URL="http://127.0.0.1:11434"
python3 run_architectos.py
```

PowerShell:

```powershell
$env:OPENAI_API_KEY = "..."
$env:ANTHROPIC_API_KEY = "..."
python .\run_architectos.py
```

| Провайдер | Переменные |
| --- | --- |
| OpenAI | `OPENAI_API_KEY`, `OPENAI_BASE_URL` |
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| Azure OpenAI | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` |
| Ollama | `OLLAMA_BASE_URL` или `OLLAMA_HOST` (по умолчанию `http://127.0.0.1:11434`) |
| Gemini CLI | `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `ANTIGRAVITY_TOKEN` |

CLI-провайдеры (Codex CLI, Claude Code) по умолчанию требуют явного разрешения
на запуск. Без разрешения авто-роутер их пропускает и отвечает через API-провайдера,
а не подвешивает ход. Разрешить можно кнопкой «Enable CLI runs» в ответе или
параметром `allow_cli: true` в API.

### 4.4. Эмбеддинги

Без настройки используется встроенный hash-фолбэк — он работает, но качество
поиска ниже. Варианты улучшения:

```bash
pip install -e ".[embeddings]"    # локальная ONNX-модель, без ключей
```

| Переменная | Назначение |
| --- | --- |
| `MEMORY_EMBEDDING_PROVIDER` | Принудительно выбрать провайдера (`hash`, `openai`, `ollama`, …) |
| `MEMORY_EMBEDDING_MODEL`, `MEMORY_EMBEDDING_DIMENSIONS` | Модель и размерность |
| `MEMORY_LOCAL_EMBED_MODEL` | Локальная модель |
| `OLLAMA_EMBED_MODEL` | Модель эмбеддингов в Ollama |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | Деплой Azure для эмбеддингов |
| `GEMINI_EMBED_MODEL`, `GEMINI_API_BASE` | Gemini |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_EMBED_MODEL` | Cloudflare Workers AI |

После смены провайдера пересоберите индекс: **Memory → Rebuild embeddings**
(или `POST /api/memory/embeddings/rebuild`).

### 4.5. Настройки в интерфейсе

Раздел **Settings** правит ключи, которые хранятся в таблице `settings` внутри БД:

- `memory_retrieval` — веса гибридного поиска, лимиты, расширение по графу.
- `memory_lifecycle` — правила старения, порог `chat_session_idle_minutes`
  (по умолчанию `30`; `0` отключает авто-финализацию чат-сессии).
- `memory_ingest.inbox_dir` — путь к drop-папке.
- `mcp_servers` — реестр внешних MCP-серверов.
- `code_graph` — параметры индексации кода.
- Стратегия и веса авто-роутера, конфигурация Council.
- Язык интерфейса (EN / RU / UK / HE).

### 4.6. Безопасность

- Сервер слушает только loopback; проверяются заголовки `Host` и `Origin`
  (защита от DNS-rebinding).
- Каждый вызов `/api/*` требует заголовок `X-ArchitectOS-Token`. Токен новый при
  каждом запуске, подставляется в `index.html` и пишется в `runtime.json`.
  Единственное исключение — `GET /api/mcp/oauth/callback`.
- Единая политика редакции секретов применяется **и** перед отправкой в модель,
  **и** перед записью в память: API-ключи, bearer-токены, приватные ключи,
  пароли, basic-auth URL.
- Проверить, что именно будет вырезано, можно в **Settings → Security Preview**
  или запросом:

```bash
curl -X POST http://127.0.0.1:8766/api/security/preview \
  -H "Content-Type: application/json" \
  -H "X-ArchitectOS-Token: <токен из runtime.json>" \
  -d '{"text":"token=abc123456789xyz"}'
```

- Исходящие URL проходят SSRF-фильтр (`netutil.py`).

---

## 5. Как пользоваться приложением

Интерфейс открывается на <http://127.0.0.1:8766/>. Левое меню разбито на группы.

### 5.1. Первые шаги

1. Выберите язык интерфейса в **Settings**.
2. **Projects** → создайте проект: укажите папку на диске, имя, при желании —
   стиль кода, ignore-паттерны и скоуп памяти. Включите индексацию файлов.
3. Дождитесь скана. Файлы попадут в дерево проекта, а извлечённые факты — в
   очередь кандидатов памяти.
4. **Memory** → просмотрите review queue (чипы **Ask** и **MCP**) и подтвердите то, что стоит помнить.
5. **Providers** → вставьте API-ключ в карточку провайдера и нажмите Test.
   ADO — в **Settings → Connections**. `.env` больше не обязателен.
6. **Ask** / **Workspace** → задавайте вопросы; ответы опираются на память и файлы.
7. **Setup → Agent hooks** → поставьте хуки, если пользуетесь Cursor / Claude Code / Codex.

### 5.2. Разделы интерфейса

| Раздел | Что делает |
| --- | --- |
| **Workspace** | Основная рабочая поверхность: редактор файлов, Ask и режим Split. Дерево файлов, поиск, git-diff |
| **Memory** | Поиск по памяти, узлы, избранное, граф (Map / Galaxy), lifecycle (Run Decay), ingest-источники, inbox, rescan, review queue со счётчиком |
| **Tasks** | Доска задач: todo / doing / blocked / done |
| **Ask** | Чат с моделью поверх памяти проекта; Council-режим, диалоги, кнопка End — сводка сессии в очередь |
| **Providers** | Каталог провайдеров и моделей, тесты подключения, настройки авто-роутера и предпросмотр маршрутизации |
| **MCP** | Реестр *внешних* MCP-серверов (Hub): включение, аргументы, переменные, тест, список инструментов, OAuth. Это не очередь кандидатов «MCP» |
| **Agent hooks** | Установка хуков Cursor / Claude Code / Codex, чтобы факты из IDE попадали в память без вызова инструмента |
| **Code** | Code Intelligence: языковые серверы, установка, символы, hover, диагностика, ссылки |
| **Settings** | Язык, безопасность, эмбеддинги, ingest, авторескан, память Ask, бэкапы, импорт/экспорт bundle |

Терминал открывается кнопкой в шапке (не отдельный пункт меню): одноразовая команда
в папке проекта; «Ask agent» создаёт новый диалог Ask с выводом.

Глобальная палитра поиска — **⌘K** / **Ctrl+K** (подробности в
[`SEARCH_MEMORY_GUIDE.md`](SEARCH_MEMORY_GUIDE.md)).

### 5.3. Как работает память

**Скоупы** (от узкого к широкому): `interface` → `project` → `shared` → `global`.
При поиске узкие скоупы получают приоритет.

**Пути попадания в память:**

- Скан проекта и ingest источников (git, ADR, встречи, inbox-папка).
- Явное «Remember answer» или ★ в Ask.
- `Add Selection to ArchitectOS Memory` из IntelliJ.
- `memory_add` из внешнего агента по MCP.
- Факты из Ask при завершении сессии (чип **Ask**).
- Факты из MCP `memory_turn` и хуков агентов (чип **MCP**).

**Ask и MCP — разные каналы.** В review queue чип показывает *откуда* пришёл
факт, а не текст сообщения:

| Канал | Откуда | Чип | Подпись карточки |
| --- | --- | --- | --- |
| Ask | Раздел Ask / панель Ask в Workspace | **Ask** | `Chat fact:` / сводка сессии |
| MCP | Инструмент `memory_turn` или хук агента | **MCP** | `MCP fact:` |

Один и тот же устойчивый факт не дублируется: если его уже захватил MCP, Ask
не пишет вторую карточку (и наоборот). Сырой диалог не хранится — только
извлечённые факты.

**Ask-сессии.** ArchitectOS не создаёт кандидата после каждой реплики Ask.
Сессия завершается кнопкой **End** на карточке диалога (или иконкой End session
в Ask) либо по простою (`memory_lifecycle.chat_session_idle_minutes`, по
умолчанию 30 минут; `0` отключает). При завершении в очередь идут атомы фактов;
сводка сессии сохраняется в `chat_context_summaries` для контекста следующих
ходов. Карточка `chat_session_summary` в Review Queue создаётся только как
fallback, если извлечь атомы не удалось. Если те же факты уже в очереди как MCP,
Ask finalize пропускает запись. Режим памяти Ask —
**Settings → Chat memory** (`off` / `strict` / `aggressive`). Явные
«Remember answer» и ★ работают в обход этого правила.

**Жизненный цикл.** Свежее → стабильное → устаревшее → архив. Обращение к узлу
обновляет его «свежесть»; неиспользуемое остывает и в итоге архивируется или
удаляется по правилам из настроек. Избранное остаётся долговременным.

**Граф.** Авто-линкер строит связи между узлами (корневые ссылки, типы рёбер для
документов и кода, связи по похожести). Две проекции:

| Проекция | Для чего | Управление |
| --- | --- | --- |
| **Map** | Читать структуру кластеров на плоскости | Перетаскивание узла; пустое место — сдвиг; колёсико — зум |
| **Galaxy** | Смотреть кластеры и связи в 3D | Тянуть — вращение; Shift / правая / средняя кнопка — сдвиг; колёсико — зум |

Кнопки: **Links** (перестроить), **Suggest** (LLM-подсказки связей), **Fit**,
**Refresh**. Двойной клик по узлу открывает карточку, Esc закрывает
полноэкранный режим.

### 5.4. Провайдеры и авто-роутинг

Smart Router выбирает провайдера по взвешенной комбинации стоимости, скорости,
качества, доступности и роли задачи. Предпросмотр решения — кнопка Routing preview
(`POST /api/router/preview`), веса настраиваются в разделе Providers.

**Council** прогоняет один запрос через роли code / architecture / docs / review
и синтезирует общий ответ — полезно для архитектурных решений и ревью.

### 5.5. Бэкапы

- Ручной бэкап БД: **Settings → Backup** (`POST /api/ops/backup`), список —
  `GET /api/ops/backups`, глубина хранения — `ARCHITECTOS_BACKUP_RETENTION`.
- Экспорт/импорт профиля: `GET /api/bundle/export` / `POST /api/bundle/import`
  (файл `architectos.bundle`, секреты редактируются при экспорте).

---

## 6. MCP

ArchitectOS участвует в MCP в **двух ролях** одновременно.

| Роль | Направление | Транспорт | Зачем |
| --- | --- | --- | --- |
| **Memory Server** (входящий) | Внешние агенты → память ArchitectOS | stdio | Cursor / Claude / Copilot / Junie получают вашу проектную память |
| **MCP Hub** (исходящий) | ArchitectOS → внешние серверы | stdio + HTTP/SSE | Внутренний агент получает файлы, GitHub, Azure DevOps, Granola и т.д. |

### 6.1. MCP Memory Server: запуск

Сервер открывает `data/architectos.db` напрямую — **приложение запускать не нужно**.
Чтобы память совпадала с приложением, укажите тот же `ARCHITECTOS_ROOT`.

```bash
# из исходников
python3 mcp_memory_server.py
npm run mcp

# после pip install -e .
architectos-mcp

# готовый бинарник
./architectos-mcp
```

Быстрая проверка протокола:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 mcp_memory_server.py
```

Транспорт — построчный JSON-RPC 2.0 по stdin/stdout, версия протокола `2024-11-05`,
имя сервера `architectos-memory`. Логи идут только в stderr, чтобы не портить поток.

### 6.2. Доступные инструменты

| Инструмент | Назначение | Ключевые аргументы |
| --- | --- | --- |
| `memory_search` | Поиск по памяти с оценками релевантности | `query`, `project_id?`, `scope?`, `limit?`, `mode?`, `filters?` |
| `memory_context` | Готовый бриф: память + открытые задачи + провайдеры | `query`, `project_id?`, `scope?`, `limit?` |
| `memory_turn` | Pack на этот ход + атомы в review (Lesson низкого риска — short-term) | `user_text`, `assistant_text?`, `project_id?` |
| `memory_add` | Записать новый факт/решение (секреты вырезаются; пачка фактов режется) | `label`, `text`, `type?`, `scope?`, `project_id?`, `confidence?` |
| `memory_get` | Полный узел по id (без обрезки, с метаданными и соседями) | `id`, `include_neighbors?` |
| `memory_feedback` | Оценка выдачи (±1) — улучшает ранжирование | `rating`, `hit_ids?`, `query?`, `note?` |
| `memory_list_projects` | Список проектов для скоупинга | — |
| `memory_themes` | Тематические кластеры графа | — |
| `memory_explain_path` | Кратчайший путь между двумя узлами | — |
| `code_neighbors` | Кто вызывает символ и кого вызывает он | — |
| `code_impact` | Радиус влияния изменения символа | — |

Ресурсы: `memory://briefing`, `memory://projects`, `memory://review`, шаблон `memory://nodes/{id}`.

Типичный цикл агента: briefing уже в `instructions` при старте MCP; на каждое сообщение пользователя — `memory_turn`; дальше `memory_search` / `memory_get` при необходимости → ответ → `memory_feedback` → `memory_add` для явной записи.

Факты из `memory_turn` попадают в очередь с источником **MCP**, не Ask. Если
тот же факт уже есть как MCP, Ask-сессия его не дублирует. Хуки агентов идут
по тому же пути захвата (`capture_memory_turn`) и тоже помечаются как MCP.

### 6.3. Регистрация в клиентах

**Cursor** — `~/.cursor/mcp.json` (глобально) или `.cursor/mcp.json` (в проекте).
Пути обязательно абсолютные.

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

Вариант из исходников:

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

После правки перезагрузите MCP в Cursor: в Settings → MCP должен появиться
`architectos-memory` со списком инструментов.

**Claude Desktop** — тот же блок `mcpServers` в файле:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Затем перезапустить приложение.

**Claude Code** — `.mcp.json` в проекте или `~/.claude.json` для пользовательского
скоупа, схема та же.

**GitHub Copilot (VS Code, Agent mode)** — `.vscode/mcp.json`, схема `servers`:

```json
{
  "servers": {
    "architectos-memory": {
      "type": "stdio",
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/ArchitectOS/mcp_memory_server.py"]
    }
  }
}
```

**JetBrains AI Assistant / Junie** — Settings → Tools → AI Assistant →
Model Context Protocol (MCP) → Add, те же `command` и `args`.

Готовые сниппеты также лежат в `README-mcp.txt` внутри zip MCP-поставки.

### 6.4. MCP Hub: подключение внешних серверов

Раздел **MCP** в интерфейсе — реестр внешних серверов, к которым подключается сам
ArchitectOS. Поддерживаются транспорты `stdio` (запуск подпроцесса) и
`http` / `streamable-http` (POST с разбором JSON или SSE).

Предустановленные записи:

| ID | Транспорт | Команда / URL |
| --- | --- | --- |
| `filesystem` | stdio | `npx -y @modelcontextprotocol/server-filesystem .` (корень подставляется из проекта) |
| `github` | stdio | `npx -y @modelcontextprotocol/server-github` |
| `azure-devops` | stdio | `npx -y @azure-devops/mcp $ADO_ORG --authentication envvar` |
| `azure-devops-git` | stdio | то же + `-d core repositories` |
| `jira` | stdio | `npx -y mcp-jira` |
| `slack` | stdio | `npx -y @modelcontextprotocol/server-slack` |
| `confluence` | stdio | `npx -y mcp-confluence` |
| `granola` | http | `https://mcp.granola.ai/mcp` (OAuth) |

Для stdio-серверов нужен установленный **Node.js/npx**.

Переменные окружения интеграций:

| Переменная | Для чего |
| --- | --- |
| `ADO_ORG` / `AZURE_DEVOPS_ORG` | Организация Azure DevOps |
| `ADO_MCP_AUTH_TOKEN` / `PERSONAL_ACCESS_TOKEN` | PAT для Azure DevOps |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | GitHub MCP |
| `MS_GRAPH_TENANT_ID`, `MS_GRAPH_CLIENT_ID`, `MS_GRAPH_CLIENT_SECRET` | Microsoft Teams / Graph |

Внутренний агент видит эти серверы через понятные имена инструментов:
`fs_read`, `fs_list`, `fs_search`, `fs_write`, `boards_my_work`, `boards_search`,
`repo_list_repositories`, `repo_get_pull_request`, `granola_list_meetings` и т.д.

### 6.5. Хуки агентов

MCP срабатывает, только если модель сама вызвала инструмент. Хуки клиента
срабатывают на каждом ходе, поэтому молчащий агент всё равно отдаёт факты в
очередь **MCP**.

Ставятся в приложении: **Setup → Agent hooks** (в русской локали — **Настройка →
Хуки агентов**). Кнопка показывает файлы, которые будут изменены. Флажок
«Только этот проект» пишет конфиг в репозиторий, а не в домашний каталог.

```bash
python3 architectos_hook.py install --client all
python3 architectos_hook.py doctor
```

Cursor хуками только *захватывает* (подставить память в промпт нельзя) — MCP
для него оставляем. Codex и Claude Code умеют и захват, и подстановку.
Подробности: [`AGENT_HOOKS.md`](AGENT_HOOKS.md).

---

## 7. Плагин для IntelliJ IDEA

Тонкая «панель памяти» внутри IntelliJ IDEA и других IDE на платформе IntelliJ.
Он **не** конкурирует с Copilot и Junie: только работа с памятью.

### 7.1. Установка и подключение

1. Соберите (`make dist-ide`) или возьмите zip из папки `ide/` внутри share-пакета.
2. **Settings → Plugins → ⚙ → Install Plugin from Disk** → выбрать zip → перезапустить IDE.
3. Запустите сервер ArchitectOS, чтобы появился `data/architectos.runtime.json`.
4. **Settings → Tools → ArchitectOS Memory → Detect** — URL и `auth_token`
   подставятся автоматически.

Если Detect недоступен, впишите вручную URL (`http://127.0.0.1:8766`) и токен из
`runtime.json`. Плагин ходит по HTTP с заголовком `X-ArchitectOS-Token`; CORS ему
не нужен, потому что нативный клиент не шлёт `Origin`.

> Токен меняется при каждом перезапуске сервера — после рестарта нажмите Detect ещё раз.

### 7.2. Возможности

| Место | Что делает |
| --- | --- |
| Tool Window «ArchitectOS Memory» → **Search** | Поиск по памяти, список результатов, полный текст узла, кнопки «Useful / Not useful», счётчик review queue |
| Tool Window → **Sources** | Статус источников ingest (локальные + MCP), путь к inbox, последние логи, кнопка «Rescan selected» |
| Правый клик в редакторе → **Add Selection to ArchitectOS Memory** | Сохранить выделенный фрагмент как память (`source=intellij`), тип и скоуп из настроек |
| Правый клик → **Search ArchitectOS Memory for Selection** | Открыть панель с запросом из выделенного текста |
| Settings → Tools → ArchitectOS Memory | URL, токен, корень ArchitectOS, кнопка Detect, дефолты для ручного добавления (project id, scope, type) |

Сами подключения источников (Azure, Granola, корни файловой системы) настраиваются
в основном приложении — плагин их только показывает и запускает.

### 7.3. Inbox-папка

На сервере есть источник `inbox`: всё, что вы кладёте в папку (по умолчанию
`<root>/data/inbox`, меняется ключом `memory_ingest.inbox_dir`), подхватывается при
ingest/rescan и попадает в память как кандидаты. Берутся текстовые файлы
(md, txt, log, код, конфиги), бинарные пропускаются. Актуальный путь виден во
вкладке Sources.

---

## 8. Совместная работа приложения, MCP и плагина

Одна машина — одна база:

1. Поставьте Full share-пакет (`install.sh` / `install.bat`) — сервер поднимается
   при входе в систему.
2. В конфигах MCP-клиентов укажите тот же `ARCHITECTOS_ROOT`.
3. В IntelliJ нажмите **Detect** — плагин подключится к тому же серверу.

Что с чем сочетается:

| Компонент | Нужен ли запущенный HTTP-сервер |
| --- | --- |
| Веб-UI / desktop | Это и есть сервер |
| MCP-сервер памяти | Нет — открывает БД напрямую |
| Плагин IntelliJ | Да |

SQLite сериализует записи, поэтому для локального однопользовательского сценария
UI, MCP-клиенты, хуки агентов и плагин могут работать параллельно с одной базой.
В review queue факты из MCP и хуков помечены **MCP**, факты из Ask — **Ask**;
один факт не появляется в обоих каналах.

Командная работа:

- **Общая память** — синхронизируйте каталог с `data/architectos.db` или задайте
  всем общий `ARCHITECTOS_ROOT`.
- **Персональная память** — у каждого свой чекаут, слияние идёт через review queue
  в приложении.

---

## 9. Разработка, тесты и сборка

### 9.1. Запуск для разработки

```bash
python3 backend/app.py        # dev-сервер: фиксированный порт, без браузера
npm run dev                   # то же самое
npm start                     # полный лаунчер с браузером
npm run app                   # режим окна приложения
```

### 9.2. Тесты и проверки

```bash
python -m unittest discover -s tests -v     # или npm test
node --check frontend/app.js                # синтаксис фронтенда
node scripts/frontend_load_smoke.js         # проверка порядка загрузки модулей
python scripts/build_release.py --check-only # манифест релиза
```

CI (`.github/workflows/ci.yml`): ruff + mypy + покрытие ≥ 72 % на Ubuntu, Windows и
macOS; проверка синтаксиса и порядка загрузки фронтенда на Node 20. Тяжёлые сборки
Tauri/PyInstaller в PR-CI не запускаются.

Линтер локально:

```bash
pip install -e ".[dev]"
ruff check .
pre-commit install     # опционально, хуки ruff
```

### 9.3. Команды сборки

```bash
make dist-desktop      # Tauri DMG / MSI
make dist-mcp          # zip с MCP-бинарником
make dist-ide          # zip плагина IntelliJ
make dist-share        # портативный Full + автозапуск в ОС
make dist-share-all    # то же + вложить MCP и IDE, если уже собраны
make dist-all          # desktop + mcp + ide
make dist-list         # список доступных целей
```

Эквиваленты через npm: `npm run dist:desktop`, `dist:mcp`, `dist:ide`,
`dist:share`, `dist:share:all`, `dist:all`.

На Windows используйте `.ps1`-двойники в `scripts/` или напрямую
`python scripts/build_all_installers.py` / `python scripts/build_share_package.py`.

### 9.4. Архитектурные паттерны

| Паттерн | Реализация |
| --- | --- |
| Repository | `SQLiteMemoryRepository` — вся персистентность и сериализация bundle |
| Service / Facade | `ArchitectOSService` из 13 миксинов `*ServiceMixin` |
| Launcher Facade | `architectos.launcher` — жизненный цикл сервера, порты, запуск окна |
| Strategy | `HybridSearchStrategy` (ранжирование) и `RouterPolicy` (выбор провайдера) |
| Orchestrator | `CouncilOrchestrator` — веерный прогон по ролям и синтез |
| Adapter / Client | `MCPManager`/`MCPClient` (MCP stdio), `CodeIntelligenceManager`/`LSPClient` (LSP stdio) |
| Server | `MemoryMCPServer` — отдаёт движок памяти внешним MCP-клиентам |

---

## 10. Диагностика и частые вопросы

**Порт занят или UI не открывается.**
Посмотрите `data/architectos.runtime.json` — там фактические `url` и `port`.
Лаунчер перебирает до 20 портов начиная с 8766. Нужна жёсткая ошибка вместо
перебора — добавьте `--strict-port`. Логи фоновой службы: `$ARCHITECTOS_ROOT/logs/`.

**MCP-клиент не видит инструменты.**
Проверьте, что путь в конфиге абсолютный, и прогоните
`echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | <команда>`. Если вернулся
список — проблема на стороне клиента, перезагрузите его MCP-конфигурацию.

**MCP видит не ту память.**
Почти всегда это разный `ARCHITECTOS_ROOT`. Задайте его явно в блоке `env` конфига
и сравните с тем, что показывает приложение.

**В очереди кандидатов одно сообщение и как MCP, и как Ask.**
Так быть не должно: чип **Ask** только у диалогов Ask, чип **MCP** — у
`memory_turn` и хуков. Если видите дубль, это старые карточки до этой логики;
отклоните лишнюю. Новые факты склеиваются по смыслу факта, канал не переписывается.

**Плагин IntelliJ не подключается.**
Сервер должен быть запущен, а токен — актуальным: он меняется при каждом старте.
Нажмите **Detect** заново.

**macOS: «файл повреждён» / не открывается.**
Сборки без нотаризации: правый клик → «Открыть», либо
`xattr -dr com.apple.quarantine ./ArchitectOS_Full_*`.

**Windows: SmartScreen блокирует установку.**
«Подробнее → Выполнить в любом случае».

**Нужен ли Python получателю пакета.**
Нет, если передан PyInstaller-бинарник (`architectos-server` / `architectos-mcp`).
Python требуется только при запуске из исходников.

**Провайдер пишет, что нет доступа.**
Задайте переменную окружения в той же оболочке, из которой стартует ArchitectOS,
и перезапустите приложение — окружение читается при старте.

**CLI-провайдер вернул `approval_required`.**
Это ожидаемо: запуск CLI требует явного разрешения. Нажмите «Enable CLI runs» в
ответе или передайте `allow_cli: true` через API. Чтение памяти и файлов проекта
разрешения не требует.

**Как удалить полностью.**
Запустите `uninstall.sh` / `uninstall.bat`, затем удалите каталог `~/ArchitectOS`
или `%LOCALAPPDATA%\ArchitectOS` — там лежат база и логи.

---

## Смежные документы

| Документ | О чём |
| --- | --- |
| [`GUIDE_RU.md`](GUIDE_RU.md) | Краткое руководство по трём поставкам (RU) |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Архитектура рантайма, хранилище, паттерны |
| [`STARTUP.md`](STARTUP.md) | Режимы запуска, порты, runtime-состояние |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Провайдеры, пути, безопасность |
| [`INSTALLERS.md`](INSTALLERS.md) | Установщики Full / MCP / IDE и smoke-чеклисты |
| [`MCP_MEMORY_SERVER.md`](MCP_MEMORY_SERVER.md) | Протокол MCP-сервера памяти и сниппеты клиентов |
| [`AGENT_HOOKS.md`](AGENT_HOOKS.md) | Хуки Cursor / Claude Code / Codex: захват хода без вызова инструмента |
| [`SDK.md`](SDK.md) | Python SDK: `from architectos import ArchitectOS` |
| [`SEARCH_MEMORY_GUIDE.md`](SEARCH_MEMORY_GUIDE.md) | Палитра поиска и работа с памятью |
| [`CODE_GRAPH.md`](CODE_GRAPH.md) | Индексация графа кода |
| [`PRODUCTION.md`](PRODUCTION.md) | Эксплуатация, health-чеки, бэкапы |
| [`RELEASE_QA.md`](RELEASE_QA.md) | Чеклист релизного QA |
| [`ide/intellij/README.md`](../ide/intellij/README.md) | Детали плагина IntelliJ |
