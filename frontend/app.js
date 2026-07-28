const state = { projectId: "architectos", projects: [], chatId: "", activeRunId: "", selectedFile: "", language: "en", theme: "system", density: "comfortable", attachments: [], memoryFiles: [], terminalHistory: [], askMode: "quick", onboardingComplete: false, projectFilesStatus: "", lastFailedMessage: "", codeInsightTab: "symbols", embeddingCatalog: [] };
const titleByView = { workspace: "view.workspace", chat: "view.chat", memory: "view.memory", tasks: "view.tasks", providers: "view.providers", mcp: "view.mcp", code: "view.code", terminal: "view.terminal", analytics: "view.analytics", settings: "view.settings" };
const SETUP_VIEWS = new Set(["providers", "mcp", "code", "terminal", "settings"]);
const ASK_MODES = ["quick", "council", "memory", "memory-mcp"];
const ASK_MODE_HINTS = {
  quick: "Agent decides which tools/memory to use and acts on your request.",
  council: "Same question to several models, then a judge merges one grounded answer.",
  memory: "Answer only from local project memory — no external provider or MCP tools.",
  "memory-mcp": "Local memory first; refine via enabled MCP tools (Azure Boards, Filesystem).",
};
const RTL_LANGUAGES = new Set(["he"]);
const LANGUAGE_META = {
  en: { flag: "🇺🇸", label: "English" },
  ru: { flag: "🇷🇺", label: "Русский" },
  uk: { flag: "🇺🇦", label: "Українська" },
  he: { flag: "🇮🇱", label: "עברית" },
};
const translations = {
  en: {
    "view.workspace": "Workspace", "view.chat": "Ask", "view.memory": "Memory", "view.tasks": "Tasks", "view.agents": "Agents", "view.providers": "Providers", "view.mcp": "MCP", "view.code": "Code", "view.terminal": "Run command", "view.analytics": "Analytics", "view.settings": "Settings",
    "terminal.title": "Run command", "terminal.hint": "Run a one-shot command in the project folder. Risky commands are blocked unless destructive commands are explicitly enabled.", "terminal.external": "Open external shell", "terminal.history": "History",
    "code.advanced": "Advanced", "code.usingFile": "File: {path}", "code.noFileSelected": "Select a file in Workspace, then click Analyze.",
    "code.eyebrow": "Setup", "code.status.title": "Code context", "code.status.noProject": "Connect a project folder to analyze code and power AI suggestions.", "code.status.noLanguages": "Scan your project to detect languages and check language-server readiness.", "code.status.ready": "Your project languages are ready for code intelligence.", "code.status.partial": "{ready} of {total} languages fully ready — install missing servers for richer analysis.", "code.status.fallbackOnly": "Using built-in parsers. Install language servers for full LSP support.",
    "code.readiness.idle": "Not ready", "code.readiness.ready": "Ready", "code.readiness.partial": "Partial", "code.readiness.fallback": "Basic",
    "code.action.analyze": "Analyze project", "code.action.analyzeFile": "Analyze file",
    "code.explore.title": "Explore file", "code.explore.hint": "Inspect symbols, diagnostics, and references for a file in your project.", "code.explore.empty": "Choose a file and run an analysis to see results here.",
    "code.fileLabel": "File", "code.filePlaceholder": "src/main.py", "code.fromWorkspace": "From Workspace",
    "code.tab.symbols": "Symbols", "code.tab.diagnostics": "Diagnostics", "code.tab.hover": "Hover", "code.tab.references": "References",
    "code.line": "Line", "code.column": "Column", "code.referenceQuery": "Reference query", "code.referencePlaceholder": "Optional symbol name",
    "code.setup.title": "Language support", "code.setup.hint": "ArchitectOS uses language servers when installed, with built-in fallbacks for Python, JavaScript/TypeScript, and Java.", "code.setup.summary": "{ready}/{total} ready", "code.lspAdvanced": "LSP configuration (advanced)",
    "code.lang.ready": "Ready", "code.lang.fallback": "Basic support", "code.lang.needsInstall": "Needs install", "code.lang.planned": "Coming soon", "code.lang.files": "{count} file(s)", "code.lang.install": "Install", "code.lang.installing": "Installing…", "code.lang.showCommand": "Show install command", "code.lang.noData": "No languages detected yet. Connect a folder and analyze the project.", "code.lang.installOk": "Installed", "code.lang.installFailed": "Install failed", "code.lang.copy": "Copy", "code.lang.run": "Run",
    "workspace.editor.empty": "No files open — click a file to start", "workspace.editor.placeholder": "Click a file in the tree to edit",
    "workspace.ask.title": "Ask", "workspace.ask.empty": "Same thread as Ask", "workspace.ask.emptyHint": "Messages here stay in sync with the Ask view",
    "workspace.surface.editor": "Editor", "workspace.surface.ask": "Ask", "workspace.surface.split": "Split",
    "nav.work": "Work", "nav.ai": "AI", "nav.setup": "Setup",
    "ask.mode.quick": "Quick", "ask.mode.council": "Council", "ask.mode.multi": "Multi-agent", "ask.mode.memory": "Memory", "ask.mode.memory-mcp": "Memory + MCP",
    "ask.hint.quick": "Agent decides which tools/memory to use and acts on your request.",
    "ask.hint.council": "Same question to several models, then a judge merges one grounded answer.",
    "ask.hint.memory": "Answer only from local project memory — no external provider or MCP tools.",
    "ask.hint.memory-mcp": "Local memory first; refine via enabled MCP tools (Azure Boards, Granola, Filesystem). Writes need approval.",
    "app.eyebrow": "Local-first developer workspace", "workspace.context": "Context Builder", "workspace.projectFiles": "Project Files", "workspace.selectedFile": "Selected File", "workspace.favorites": "Favorites",
    "action.useFolder": "Use Folder", "action.connectFolder": "Connect folder", "action.connectAndIndex": "Connect & index", "action.scanProject": "Scan Project", "action.build": "Build", "action.refresh": "Refresh", "action.buildContext": "Build Context", "action.gitDiff": "Git Diff", "action.saveSettings": "Save Settings",
    "autoscan.selectAll": "Select all", "autoscan.unselectAll": "Unselect all",
    "autoscan.ingest": "🔍 Ingest Sources", "autoscan.rescanAll": "🔄 Rescan all sources",
    "settings.autoRescan": "Rescan sources on startup",
    "folder.title": "Connect project folder", "folder.subtitle": "Choose a folder on this computer to load files and build project memory.", "folder.moreHint": "Analyze and reindex are available in Project Settings.",
    "wizard.title": "Create project", "wizard.welcomeTitle": "Welcome to ArchitectOS", "wizard.lead": "Choose a folder on this computer. ArchitectOS will load files and build project memory.", "wizard.welcomeLead": "Connect a project folder to start working with AI on your code.", "wizard.folderLabel": "Project folder", "wizard.browse": "Browse…", "wizard.nameLabel": "Project name", "wizard.nameHint": "Filled from the folder name — you can change it", "wizard.autoIndex": "Index files after creating", "wizard.advanced": "Advanced options", "wizard.codeStyle": "Code style", "wizard.codeStyle.none": "None", "wizard.ignorePatterns": "Ignore patterns", "wizard.ignoreHint": "One pattern per line", "wizard.autoMemory": "Build memory tree automatically", "wizard.memoryScope": "Memory scope", "wizard.memoryScope.project": "Project", "wizard.memoryScope.shared": "Shared", "wizard.memoryScope.global": "Global", "wizard.skip": "Skip for now", "wizard.cancel": "Cancel", "wizard.create": "Create project", "wizard.getStarted": "Get started", "wizard.pickerOpening": "Opening folder picker…", "wizard.pickerSelected": "Folder selected.", "wizard.pickerCancelled": "Folder selection cancelled.",     "wizard.pickerUnavailable": "Folder picker is unavailable. Type the path manually.", "wizard.validation": "Select a folder and enter a project name.", "wizard.pickerWaiting": "System folder dialog is open — choose a folder or cancel.",
    "error.serverUnreachable": "Cannot reach ArchitectOS server. Run python3 run_architectos.py and open the URL it prints (for example http://127.0.0.1:8765). You can also type the folder path manually.", "error.invalidResponse": "Server returned an invalid response. Restart ArchitectOS and try again.",
    "provider.retryAuto": "Retry with Auto",
    "graph.expand": "Expand", "graph.expandTitle": "Memory Graph", "graph.expandHint": "Drag nodes, scroll to zoom, double-click to inspect, Esc to close", "graph.fit": "Fit", "graph.closeExpand": "Close", "graph.rebuildLinks": "Rebuild Links", "graph.rebuildLinksShort": "Links", "graph.refresh": "Refresh", "graph.inspectHint": "Double-click a node to inspect", "graph.tip.pinned": "Show only pinned memory nodes", "graph.tip.fit": "Fit the graph into the visible area", "graph.tip.expand": "Open the graph in fullscreen", "graph.tip.rebuildLinks": "Rebuild relationships between memory nodes", "graph.tip.refresh": "Reload the graph with current filters",
    "lifecycle.title": "Memory Lifecycle", "lifecycle.desc": "Tracks how project memory ages: fresh → stable → stale → archived. Unused items cool down; useful ones stay long-term.", "lifecycle.runDecay": "Run Decay", "lifecycle.tip.runDecay": "Apply retention rules now: refresh used memory, archive or delete stale short-term items according to settings",
    "providers.eyebrow": "AI routing", "providers.title": "Providers", "providers.status.empty": "Connect and test AI backends to power Ask and agents.", "providers.status.summary": "{ready} of {total} ready · {enabled} enabled", "providers.status.ready": "Ready", "providers.status.error": "Needs setup", "providers.status.disabled": "Disabled", "providers.status.planned": "Planned", "providers.readiness.ready": "Ready", "providers.readiness.partial": "Partial", "providers.readiness.idle": "Not ready", "providers.connectEnv": "Connect from env", "providers.testAll": "Test all", "providers.backendsTitle": "Connected backends", "providers.backendsHint": "Enable providers for auto-routing. Open configuration for API keys and CLI paths.", "providers.routerTitle": "Smart Router", "providers.routerHint": "Choose how ArchitectOS balances quality, cost, and speed.", "providers.strategy": "Strategy", "providers.preset.cheap": "Cheap", "providers.preset.fast": "Fast", "providers.preset.quality": "Quality", "providers.preset.balanced": "Balanced", "providers.advancedWeights": "Advanced weights", "providers.saveRouter": "Save router", "providers.previewTitle": "Routing preview", "providers.previewPlaceholder": "Describe a task to preview routing", "providers.preview": "Preview", "providers.runsTitle": "Recent runs",
    "placeholder.projectName": "Project name", "placeholder.projectRoot": "Project folder path", "placeholder.context": "Build context for a task, review, decision, or architecture question", "placeholder.selectedFile": "Select a file",
    "settings.ui": "UI Settings", "settings.theme": "Theme", "settings.density": "Density", "settings.language": "Language", "settings.memory": "Memory enabled", "settings.lifecycle": "Lifecycle enabled", "settings.refresh": "Refresh on access", "settings.autoRescan": "Rescan sources on startup", "settings.shortTtl": "Short-term TTL", "settings.archive": "Archive after days", "settings.delete": "Delete after days", "settings.promote": "Promote after hits",
    "settings.embeddings": "Memory Embeddings", "settings.embeddingsEnabled": "Embeddings enabled", "settings.embeddingProvider": "Provider", "settings.embeddingModel": "Model", "settings.embeddingDims": "Dimensions", "settings.vectorPool": "Vector pool", "settings.vectorMinScore": "Min score", "settings.vectorTimeout": "Query timeout (ms)", "settings.reindexStartup": "Reindex on startup", "settings.saveEmbeddings": "Save Embeddings", "settings.rebuildEmbeddings": "Rebuild Index",
    "memory.embeddingsCoverage": "Embeddings Indexed", "memory.embeddingsMetric": "Embeddings",
    "chat.usageTitle": "Token usage for this reply", "usage.tokensMetric": "Tokens", "usage.costMetric": "Est. cost", "usage.byModel": "Usage by model", "usage.empty": "No token usage recorded yet. Send a chat request to start tracking.",
    "settings.bundle": "Profile Bundle", "settings.security": "Security Preview", "language.en": "English", "language.ru": "Russian", "language.uk": "Ukrainian", "language.he": "Hebrew"
  },
  ru: {
    "view.workspace": "Рабочая область", "view.chat": "Спросить", "view.memory": "Память", "view.tasks": "Задачи", "view.agents": "Агенты", "view.providers": "Провайдеры", "view.mcp": "MCP", "view.code": "Код", "view.terminal": "Выполнить команду", "view.analytics": "Аналитика", "view.settings": "Настройки",
    "terminal.title": "Выполнить команду", "terminal.hint": "Одноразовый запуск команды в папке проекта. Опасные команды блокируются, пока не включён флаг destructive.", "terminal.external": "Открыть внешнюю оболочку", "terminal.history": "История",
    "code.advanced": "Дополнительно", "code.usingFile": "Файл: {path}", "code.noFileSelected": "Выберите файл в Workspace и нажмите «Анализировать».",
    "code.eyebrow": "Настройка", "code.status.title": "Контекст кода", "code.status.noProject": "Подключите папку проекта, чтобы анализировать код и улучшать подсказки AI.", "code.status.noLanguages": "Просканируйте проект, чтобы определить языки и проверить language servers.", "code.status.ready": "Языки проекта готовы для code intelligence.", "code.status.partial": "{ready} из {total} языков полностью готовы — установите недостающие серверы для более глубокого анализа.", "code.status.fallbackOnly": "Используются встроенные парсеры. Установите language servers для полной поддержки LSP.",
    "code.readiness.idle": "Не готов", "code.readiness.ready": "Готов", "code.readiness.partial": "Частично", "code.readiness.fallback": "Базовый",
    "code.action.analyze": "Анализировать проект", "code.action.analyzeFile": "Анализировать файл",
    "code.explore.title": "Обзор файла", "code.explore.hint": "Символы, диагностика и ссылки для файла в проекте.", "code.explore.empty": "Выберите файл и запустите анализ, чтобы увидеть результаты.",
    "code.fileLabel": "Файл", "code.filePlaceholder": "src/main.py", "code.fromWorkspace": "Из Workspace",
    "code.tab.symbols": "Символы", "code.tab.diagnostics": "Диагностика", "code.tab.hover": "Hover", "code.tab.references": "Ссылки",
    "code.line": "Строка", "code.column": "Столбец", "code.referenceQuery": "Поиск ссылки", "code.referencePlaceholder": "Имя символа (необязательно)",
    "code.setup.title": "Поддержка языков", "code.setup.hint": "ArchitectOS использует language servers при установке и встроенные fallback для Python, JS/TS и Java.", "code.setup.summary": "{ready}/{total} готово", "code.lspAdvanced": "Настройка LSP (для продвинутых)",
    "code.lang.ready": "Готов", "code.lang.fallback": "Базовая поддержка", "code.lang.needsInstall": "Нужна установка", "code.lang.planned": "Скоро", "code.lang.files": "{count} файл(ов)", "code.lang.install": "Установить", "code.lang.installing": "Установка…", "code.lang.showCommand": "Показать команду установки", "code.lang.noData": "Языки не обнаружены. Подключите папку и проанализируйте проект.", "code.lang.installOk": "Установлено", "code.lang.installFailed": "Ошибка установки", "code.lang.copy": "Копировать", "code.lang.run": "Запустить",
    "workspace.editor.empty": "Нет открытых файлов — кликните по файлу", "workspace.editor.placeholder": "Кликните по файлу в дереве для редактирования",
    "workspace.ask.title": "Спросить", "workspace.ask.empty": "Тот же поток, что и в Ask", "workspace.ask.emptyHint": "Сообщения синхронизируются с экраном Ask",
    "workspace.surface.editor": "Редактор", "workspace.surface.ask": "Ask", "workspace.surface.split": "Split",
    "nav.work": "Работа", "nav.ai": "AI", "nav.setup": "Настройка",
    "ask.mode.quick": "Быстрый", "ask.mode.council": "Совет", "ask.mode.multi": "Мульти‑агент", "ask.mode.memory": "Память", "ask.mode.memory-mcp": "Память + MCP",
    "ask.hint.quick": "Агент сам выбирает инструменты/память и выполняет запрос.",
    "ask.hint.council": "Один вопрос — несколько моделей, затем судья сводит один обоснованный ответ.",
    "ask.hint.memory": "Ответ только из локальной памяти проекта — без внешнего провайдера и MCP.",
    "ask.hint.memory-mcp": "Сначала локальная память; уточнение через включённые MCP (Azure Boards, Granola, Filesystem). Запись — с approval.",
    "app.eyebrow": "Локальная рабочая область разработчика", "workspace.context": "Сборщик контекста", "workspace.projectFiles": "Файлы проекта", "workspace.selectedFile": "Выбранный файл", "workspace.favorites": "Избранное",
    "action.useFolder": "Использовать папку", "action.connectFolder": "Подключить папку", "action.connectAndIndex": "Подключить и проиндексировать", "action.scanProject": "Сканировать проект", "action.build": "Собрать", "action.refresh": "Обновить", "action.buildContext": "Собрать контекст", "action.gitDiff": "Git diff", "action.saveSettings": "Сохранить настройки",
    "autoscan.selectAll": "Выбрать все", "autoscan.unselectAll": "Снять все",
    "autoscan.ingest": "🔍 Ingest Sources", "autoscan.rescanAll": "🔄 Пересканировать все",
    "settings.autoRescan": "Пересканировать источники при запуске",
    "folder.title": "Подключить папку проекта", "folder.subtitle": "Выберите папку на этом компьютере, чтобы загрузить файлы и построить память проекта.", "folder.moreHint": "Анализ и переиндексация доступны в настройках проекта.",
    "wizard.title": "Создать проект", "wizard.welcomeTitle": "Добро пожаловать в ArchitectOS", "wizard.lead": "Выберите папку на этом компьютере. ArchitectOS загрузит файлы и построит память проекта.", "wizard.welcomeLead": "Подключите папку проекта, чтобы начать работать с AI над кодом.", "wizard.folderLabel": "Папка проекта", "wizard.browse": "Обзор…", "wizard.nameLabel": "Название проекта", "wizard.nameHint": "Подставляется из имени папки — можно изменить", "wizard.autoIndex": "Проиндексировать файлы после создания", "wizard.advanced": "Дополнительные настройки", "wizard.codeStyle": "Стиль кода", "wizard.codeStyle.none": "Нет", "wizard.ignorePatterns": "Игнорируемые шаблоны", "wizard.ignoreHint": "Один шаблон на строку", "wizard.autoMemory": "Автоматически строить дерево памяти", "wizard.memoryScope": "Область памяти", "wizard.memoryScope.project": "Проект", "wizard.memoryScope.shared": "Общая", "wizard.memoryScope.global": "Глобальная", "wizard.skip": "Пропустить", "wizard.cancel": "Отмена", "wizard.create": "Создать проект", "wizard.getStarted": "Начать", "wizard.pickerOpening": "Открывается выбор папки…", "wizard.pickerSelected": "Папка выбрана.", "wizard.pickerCancelled": "Выбор папки отменён.",     "wizard.pickerUnavailable": "Выбор папки недоступен. Введите путь вручную.", "wizard.validation": "Выберите папку и введите название проекта.", "wizard.pickerWaiting": "Открыт системный диалог — выберите папку или отмените.",
    "error.serverUnreachable": "Нет связи с сервером ArchitectOS. Запустите python3 run_architectos.py и откройте указанный URL (например http://127.0.0.1:8765). Путь к папке можно ввести вручную.", "error.invalidResponse": "Сервер вернул некорректный ответ. Перезапустите ArchitectOS и попробуйте снова.",
    "provider.retryAuto": "Повторить с Auto",
    "graph.expand": "Развернуть", "graph.expandTitle": "Memory Graph", "graph.expandHint": "Перетаскивайте узлы, колёсико — зум, двойной клик — детали, Esc — закрыть", "graph.fit": "Вписать", "graph.closeExpand": "Закрыть", "graph.rebuildLinks": "Пересобрать связи", "graph.rebuildLinksShort": "Связи", "graph.refresh": "Обновить", "graph.inspectHint": "Двойной клик по узлу — детали", "graph.tip.pinned": "Показать только закреплённые узлы памяти", "graph.tip.fit": "Вписать граф в видимую область", "graph.tip.expand": "Открыть граф на весь экран", "graph.tip.rebuildLinks": "Пересобрать связи между узлами памяти", "graph.tip.refresh": "Перезагрузить граф с текущими фильтрами",
    "lifecycle.title": "Жизненный цикл памяти", "lifecycle.desc": "Показывает, как стареет память проекта: fresh → stable → stale → archived. Неиспользуемое остывает, полезное остаётся в long-term.", "lifecycle.runDecay": "Запустить Decay", "lifecycle.tip.runDecay": "Сейчас применить правила хранения: обновить используемую память, архивировать или удалить устаревшие short-term записи по настройкам",
    "providers.eyebrow": "AI-маршрутизация", "providers.title": "Провайдеры", "providers.status.empty": "Подключите и протестируйте AI-бэкенды для Ask и агентов.", "providers.status.summary": "{ready} из {total} готовы · {enabled} включено", "providers.status.ready": "Готов", "providers.status.error": "Нужна настройка", "providers.status.disabled": "Выключен", "providers.status.planned": "Запланирован", "providers.readiness.ready": "Готов", "providers.readiness.partial": "Частично", "providers.readiness.idle": "Не готов", "providers.connectEnv": "Из env", "providers.testAll": "Тест всех", "providers.backendsTitle": "Подключённые бэкенды", "providers.backendsHint": "Включите провайдеры для auto-route. Конфигурация — внутри карточки.", "providers.routerTitle": "Smart Router", "providers.routerHint": "Баланс качества, стоимости и скорости.", "providers.strategy": "Стратегия", "providers.preset.cheap": "Дёшево", "providers.preset.fast": "Быстро", "providers.preset.quality": "Качество", "providers.preset.balanced": "Баланс", "providers.advancedWeights": "Веса (дополнительно)", "providers.saveRouter": "Сохранить router", "providers.previewTitle": "Превью маршрута", "providers.previewPlaceholder": "Опишите задачу для превью маршрутизации", "providers.preview": "Превью", "providers.runsTitle": "Недавние запуски",
    "placeholder.projectName": "Название проекта", "placeholder.projectRoot": "Путь к папке проекта", "placeholder.context": "Собрать контекст для задачи, ревью, решения или архитектурного вопроса", "placeholder.selectedFile": "Выберите файл",
    "settings.ui": "Настройки UI", "settings.theme": "Тема", "settings.density": "Плотность", "settings.language": "Язык", "settings.memory": "Память включена", "settings.lifecycle": "Жизненный цикл включен", "settings.refresh": "Обновлять при доступе", "settings.autoRescan": "Пересканировать источники при запуске", "settings.shortTtl": "TTL краткосрочной памяти", "settings.archive": "Архивировать через дней", "settings.delete": "Удалять через дней", "settings.promote": "Повышать после обращений",
    "settings.embeddings": "Эмбеддинги памяти", "settings.embeddingsEnabled": "Эмбеддинги включены", "settings.embeddingProvider": "Провайдер", "settings.embeddingModel": "Модель", "settings.embeddingDims": "Размерность", "settings.vectorPool": "Vector pool", "settings.vectorMinScore": "Мин. score", "settings.vectorTimeout": "Таймаут query (мс)", "settings.reindexStartup": "Реиндекс при старте", "settings.saveEmbeddings": "Сохранить эмбеддинги", "settings.rebuildEmbeddings": "Пересобрать индекс",
    "memory.embeddingsCoverage": "Проиндексировано embeddings", "memory.embeddingsMetric": "Embeddings",
    "chat.usageTitle": "Токены этого ответа", "usage.tokensMetric": "Токены", "usage.costMetric": "Оценка $", "usage.byModel": "По моделям", "usage.empty": "Пока нет данных. Отправьте запрос в Ask — usage появится здесь.",
    "settings.bundle": "Пакет профиля", "settings.security": "Проверка безопасности", "language.en": "Английский", "language.ru": "Русский", "language.uk": "Украинский", "language.he": "Иврит"
  },
  uk: {
    "view.workspace": "Робоча область", "view.chat": "Запитати", "view.memory": "Пам'ять", "view.tasks": "Завдання", "view.agents": "Агенти", "view.providers": "Провайдери", "view.mcp": "MCP", "view.code": "Код", "view.terminal": "Виконати команду", "view.analytics": "Аналітика", "view.settings": "Налаштування",
    "terminal.title": "Виконати команду", "terminal.hint": "Одноразовий запуск команди в папці проєкту. Небезпечні команди блокуються, доки не увімкнено destructive.", "terminal.external": "Відкрити зовнішню оболонку", "terminal.history": "Історія",
    "code.advanced": "Додатково", "code.usingFile": "Файл: {path}", "code.noFileSelected": "Виберіть файл у Workspace і натисніть Symbols.",
    "workspace.editor.empty": "Немає відкритих файлів — клацніть по файлу", "workspace.editor.placeholder": "Клацніть по файлу в дереві для редагування",
    "workspace.ask.title": "Запитати", "workspace.ask.empty": "Той самий потік, що й у Ask", "workspace.ask.emptyHint": "Повідомлення синхронізуються з екраном Ask",
    "workspace.surface.editor": "Редактор", "workspace.surface.ask": "Ask", "workspace.surface.split": "Split",
    "nav.work": "Робота", "nav.ai": "AI", "nav.setup": "Налаштування",
    "ask.mode.quick": "Швидкий", "ask.mode.council": "Рада", "ask.mode.multi": "Мульти‑агент", "ask.mode.memory": "Пам'ять", "ask.mode.memory-mcp": "Пам'ять + MCP",
    "ask.hint.quick": "Агент сам обирає інструменти/пам'ять і виконує запит.",
    "ask.hint.council": "Одне питання — кілька моделей, потім суддя зводить одну обґрунтовану відповідь.",
    "ask.hint.memory": "Відповідь лише з локальної пам'яті проєкту — без зовнішнього провайдера і MCP.",
    "ask.hint.memory-mcp": "Спочатку локальна пам'ять; уточнення через увімкнені MCP (Azure Boards, Granola, Filesystem). Запис — з approval.",
    "app.eyebrow": "Локальна робоча область розробника", "workspace.context": "Збірник контексту", "workspace.projectFiles": "Файли проєкту", "workspace.selectedFile": "Вибраний файл", "workspace.favorites": "Обране",
    "action.useFolder": "Використати папку", "action.connectFolder": "Підключити папку", "action.connectAndIndex": "Підключити й проіндексувати", "action.scanProject": "Сканувати проєкт", "action.build": "Зібрати", "action.refresh": "Оновити", "action.buildContext": "Зібрати контекст", "action.gitDiff": "Git diff", "action.saveSettings": "Зберегти налаштування",
    "autoscan.selectAll": "Вибрати всі", "autoscan.unselectAll": "Зняти всі",
    "autoscan.ingest": "🔍 Ingest Sources", "autoscan.rescanAll": "🔄 Пересканувати всі",
    "settings.autoRescan": "Пересканувати джерела при запуску",
    "folder.title": "Підключити папку проєкту", "folder.subtitle": "Виберіть папку на цьому комп'ютері, щоб завантажити файли та побудувати пам'ять проєкту.", "folder.moreHint": "Аналіз і переіндексація доступні в налаштуваннях проєкту.",
    "wizard.title": "Створити проєкт", "wizard.welcomeTitle": "Ласкаво просимо до ArchitectOS", "wizard.lead": "Виберіть папку на цьому комп'ютері. ArchitectOS завантажить файли та побудує пам'ять проєкту.", "wizard.welcomeLead": "Підключіть папку проєкту, щоб почати працювати з AI над кодом.", "wizard.folderLabel": "Папка проєкту", "wizard.browse": "Огляд…", "wizard.nameLabel": "Назва проєкту", "wizard.nameHint": "Підставляється з імені папки — можна змінити", "wizard.autoIndex": "Проіндексувати файли після створення", "wizard.advanced": "Додаткові налаштування", "wizard.codeStyle": "Стиль коду", "wizard.codeStyle.none": "Немає", "wizard.ignorePatterns": "Шаблони ігнорування", "wizard.ignoreHint": "Один шаблон на рядок", "wizard.autoMemory": "Автоматично будувати дерево пам'яті", "wizard.memoryScope": "Область пам'яті", "wizard.memoryScope.project": "Проєкт", "wizard.memoryScope.shared": "Спільна", "wizard.memoryScope.global": "Глобальна", "wizard.skip": "Пропустити", "wizard.cancel": "Скасувати", "wizard.create": "Створити проєкт", "wizard.getStarted": "Почати", "wizard.pickerOpening": "Відкривається вибір папки…", "wizard.pickerSelected": "Папку вибрано.", "wizard.pickerCancelled": "Вибір папки скасовано.", "wizard.pickerUnavailable": "Вибір папки недоступний. Введіть шлях вручну.", "wizard.validation": "Виберіть папку та введіть назву проєкту.",
    "provider.retryAuto": "Повторити з Auto",
    "placeholder.projectName": "Назва проєкту", "placeholder.projectRoot": "Шлях до папки проєкту", "placeholder.context": "Зібрати контекст для завдання, рев'ю, рішення або архітектурного питання", "placeholder.selectedFile": "Виберіть файл",
    "settings.ui": "Налаштування UI", "settings.theme": "Тема", "settings.density": "Щільність", "settings.language": "Мова", "settings.memory": "Пам'ять увімкнена", "settings.lifecycle": "Життєвий цикл увімкнено", "settings.refresh": "Оновлювати при доступі", "settings.shortTtl": "TTL короткострокової пам'яті", "settings.archive": "Архівувати через днів", "settings.delete": "Видаляти через днів", "settings.promote": "Підвищувати після звернень",
    "settings.autoRescan": "Пересканувати джерела при запуску",
    "settings.embeddings": "Ембедінги пам'яті", "settings.embeddingsEnabled": "Ембедінги увімкнено", "settings.embeddingProvider": "Провайдер", "settings.embeddingModel": "Модель", "settings.embeddingDims": "Розмірність", "settings.vectorPool": "Vector pool", "settings.vectorMinScore": "Мін. score", "settings.vectorTimeout": "Таймаут query (мс)", "settings.reindexStartup": "Реіндекс при старті", "settings.saveEmbeddings": "Зберегти ембедінги", "settings.rebuildEmbeddings": "Перезібрати індекс",
    "memory.embeddingsCoverage": "Проіндексовано embeddings", "memory.embeddingsMetric": "Embeddings",
    "chat.usageTitle": "Токени цієї відповіді", "usage.tokensMetric": "Токени", "usage.costMetric": "Оцінка $", "usage.byModel": "За моделями", "usage.empty": "Поки немає даних. Надішліть запит в Ask — usage з’явиться тут.",
    "settings.bundle": "Пакет профілю", "settings.security": "Перевірка безпеки", "language.en": "Англійська", "language.ru": "Російська", "language.uk": "Українська", "language.he": "Іврит"
  },
  he: {
    "view.workspace": "סביבת עבודה", "view.chat": "שאל", "view.memory": "זיכרון", "view.tasks": "משימות", "view.agents": "סוכנים", "view.providers": "ספקים", "view.mcp": "MCP", "view.code": "קוד", "view.terminal": "הרץ פקודה", "view.analytics": "אנליטיקה", "view.settings": "הגדרות",
    "terminal.title": "הרץ פקודה", "terminal.hint": "הרצת פקודה חד‑פעמית בתיקיית הפרויקט. פקודות מסוכנות נחסמות עד שמאפשרים destructive.", "terminal.external": "פתח מעטפת חיצונית", "terminal.history": "היסטוריה",
    "code.advanced": "מתקדם", "code.usingFile": "קובץ: {path}", "code.noFileSelected": "בחר קובץ ב‑Workspace ולחץ Symbols.",
    "workspace.editor.empty": "אין קבצים פתוחים — לחץ על קובץ", "workspace.editor.placeholder": "לחץ על קובץ בעץ לעריכה",
    "workspace.ask.title": "שאל", "workspace.ask.empty": "אותו שרשור כמו ב‑Ask", "workspace.ask.emptyHint": "הודעות כאן מסונכרנות עם מסך Ask",
    "workspace.surface.editor": "עורך", "workspace.surface.ask": "Ask", "workspace.surface.split": "Split",
    "nav.work": "עבודה", "nav.ai": "AI", "nav.setup": "הגדרה",
    "ask.mode.quick": "מהיר", "ask.mode.council": "מועצה", "ask.mode.multi": "רב‑סוכן", "ask.mode.memory": "זיכרון", "ask.mode.memory-mcp": "זיכרון + MCP",
    "ask.hint.quick": "הסוכן מחליט באילו כלים/זיכרון להשתמש ומבצע את הבקשה.",
    "ask.hint.council": "שאלה אחת למספר מודלים, ואז שופט ממזג תשובה אחת מבוססת.",
    "ask.hint.memory": "תשובה מזיכרון מקומי בלבד — ללא ספק חיצוני וללא MCP.",
    "ask.hint.memory-mcp": "קודם זיכרון מקומי; דיוק דרך MCP פעיל (Azure Boards, Granola, Filesystem). כתיבה דורשת אישור.",
    "app.eyebrow": "סביבת עבודה מקומית למפתחים", "workspace.context": "בונה הקשר", "workspace.projectFiles": "קבצי פרויקט", "workspace.selectedFile": "קובץ נבחר", "workspace.favorites": "מועדפים",
    "action.useFolder": "השתמש בתיקייה", "action.connectFolder": "חבר תיקייה", "action.connectAndIndex": "חבר ואנדקס", "action.scanProject": "סרוק פרויקט", "action.build": "בנה", "action.refresh": "רענן", "action.buildContext": "בנה הקשר", "action.gitDiff": "Git diff", "action.saveSettings": "שמור הגדרות",
    "autoscan.selectAll": "בחר הכל", "autoscan.unselectAll": "בטל הכל",
    "autoscan.ingest": "🔍 Ingest Sources", "autoscan.rescanAll": "🔄 סרוק מחדש הכול",
    "settings.autoRescan": "סרוק מקורות מחדש בהפעלה",
    "folder.title": "חבר תיקיית פרויקט", "folder.subtitle": "בחר תיקייה במחשב הזה כדי לטעון קבצים ולבנות זיכרון פרויקט.", "folder.moreHint": "ניתוח ואינדוקס מחדש זמינים בהגדרות הפרויקט.",
    "wizard.title": "צור פרויקט", "wizard.welcomeTitle": "ברוכים הבאים ל‑ArchitectOS", "wizard.lead": "בחר תיקייה במחשב הזה. ArchitectOS יטען קבצים ויבנה זיכרון פרויקט.", "wizard.welcomeLead": "חבר תיקיית פרויקט כדי להתחיל לעבוד עם AI על הקוד שלך.", "wizard.folderLabel": "תיקיית פרויקט", "wizard.browse": "עיון…", "wizard.nameLabel": "שם הפרויקט", "wizard.nameHint": "מתמלא משם התיקייה — אפשר לשנות", "wizard.autoIndex": "אנדקס קבצים אחרי יצירה", "wizard.advanced": "אפשרויות מתקדמות", "wizard.codeStyle": "סגנון קוד", "wizard.codeStyle.none": "ללא", "wizard.ignorePatterns": "תבניות התעלמות", "wizard.ignoreHint": "תבנית אחת בכל שורה", "wizard.autoMemory": "בנה עץ זיכרון אוטומטית", "wizard.memoryScope": "היקף זיכרון", "wizard.memoryScope.project": "פרויקט", "wizard.memoryScope.shared": "משותף", "wizard.memoryScope.global": "גלובלי", "wizard.skip": "דלג לעת עתה", "wizard.cancel": "ביטול", "wizard.create": "צור פרויקט", "wizard.getStarted": "התחל", "wizard.pickerOpening": "פותח בוחר תיקיות…", "wizard.pickerSelected": "תיקייה נבחרה.", "wizard.pickerCancelled": "בחירת התיקייה בוטלה.", "wizard.pickerUnavailable": "בוחר התיקיות לא זמין. הקלד את הנתיב ידנית.", "wizard.validation": "בחר תיקייה והזן שם פרויקט.",
    "provider.retryAuto": "נסה שוב עם Auto",
    "placeholder.projectName": "שם הפרויקט", "placeholder.projectRoot": "נתיב תיקיית הפרויקט", "placeholder.context": "בנה הקשר למשימה, סקירה, החלטה או שאלת ארכיטקטורה", "placeholder.selectedFile": "בחר קובץ",
    "settings.ui": "הגדרות ממשק", "settings.theme": "ערכת נושא", "settings.density": "צפיפות", "settings.language": "שפה", "settings.memory": "זיכרון פעיל", "settings.lifecycle": "מחזור חיים פעיל", "settings.refresh": "רענון בעת גישה", "settings.shortTtl": "TTL לזיכרון קצר", "settings.archive": "ארכוב אחרי ימים", "settings.delete": "מחיקה אחרי ימים", "settings.promote": "קידום אחרי שימושים",
    "settings.autoRescan": "סרוק מקורות מחדש בהפעלה",
    "settings.embeddings": "Embedding לזיכרון", "settings.embeddingsEnabled": "Embeddings פעילים", "settings.embeddingProvider": "ספק", "settings.embeddingModel": "מודל", "settings.embeddingDims": "ממדים", "settings.vectorPool": "Vector pool", "settings.vectorMinScore": "ציון מינ'", "settings.vectorTimeout": "Timeout לשאילתה (ms)", "settings.reindexStartup": "אינדוקס מחדש בהפעלה", "settings.saveEmbeddings": "שמור Embeddings", "settings.rebuildEmbeddings": "בנה אינדקס מחדש",
    "memory.embeddingsCoverage": "Embeddings באינדקס", "memory.embeddingsMetric": "Embeddings",
    "chat.usageTitle": "שימוש בטוקנים לתשובה זו", "usage.tokensMetric": "טוקנים", "usage.costMetric": "עלות משוערת", "usage.byModel": "שימוש לפי מודל", "usage.empty": "אין עדיין נתונים. שלח הודעה ב-Ask כדי להתחיל למדוד.",
    "settings.bundle": "חבילת פרופיל", "settings.security": "בדיקת אבטחה", "language.en": "אנגלית", "language.ru": "רוסית", "language.uk": "אוקראינית", "language.he": "עברית"
  }
};

const AUTH_TOKEN = (document.querySelector('meta[name="architectos-token"]') || {}).content || "";
function authHeaders(extra = {}) {
  return AUTH_TOKEN ? { "X-ArchitectOS-Token": AUTH_TOKEN, ...extra } : { ...extra };
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: authHeaders({ "Content-Type": "application/json", ...(options.headers || {}) }) });
  } catch (error) {
    const message = error?.message || "network error";
    if (/failed to fetch|networkerror|load failed/i.test(message)) {
      throw new Error(t("error.serverUnreachable"));
    }
    throw error;
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error(t("error.invalidResponse"));
  }
  if (!response.ok || payload.error) throw new Error(payload.error || payload.message || `Request failed: ${response.status}`);
  return payload;
}
async function ensureServerOnline() {
  try {
    await fetch("/api/health", { method: "GET", cache: "no-store", headers: authHeaders() });
    return true;
  } catch (error) {
    return false;
  }
}
function projectParam() { return encodeURIComponent(state.projectId || "architectos"); }
function currentProject() { return state.projects.find(project => project.id === state.projectId) || null; }
function isSystemWorkspace(project) { return Boolean(project && project.id === "architectos"); }
function displayProjectName(project) { return isSystemWorkspace(project) ? "System Workspace" : (project ? project.name : ""); }
function syncProjectSwitcherLabel() {
  const label = document.querySelector("#project-select-label");
  if (!label) return;
  label.textContent = isSystemWorkspace(currentProject()) ? "Workspace" : "Project";
}
function syncProjectTerminology() {
  const system = isSystemWorkspace(currentProject());
  const filesTitle = document.querySelector("#workspace-files-title");
  const scanButton = document.querySelector("#scan-project");
  if (filesTitle) filesTitle.textContent = system ? "Workspace Files" : t("workspace.projectFiles");
  if (scanButton) scanButton.textContent = system ? "Scan Workspace" : t("action.scanProject");
}
function t(key) { return (translations[state.language] && translations[state.language][key]) || translations.en[key] || key; }
function escapeHtml(value) { return String(value || "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char])); }
function setText(selector, key) { const el = document.querySelector(selector); if (el) el.textContent = t(key); }
function setPlaceholder(selector, key) { const el = document.querySelector(selector); if (el) el.placeholder = t(key); }
function setButton(selector, key) { setText(selector, key); }
function setTextContent(selector, value) {
  const el = typeof selector === "string" ? document.querySelector(selector) : selector;
  if (el) el.textContent = value;
  return el;
}
function setElementValue(selector, value) {
  const el = typeof selector === "string" ? document.querySelector(selector) : selector;
  if (el) el.value = value;
  return el;
}
function setElementDisabled(selector, disabled) {
  const el = typeof selector === "string" ? document.querySelector(selector) : selector;
  if (el) el.disabled = disabled;
  return el;
}
function on(selector, eventName, handler, options) {
  const el = typeof selector === "string" ? document.querySelector(selector) : selector;
  if (el) el.addEventListener(eventName, handler, options);
  return el;
}
function onAll(selector, eventName, handler, options) {
  document.querySelectorAll(selector).forEach(el => el.addEventListener(eventName, handler, options));
}
function showSnackbar(message, tone = "info") {
  let stack = document.querySelector("#snackbar-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.id = "snackbar-stack";
    stack.className = "snackbar-stack";
    document.body.appendChild(stack);
  }
  const item = document.createElement("div");
  item.className = `snackbar ${tone}`.trim();
  item.setAttribute("role", tone === "error" ? "alert" : "status");
  item.innerHTML = `<span>${escapeHtml(message)}</span><button type="button" aria-label="Dismiss">&times;</button>`;
  stack.appendChild(item);
  const close = () => {
    item.classList.add("leaving");
    window.setTimeout(() => item.remove(), 180);
  };
  on(item.querySelector("button"), "click", close);
  window.setTimeout(close, tone === "error" ? 7000 : 4200);
}
function labelPrefix(inputSelector, key) {
  const input = document.querySelector(inputSelector);
  const label = input ? input.closest("label") : null;
  if (!label) return;
  const control = label.querySelector("input,select,textarea");
  for (const node of Array.from(label.childNodes)) {
    if (node.nodeType === Node.TEXT_NODE) node.remove();
  }
  label.insertBefore(document.createTextNode(t(key)), control || label.firstChild);
}
function syncLanguageMenu() {
  const current = LANGUAGE_META[state.language] || LANGUAGE_META.en;
  const flag = document.querySelector("#language-current-flag");
  const button = document.querySelector("#language-btn");
  if (flag) flag.textContent = current.flag;
  if (button) button.title = `Language: ${current.label}`;
  document.querySelectorAll("[data-language-option]").forEach(option => {
    const active = option.dataset.languageOption === state.language;
    option.classList.toggle("active", active);
    option.setAttribute("aria-checked", active ? "true" : "false");
  });
}
function applyLanguage(language) {
  state.language = translations[language] ? language : "en";
  document.documentElement.lang = state.language;
  document.documentElement.dir = RTL_LANGUAGES.has(state.language) ? "rtl" : "ltr";
  document.body.classList.toggle("rtl", RTL_LANGUAGES.has(state.language));
  document.querySelectorAll("#settings-language-select").forEach(select => { select.value = state.language; });
  document.querySelectorAll(".nav-item").forEach(item => {
    if (item.hidden) return;
    item.textContent = t(`view.${item.dataset.view}`);
  });
  document.querySelectorAll("[data-i18n]").forEach(el => {
    const key = el.dataset.i18n;
    if (key) el.textContent = t(key);
  });
  document.querySelectorAll("[data-ask-mode]").forEach(btn => {
    const mode = btn.dataset.askMode;
    btn.textContent = t(`ask.mode.${mode}`);
  });
  syncAskMode();
  const active = document.querySelector(".nav-item.active");
  setTextContent("#view-title", t(titleByView[(active && active.dataset.view) || "workspace"]));
  setText(".eyebrow", "app.eyebrow");
  setText("#workspace-view .panel:nth-of-type(1) h3", "workspace.context");
  setText("#workspace-view .panel:nth-of-type(2) h3", "workspace.projectFiles");
  setText("#workspace-view .panel:nth-of-type(3) h3", "workspace.selectedFile");
  setText("#workspace-view .panel:nth-of-type(4) h3", "workspace.favorites");
  setButton("#save-project", "action.connectFolder");
  setButton("#init-project", "action.connectAndIndex");
  setText("#project-folder-title", "folder.title");
  setText("#project-folder-subtitle", "folder.subtitle");
  setText("#project-folder-more-hint", "folder.moreHint");
  setButton("#context-form button", "action.build");
  setButton("#refresh-files", "action.refresh");
  setButton("#build-file-context", "action.buildContext");
  setButton("#load-git-diff", "action.gitDiff");
  setPlaceholder("#project-name", "placeholder.projectName");
  setPlaceholder("#project-root-path", "placeholder.projectRoot");
  setPlaceholder("#context-query", "placeholder.context");
  setPlaceholder("#selected-file-path", "placeholder.selectedFile");
  setText("#settings-view .panel:nth-of-type(1) h3", "settings.bundle");
  setText("#settings-view .panel:nth-of-type(2) h3", "settings.ui");
  setText("#settings-view .panel:nth-of-type(3) h3", "settings.embeddings");
  setText("#settings-view .panel:nth-of-type(4) h3", "settings.security");
  labelPrefix("#theme-select", "settings.theme");
  labelPrefix("#density-select", "settings.density");
  labelPrefix("#settings-language-select", "settings.language");
  labelPrefix("#short-term-ttl", "settings.shortTtl");
  labelPrefix("#archive-after", "settings.archive");
  labelPrefix("#delete-after", "settings.delete");
  labelPrefix("#promote-after-hits", "settings.promote");
  labelPrefix("#embedding-provider", "settings.embeddingProvider");
  labelPrefix("#embedding-model", "settings.embeddingModel");
  labelPrefix("#embedding-dimensions", "settings.embeddingDims");
  labelPrefix("#vector-pool", "settings.vectorPool");
  labelPrefix("#vector-min-score", "settings.vectorMinScore");
  labelPrefix("#vector-query-timeout", "settings.vectorTimeout");
  const embeddingsEnabledLabel = document.querySelector("#embeddings-enabled")?.closest("label");
  if (embeddingsEnabledLabel) embeddingsEnabledLabel.lastChild.textContent = ` ${t("settings.embeddingsEnabled")}`;
  const reindexStartupLabel = document.querySelector("#reindex-on-startup")?.closest("label");
  if (reindexStartupLabel) reindexStartupLabel.lastChild.textContent = ` ${t("settings.reindexStartup")}`;
  setButton("#embeddings-form button[type='submit']", "settings.saveEmbeddings");
  setButton("#embeddings-rebuild", "settings.rebuildEmbeddings");
  const memoryLabel = document.querySelector("#memory-enabled")?.closest("label");
  if (memoryLabel) memoryLabel.lastChild.textContent = ` ${t("settings.memory")}`;
  const lifecycleLabel = document.querySelector("#lifecycle-enabled")?.closest("label");
  if (lifecycleLabel) lifecycleLabel.lastChild.textContent = ` ${t("settings.lifecycle")}`;
  const refreshLabel = document.querySelector("#refresh-on-access")?.closest("label");
  if (refreshLabel) refreshLabel.lastChild.textContent = ` ${t("settings.refresh")}`;
  const autoRescanLabel = document.querySelector("#auto-rescan-on-startup")?.closest("label");
  if (autoRescanLabel) autoRescanLabel.lastChild.textContent = ` ${t("settings.autoRescan")}`;
  setButton("#ingest-memory", "autoscan.ingest");
  setButton("#rescan-memory-all", "autoscan.rescanAll");
  setButton("#settings-form button", "action.saveSettings");
  setText("#terminal-panel-title", "terminal.title");
  setText("#terminal-panel-hint", "terminal.hint");
  setButton("#terminal-open", "terminal.external");
  setText("#terminal-view .panel:nth-of-type(2) h3", "terminal.history");
  setText("#code-run-insight", "code.action.analyzeFile");
  setText("#code-analyze-project", "code.action.analyze");
  setText("#code-connect-folder", "action.connectFolder");
  const expandBtn = document.querySelector("#graph-expand");
  if (expandBtn) {
    expandBtn.setAttribute("aria-label", t("graph.expand"));
    expandBtn.title = t("graph.tip.expand");
  }
  setText("#graph-fit-expanded", "graph.fit");
  setText("#graph-close-expand", "graph.closeExpand");
  setText(".memory-lifecycle-heading h4", "lifecycle.title");
  setText(".memory-lifecycle-desc", "lifecycle.desc");
  setText("#run-memory-decay", "lifecycle.runDecay");
  document.querySelectorAll("[data-i18n-title]").forEach(el => {
    el.title = t(el.dataset.i18nTitle);
  });
  setText("#workspace-ask-title", "workspace.ask.title");
  document.querySelectorAll(".workspace-surface-btn [data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    if (key) el.textContent = t(key);
  });
  setText("#editor-tab-empty-hint", "workspace.editor.empty");
  setPlaceholder("#file-editor", "workspace.editor.placeholder");
  syncCodeFileHint();
  syncProjectTerminology();
  syncLanguageMenu();
}
function resolveTheme(theme) {
  if (theme === "dark" || theme === "light") return theme;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(theme) {
  state.theme = theme || "system";
  const resolved = resolveTheme(state.theme);
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.colorScheme = resolved;
}
function applyDensity(density) {
  document.documentElement.dataset.density = density === "compact" ? "compact" : "comfortable";
}
if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (state.theme === "system") applyTheme("system"); });
}
async function saveUiSettings(partial = {}) {
  const ui = {
    theme: document.querySelector("#theme-select")?.value || state.theme || "system",
    density: document.querySelector("#density-select")?.value || "comfortable",
    memory_enabled: document.querySelector("#memory-enabled") ? document.querySelector("#memory-enabled").checked : true,
    language: state.language,
    onboarding_complete: state.onboardingComplete,
    ...partial,
  };
  if (partial.onboarding_complete !== undefined) state.onboardingComplete = partial.onboarding_complete;
  await api("/api/settings", { method: "PATCH", body: JSON.stringify({ ui }) });
}
async function saveWorkspaceSettings(partial = {}) {
  const workspace = {
    current_project_id: state.projectId || "architectos",
    ...partial
  };
  await api("/api/settings", { method: "PATCH", body: JSON.stringify({ workspace }) });
}
function showError(error) {
  const message = error && error.message ? error.message : String(error || "Unexpected error");
  const output = document.querySelector("#context-output");
  if (output) output.textContent = message;
  showSnackbar(message, "error");
}
function providerStatusClass(status, enabled) {
  if (!enabled) return "disabled";
  if (["configured", "ok"].includes(status)) return "ready";
  if (["missing_credentials", "missing_endpoint", "missing_cli", "missing_command", "missing_model", "unreachable", "error"].includes(status)) return "error";
  return "planned";
}
function providerHint(provider) {
  if (provider.last_check && provider.last_check.message) return provider.last_check.message;
  if (provider.id === "openai") return "Set OPENAI_API_KEY, then test this provider.";
  if (provider.id === "azure-openai") return "Set AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT, then set Model to your Azure deployment name. You can also set AZURE_OPENAI_DEPLOYMENT.";
  if (provider.id === "anthropic") return "Set ANTHROPIC_API_KEY, choose a Claude model, then test this provider.";
  if (provider.id === "gemini-cli") return "Antigravity (agy): Sign in with Google, then Test. Gemini CLI is deprecated.";
  if (provider.id === "codex-cli") return "Sign in with ChatGPT in the terminal (like Codex email login), then Test.";
  if (provider.id === "openrouter") {
    if (provider.available_models && provider.available_models.length) return `${provider.available_models.length} OpenRouter model(s) discovered.`;
    return "Set OPENROUTER_API_KEY, refresh models, then choose a model slug.";
  }
  if (provider.id === "ollama") {
    if (provider.available_models && provider.available_models.length) return `${provider.available_models.length} local model(s) discovered.`;
    return "Start Ollama, refresh models, then choose one.";
  }
  if (provider.provider_type === "cli") return "Install the CLI, confirm PATH, then test this provider.";
  return "Configure and test this provider before enabling auto-route.";
}

function providerLoginLabel(provider) {
  if (provider.id === "gemini-cli") return "Sign in with Google";
  if (provider.id === "codex-cli") return "Sign in with ChatGPT";
  return "";
}

function renderResults(container, hits) {
  if (!container) return;
  container.innerHTML = "";
  if (!hits.length) { container.innerHTML = '<div class="result"><strong>No matches</strong><p>Scan project or add memory.</p></div>'; return; }
  for (const hit of hits) {
    const node = hit.node || hit;
    const favorite = Boolean(node.metadata && node.metadata.favorite);
    const el = document.createElement("article");
    el.className = "result";
    const meta = node.metadata || {};
    const tier = meta.memory_tier ? `<span class="badge">${escapeHtml(meta.memory_tier)}</span>` : "";
    const lifecycle = meta.lifecycle_state ? `<span class="badge">${escapeHtml(meta.lifecycle_state)}</span>` : "";
    const access = meta.access_count ? `<span class="badge">used ${escapeHtml(String(meta.access_count))}</span>` : "";
    const longTermButton = meta.memory_tier === "long_term" ? "" : `<button data-long-term="${escapeHtml(node.id)}" type="button">Long-term</button>`;
    el.innerHTML = `<div class="row"><strong>${escapeHtml(node.label)}</strong><div class="provider-actions"><button data-fav="${escapeHtml(node.id)}">${favorite ? "Starred" : "Star"}</button>${longTermButton}</div></div><p>${escapeHtml(node.text)}</p><span class="badge">${escapeHtml(node.type)}</span><span class="badge">${escapeHtml(node.scope)}</span>${tier}${lifecycle}${access}${meta.memory_score ? `<span class="badge">memory ${escapeHtml(String(meta.memory_score))}</span>` : ""}${hit.score ? `<span class="badge">score ${hit.score}</span>` : ""}`;
    container.appendChild(el);
  }
  container.querySelectorAll("[data-fav]").forEach(button => button.addEventListener("click", async () => { await api(`/api/memory/${button.dataset.fav}/favorite`, { method: "PATCH", body: "{}" }); await refreshWorkspace(); await runSearch(document.querySelector("#search-query").value || "memory"); }));
  container.querySelectorAll("[data-long-term]").forEach(button => button.addEventListener("click", async () => { await api(`/api/memory/${button.dataset.longTerm}/promote-long-term`, { method: "POST", body: JSON.stringify({ reason: "ui" }) }); await refreshWorkspace(); await runSearch(document.querySelector("#search-query").value || "memory"); }));
}

async function loadProjects() {
  const payload = await api("/api/projects");
  state.projects = Array.isArray(payload.projects) ? payload.projects : [];
  const realProject = state.projects.find(project => project.id !== "architectos" && project.root_path);
  const savedProject = state.projects.find(project => project.id === state.projectId);
  if (!savedProject && state.projects.length) {
    state.projectId = (realProject || state.projects[0]).id;
    saveWorkspaceSettings({ current_project_id: state.projectId }).catch(showError);
  }
  const select = document.querySelector("#project-select");
  if (!select) {
    syncProjectFields();
    return;
  }
  select.innerHTML = "";

  // Add placeholder option
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select project...";
  placeholder.disabled = true;
  placeholder.selected = !state.projectId;
  select.appendChild(placeholder);

  for (const project of state.projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = displayProjectName(project);
    select.appendChild(option);
  }
  select.value = state.projectId;
  if (!select.value && state.projects.length) {
    state.projectId = (realProject || state.projects[0]).id;
    select.value = state.projectId;
  }
  syncProjectSwitcherLabel();
  syncProjectTerminology();
  syncProjectFields();
}
function syncProjectFields() {
  const project = currentProject();
  const name = document.querySelector("#project-name");
  const root = document.querySelector("#project-root-path");
  const summaryName = document.querySelector("#project-folder-summary-name");
  const summaryPath = document.querySelector("#project-folder-summary-path");
  const currentProjectName = document.querySelector("#current-project-name");

  if (name) name.value = displayProjectName(project);
  if (root) root.value = project ? project.root_path || "" : "";
  if (summaryName) summaryName.textContent = displayProjectName(project) || "No workspace";
  if (summaryPath) summaryPath.textContent = project && project.root_path ? project.root_path : "No folder selected";

  // Update workspace tree project name
  if (currentProjectName) {
    currentProjectName.textContent = displayProjectName(project) || "No project selected";
  }
}
function projectFolderModal() {
  return document.querySelector("#projectFolderModal");
}
function openProjectFolderModal() {
  const modal = projectFolderModal();
  if (!modal) return;
  syncProjectFields();
  modal.classList.add("active");
  modal.setAttribute("aria-hidden", "false");
  document.querySelector("#project-root-path")?.focus();
}
function closeProjectFolderModal() {
  const modal = projectFolderModal();
  if (!modal) return;
  modal.classList.remove("active");
  modal.setAttribute("aria-hidden", "true");
}
function setProjectFolderStatus(message, tone = "") {
  const status = document.querySelector("#project-folder-picker-status");
  if (!status) return;
  status.className = `provider-test ${tone}`.trim();
  status.textContent = message;
}
function setWizardFolderStatus(message, tone = "") {
  const status = document.querySelector("#wizard-folder-status");
  if (!status) return;
  status.className = `wizard-inline-status ${tone}`.trim();
  status.textContent = message;
}
async function pickProjectFolder({ initialPath = "", onStatus } = {}) {
  if (!(await ensureServerOnline())) {
    const message = t("error.serverUnreachable");
    if (onStatus) onStatus(message, "error");
    throw new Error(message);
  }
  if (onStatus) onStatus(t("wizard.pickerOpening"));
  let payload;
  try {
    payload = await api("/api/system/folder-picker", {
      method: "POST",
      body: JSON.stringify({ initial_path: initialPath }),
    });
  } catch (error) {
    if (onStatus) onStatus(error.message || t("wizard.pickerUnavailable"), "error");
    throw error;
  }
  if (!payload.supported) {
    const message = payload.message || t("wizard.pickerUnavailable");
    if (onStatus) onStatus(message, "error");
    return null;
  }
  if (payload.cancelled || !payload.path) {
    if (onStatus) onStatus(t("wizard.pickerCancelled"));
    return null;
  }
  if (onStatus) onStatus(t("wizard.pickerSelected"), "ok");
  return payload.path;
}
async function browseProjectFolder() {
  const root = document.querySelector("#project-root-path");
  const name = document.querySelector("#project-name");
  const path = await pickProjectFolder({
    initialPath: root?.value || "",
    onStatus: setProjectFolderStatus,
  });
  if (!path) return;
  if (root) root.value = path;
  const folderName = path.split(/[\\/]/).filter(Boolean).pop() || "Project";
  if (name && (!name.value.trim() || name.value.trim() === "System Workspace")) name.value = folderName;
}
async function markOnboardingComplete() {
  state.onboardingComplete = true;
  await saveUiSettings({ onboarding_complete: true });
}
function needsProjectOnboarding() {
  if (state.projectFilesStatus === "no_root" || state.projectFilesStatus === "missing_root") return true;
  const project = currentProject();
  if (!project) return true;
  return isSystemWorkspace(project) && !project.root_path;
}
async function maybeShowOnboarding() {
  if (state.onboardingComplete) return;
  if (!needsProjectOnboarding()) {
    await markOnboardingComplete();
    return;
  }
  if (typeof projectWizard !== "undefined" && projectWizard.openWizard) {
    projectWizard.openWizard({ firstRun: true });
    return;
  }
  openProjectFolderModal();
}
function resetGraphFilters() {
  document.querySelectorAll("#graph-task-filter,#graph-provider-filter").forEach(filter => { if (filter) delete filter.dataset.ready; });
  graphState.searchQuery = "";
  graphState.groupFilter = "";
  graphState.densityLevel = 2;
  setElementValue("#graph-search", "");
  setElementValue("#graph-group-filter", "");
  const density = document.querySelector("#graph-density-level");
  if (density) density.value = "2";
  const densityValue = document.querySelector("#graph-density-value");
  if (densityValue) densityValue.textContent = "2";
}
async function saveProjectFromFolder() {
  const current = currentProject();
  const root = (document.querySelector("#project-root-path")?.value || current?.root_path || "").trim();
  const name = (document.querySelector("#project-name")?.value || current?.name || "").trim() || root.split(/[\\/]/).filter(Boolean).pop() || "Project";
  if (!root) throw new Error("Project folder path is required.");
  const project = await api("/api/projects", { method: "POST", body: JSON.stringify({ name, root_path: root }) });
  state.projectId = project.project_id || project.id || (project.project && project.project.id);
  if (!state.projectId) throw new Error("Project was saved but no project id was returned.");
  await saveWorkspaceSettings({ current_project_id: state.projectId });
  state.selectedFile = "";
  await markOnboardingComplete();
  await loadProjects();
  await refreshWorkspace();
  await loadProjectFiles();
  showSnackbar(project.message || `Project ${name} was saved.`, project.created === false ? "info" : "success");
  return project;
}
async function initProjectFromFolder() {
  await saveProjectFromFolder();
  return runProjectIndex({ reindex: true, limit: 80, label: "Initial index" });
}
async function scanSelectedProject() {
  return runProjectIndex({ reindex: false, limit: 40, label: "Analyze" });
}
async function reindexSelectedProject() {
  return runProjectIndex({ reindex: true, limit: 120, label: "Reindex" });
}
async function runProjectIndex({ reindex = false, limit = 40, label = "Analyze" } = {}) {
  const current = currentProject();
  const root = (document.querySelector("#project-root-path")?.value || current?.root_path || "").trim();
  const name = (document.querySelector("#project-name")?.value || current?.name || "").trim();
  const payload = { project_id: state.projectId, root_path: root, name, limit, reindex, rebuild_links: reindex };
  setTextContent("#context-output", `${label} running...`);
  const result = await api("/api/project/scan", { method: "POST", body: JSON.stringify(payload) });
  state.projectId = result.project_id || state.projectId;
  state.selectedFile = "";
  resetGraphFilters();
  await loadProjects();
  const linked = result.links ? `, ${result.links.created || 0} new link(s)` : "";
  const archived = result.archived ? `, ${result.archived} stale item(s) archived` : "";
  setTextContent("#context-output", `${label} complete: ${result.count} file memory item(s) from ${result.root}${archived}${linked}.`);
  await refreshWorkspace();
  await loadProjectFiles();
  await loadGraph();
  return result;
}
async function buildContext(query) {
  const payload = await api(`/api/context?query=${encodeURIComponent(query)}&project_id=${projectParam()}&limit=8`);
  setTextContent("#context-output", payload.context);
}
async function runSearch(query, targetId = "search-results") {
  const payload = await api(`/api/memory/search?query=${encodeURIComponent(query)}&project_id=${projectParam()}&limit=10&refresh=0`);
  renderResults(document.querySelector(`#${targetId}`), payload.hits);
}

function scheduleGraphLoad() {
  const memoryView = document.querySelector("#memory-view");
  if (!memoryView || !memoryView.classList.contains("active")) return;
  requestAnimationFrame(() => {
    resizeGraphCanvas();
    requestAnimationFrame(() => loadGraph().catch(showError));
  });
}

function renderFileTreeEmptyState(list, payload) {
  const status = payload.status || "";
  const configuredRoot = (payload.configured_root || "").trim();

  if (status === "missing_root" || status === "no_root") {
    const isMissing = status === "missing_root";
    const title = isMissing ? "Folder not available here" : "No folder connected";
    const message = isMissing
      ? "This project's folder can't be found on this computer. Update its path or connect another folder to load files and index memory."
      : "Connect a project folder to load its files and start building memory.";
    const primaryLabel = isMissing ? "Update folder" : "Connect folder";
    const pathLine = isMissing && configuredRoot
      ? `<code class="file-tree-empty-path">${escapeHtml(configuredRoot)}</code>`
      : "";

    list.innerHTML = `
      <div class="file-tree-empty file-tree-onboard">
        <div class="file-tree-onboard-icon">📁</div>
        <strong class="file-tree-onboard-title">${escapeHtml(title)}</strong>
        <p class="file-tree-onboard-text">${escapeHtml(message)}</p>
        ${pathLine}
        <div class="file-tree-onboard-actions">
          <button type="button" class="btn btn-primary" data-onboard-action="folder">${escapeHtml(primaryLabel)}</button>
          <button type="button" class="btn btn-secondary" data-onboard-action="new-project">New project</button>
        </div>
      </div>`;

    const folderBtn = list.querySelector('[data-onboard-action="folder"]');
    if (folderBtn) folderBtn.addEventListener("click", () => openProjectFolderModal());
    const newBtn = list.querySelector('[data-onboard-action="new-project"]');
    if (newBtn) newBtn.addEventListener("click", () => {
      if (typeof projectWizard !== "undefined" && projectWizard.openWizard) projectWizard.openWizard();
      else openProjectFolderModal();
    });
    return;
  }

  const message = payload.message || "No files found here yet. Use Refresh or connect a folder.";
  list.innerHTML = `<div class="file-tree-empty">${escapeHtml(message)}</div>`;
}

async function loadProjectFiles() {
  const list = document.querySelector("#file-list");
  if (!list) return;

  console.log('Loading files for project:', state.projectId);
  list.innerHTML = '<div class="file-tree-empty">Loading project files...</div>';

  let payload;
  try {
    payload = await api(`/api/project/files?project_id=${projectParam()}&limit=5000`);
  } catch (error) {
    list.innerHTML = `<div class="file-tree-empty"><strong>Could not load files</strong><p>${escapeHtml(error.message || String(error))}</p></div>`;
    throw error;
  }

  const files = Array.isArray(payload.files) ? payload.files : [];
  state.projectFilesStatus = payload.status || (files.length ? "ok" : "");
  console.log('Loaded files:', files.length, 'files');

  if (!files.length) {
    renderFileTreeEmptyState(list, payload);
    return;
  }

  // Use file tree rendering
  renderFileTree(files, list, (file) => {
    openProjectFile(file.path).catch(showError);
    fileEditor.openFile(file.path);
  });
}

const fileFind = {
  isOpen: false,
  mode: "name",
  hits: [],
  selectedIndex: 0,
  timer: 0,
  requestSeq: 0,

  el() {
    return document.querySelector("#fileFindModal");
  },

  queryEl() {
    return document.querySelector("#file-find-query");
  },

  maskEl() {
    return document.querySelector("#file-find-mask");
  },

  open(mode = "name") {
    const modal = this.el();
    if (!modal) return;
    this.mode = mode === "content" ? "content" : "name";
    this.syncModeUi();
    modal.classList.add("active");
    modal.setAttribute("aria-hidden", "false");
    this.isOpen = true;
    const input = this.queryEl();
    if (input) {
      input.focus();
      input.select();
    }
    this.scheduleSearch(true);
  },

  close() {
    const modal = this.el();
    if (!modal) return;
    modal.classList.remove("active");
    modal.setAttribute("aria-hidden", "true");
    this.isOpen = false;
    clearTimeout(this.timer);
  },

  toggle(mode = "name") {
    if (this.isOpen && this.mode === mode) {
      this.close();
      return;
    }
    this.open(mode);
  },

  setMode(mode) {
    this.mode = mode === "content" ? "content" : "name";
    this.syncModeUi();
    this.scheduleSearch(true);
    this.queryEl()?.focus();
  },

  syncModeUi() {
    const title = document.querySelector("#file-find-title");
    const label = document.querySelector("#file-find-query-label");
    const input = this.queryEl();
    document.querySelectorAll("[data-file-find-mode]").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.fileFindMode === this.mode);
    });
    if (title) title.textContent = this.mode === "content" ? "Find in Files" : "Find File";
    if (label) label.textContent = this.mode === "content" ? "Text to find" : "Name";
    if (input) {
      input.placeholder = this.mode === "content" ? "Enter text to find…" : "Enter file name…";
    }
  },

  scheduleSearch(immediate = false) {
    clearTimeout(this.timer);
    const run = () => this.runSearch().catch(err => {
      const meta = document.querySelector("#file-find-meta");
      if (meta) meta.textContent = err.message || String(err);
    });
    if (immediate) {
      run();
      return;
    }
    this.timer = window.setTimeout(run, this.mode === "content" ? 280 : 160);
  },

  async runSearch() {
    const query = (this.queryEl()?.value || "").trim();
    const mask = (this.maskEl()?.value || "").trim();
    const results = document.querySelector("#file-find-results");
    const meta = document.querySelector("#file-find-meta");
    if (!results) return;

    if (!query && !(this.mode === "name" && mask)) {
      this.hits = [];
      this.selectedIndex = 0;
      results.innerHTML = `<div class="file-find-empty">${this.mode === "content" ? "Enter text to search in project files." : "Enter a file name or a file mask."}</div>`;
      if (meta) meta.textContent = "Type to search. Use ↑↓ and Enter.";
      return;
    }

    const seq = ++this.requestSeq;
    if (meta) meta.textContent = this.mode === "content" ? "Searching contents…" : "Searching files…";
    const payload = await api(
      `/api/project/search?project_id=${projectParam()}&q=${encodeURIComponent(query)}&mode=${encodeURIComponent(this.mode)}&mask=${encodeURIComponent(mask)}&limit=80`
    );
    if (seq !== this.requestSeq || !this.isOpen) return;

    this.hits = Array.isArray(payload.hits) ? payload.hits : [];
    this.selectedIndex = 0;
    if (meta) {
      const maskHint = mask ? ` · mask ${mask}` : "";
      meta.textContent = this.hits.length
        ? `${this.hits.length} result${this.hits.length === 1 ? "" : "s"}${maskHint}`
        : `No matches${maskHint}`;
    }
    this.renderHits();
  },

  renderHits() {
    const results = document.querySelector("#file-find-results");
    if (!results) return;
    results.innerHTML = "";
    if (!this.hits.length) {
      results.innerHTML = `<div class="file-find-empty">No matches found.</div>`;
      return;
    }
    this.hits.forEach((hit, index) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `file-find-hit${index === this.selectedIndex ? " selected" : ""}`;
      btn.setAttribute("role", "option");
      btn.dataset.index = String(index);
      const lineHint = hit.line ? `:${hit.line}` : "";
      btn.innerHTML = `
        <span class="file-find-hit-name">${escapeHtml(hit.name || hit.path || "")}${escapeHtml(lineHint)}</span>
        <span class="file-find-hit-path">${escapeHtml(hit.path || "")}</span>
        ${this.mode === "content" && hit.snippet ? `<span class="file-find-hit-snippet">${escapeHtml(hit.snippet)}</span>` : ""}
      `;
      btn.addEventListener("mouseenter", () => {
        this.selectedIndex = index;
        this.syncSelection();
      });
      btn.addEventListener("click", () => this.openHit(hit));
      results.appendChild(btn);
    });
    this.ensureSelectedVisible();
  },

  syncSelection() {
    const results = document.querySelector("#file-find-results");
    if (!results) return;
    results.querySelectorAll(".file-find-hit").forEach((el, index) => {
      el.classList.toggle("selected", index === this.selectedIndex);
    });
    this.ensureSelectedVisible();
  },

  ensureSelectedVisible() {
    const results = document.querySelector("#file-find-results");
    const selected = results?.querySelector(".file-find-hit.selected");
    selected?.scrollIntoView({ block: "nearest" });
  },

  navigate(direction) {
    if (!this.hits.length) return;
    const delta = direction === "down" ? 1 : -1;
    this.selectedIndex = (this.selectedIndex + delta + this.hits.length) % this.hits.length;
    this.syncSelection();
  },

  openSelected() {
    const hit = this.hits[this.selectedIndex];
    if (hit) this.openHit(hit);
  },

  openHit(hit) {
    if (!hit?.path) return;
    this.close();
    openProjectFile(hit.path).catch(showError);
    fileEditor.openFile(hit.path);
  },

  bindEvents() {
    on("#open-find-file", "click", () => this.open("name"));
    on("#open-find-in-files", "click", () => this.open("content"));
    document.querySelectorAll("[data-close-file-find]").forEach(el => {
      el.addEventListener("click", () => this.close());
    });
    document.querySelectorAll("[data-file-find-mode]").forEach(btn => {
      btn.addEventListener("click", () => this.setMode(btn.dataset.fileFindMode || "name"));
    });
    on("#file-find-query", "input", () => this.scheduleSearch());
    on("#file-find-mask", "input", () => this.scheduleSearch());
    document.addEventListener("keydown", event => {
      const isMod = event.metaKey || event.ctrlKey;
      const key = String(event.key || "").toLowerCase();

      if (isMod && event.shiftKey && (key === "o" || key === "n")) {
        event.preventDefault();
        this.toggle("name");
        return;
      }
      if (isMod && event.shiftKey && key === "f") {
        event.preventDefault();
        this.toggle("content");
        return;
      }
      if (!this.isOpen) return;

      if (event.key === "Escape") {
        event.preventDefault();
        this.close();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        this.navigate("down");
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        this.navigate("up");
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        this.openSelected();
      }
    });
  }
};

fileFind.bindEvents();
async function openProjectFile(path) {
  const payload = await api(`/api/project/file?project_id=${projectParam()}&path=${encodeURIComponent(path)}`);
  state.selectedFile = payload.path;
  setElementValue("#selected-file-path", payload.path);
  setElementValue("#file-preview", payload.readable === false ? (payload.message || "Preview unavailable for this file.") : payload.text);
  fillCodeFileFromSelection(false);
  syncCodeFileHint();
  if (payload.readable === false) showSnackbar(payload.message || "Preview unavailable for this file.", "info");
}
async function buildSelectedFileContext() {
  if (!state.selectedFile) throw new Error("Select a project file first.");
  const query = document.querySelector("#context-query")?.value || "selected file context";
  const payload = await api("/api/project/context", { method: "POST", body: JSON.stringify({ project_id: state.projectId, path: state.selectedFile, query }) });
  setTextContent("#context-output", payload.context);
}
async function loadGitDiff() {
  const path = state.selectedFile || "";
  const payload = await api(`/api/project/git-diff?project_id=${projectParam()}${path ? `&path=${encodeURIComponent(path)}` : ""}`);
  setTextContent("#context-output", payload.diff || payload.message || "No git diff.");
}
async function refreshWorkspace() {
  const search = await api(`/api/memory/search?query=${encodeURIComponent("favorite memory decision")}&project_id=${projectParam()}&limit=12`);
  renderResults(document.querySelector("#workspace-favorites"), search.hits.filter(hit => hit.node.metadata && hit.node.metadata.favorite).slice(0, 5));
}

async function loadMemoryLifecycle() {
  const container = document.querySelector("#memory-lifecycle-dashboard");
  if (!container) return;

  try {
    const payload = await api(`/api/analytics?project_id=${projectParam()}`);
    const life = payload.memory_lifecycle || {};
    const tier = life.by_tier || {};
    const stateCounts = life.by_state || {};
    const emb = payload.embeddings || {};
    const cov = emb.coverage || {};
    const indexed = Number(cov.indexed || 0);
    const active = Number(cov.active_nodes || 0);
    const missing = Number(cov.missing || Math.max(0, active - indexed));
    const pct = cov.coverage_pct != null ? Number(cov.coverage_pct) : (active ? Math.round((indexed / active) * 1000) / 10 : 0);
    const embLabel = emb.provider ? `${emb.provider}${emb.model ? ` / ${emb.model}` : ""}${emb.dimensions ? ` · ${emb.dimensions}d` : ""}` : "";

    const total = (tier.long_term || 0) + (tier.short_term || 0);
    const longTermPct = total > 0 ? Math.round((tier.long_term || 0) / total * 100) : 0;
    const shortTermPct = total > 0 ? Math.round((tier.short_term || 0) / total * 100) : 0;

    container.innerHTML = `
      <div style="margin-bottom: 16px;">
        <strong style="color: var(--ink); font-size: 14px;">Status Breakdown</strong>
        <div style="display: flex; gap: 10px; margin-top: 8px; flex-wrap: wrap;">
          <span class="badge">Fresh: ${stateCounts.fresh || 0}</span>
          <span class="badge">Stable: ${stateCounts.stable || 0}</span>
          <span class="badge warning">Stale: ${stateCounts.stale || 0}</span>
          <span class="badge">Archived: ${stateCounts.archived || 0}</span>
        </div>
      </div>
      <div style="margin-bottom: 16px;">
        <strong style="color: var(--ink); font-size: 14px;">Tier Distribution</strong>
        <div style="margin-top: 8px;">
          <div style="font-size: 13px; margin-bottom: 4px;">Long-term: ${tier.long_term || 0} (${longTermPct}%)</div>
          <div style="font-size: 13px;">Short-term: ${tier.short_term || 0} (${shortTermPct}%)</div>
        </div>
      </div>
      <div style="margin-bottom: 16px;">
        <strong style="color: var(--ink); font-size: 14px;">${t("memory.embeddingsCoverage")}</strong>
        <div style="margin-top: 4px; font-size: 24px; font-weight: 600; color: var(--accent);">${indexed}<span style="font-size: 14px; font-weight: 500; color: var(--muted);"> / ${active}</span></div>
        <div style="font-size: 13px; color: var(--muted); margin-top: 2px;">${pct}% · ${missing} missing${embLabel ? ` · ${escapeHtml(embLabel)}` : ""}</div>
      </div>
      <div>
        <strong style="color: var(--ink); font-size: 14px;">Total Memory Items</strong>
        <div style="margin-top: 4px; font-size: 24px; font-weight: 600; color: var(--accent);">${total}</div>
      </div>
    `;
    await loadMemoryLifecycleItems();
  } catch (error) {
    container.innerHTML = '<div style="color: var(--muted);">Failed to load lifecycle data</div>';
  }
}

async function loadMemoryLifecycleItems() {
  const container = document.querySelector("#memory-lifecycle-items");
  if (!container) return;
  const stateFilter = document.querySelector("#memory-lifecycle-state-filter")?.value || "all";
  const tierFilter = document.querySelector("#memory-lifecycle-tier-filter")?.value || "all";
  try {
    const payload = await api(`/api/memory/items?project_id=${projectParam()}&lifecycle_state=${encodeURIComponent(stateFilter)}&tier=${encodeURIComponent(tierFilter)}&limit=40`);
    const items = payload.items || [];
    const total = Number(payload.total || items.length);
    if (!items.length) {
      container.innerHTML = '<div class="memory-lifecycle-empty">No memory items match this lifecycle filter.</div>';
      return;
    }
    container.innerHTML = `
      <div class="memory-lifecycle-item-count">${items.length} shown · ${total} matched</div>
      ${items.map(renderMemoryLifecycleItem).join("")}
    `;
  } catch (error) {
    container.innerHTML = `<div class="memory-lifecycle-empty">${escapeHtml(error.message || "Failed to load lifecycle items")}</div>`;
  }
}

function renderMemoryLifecycleItem(item) {
  const meta = item.metadata || {};
  const stage = meta.lifecycle_state || "unknown";
  const tier = meta.memory_tier || "unknown";
  const score = meta.memory_score != null ? `<span class="candidate-chip subtle">score ${escapeHtml(String(meta.memory_score))}</span>` : "";
  const text = String(item.text || "").trim();
  const preview = text.length > 180 ? `${text.slice(0, 177)}…` : text;
  return `
    <article class="memory-lifecycle-item">
      <div class="memory-lifecycle-item-top">
        <strong title="${escapeHtml(item.label || "")}">${escapeHtml(item.label || "Memory item")}</strong>
        <span class="candidate-chip">${escapeHtml(stage)}</span>
      </div>
      <p>${escapeHtml(preview || "No preview.")}</p>
      <div class="candidate-card-chips">
        <span class="candidate-chip subtle">${escapeHtml(tier)}</span>
        <span class="candidate-chip subtle">${escapeHtml(item.status || "active")}</span>
        ${item.type ? `<span class="candidate-chip subtle">${escapeHtml(item.type)}</span>` : ""}
        ${score}
      </div>
    </article>`;
}

async function loadMemoryCandidates() {
  const container = document.querySelector("#candidate-list");
  if (!container) return;
  syncCandidateBatchActions();
  const filter = document.querySelector("#candidate-status-filter")?.value || "candidate";
  const listStatus = filter === "duplicate" ? "candidate" : filter;
  const payload = await api(`/api/memory/candidates?project_id=${projectParam()}&status=${encodeURIComponent(listStatus)}&limit=80`);
  let candidates = payload.candidates || [];
  if (filter === "duplicate") {
    candidates = candidates.filter(item => item.metadata && item.metadata.duplicate);
  }
  renderCandidateStatusStats(payload.counts || payload);
  const countEl = document.querySelector("#candidates-count");
  if (countEl) {
    const total = Number((payload.counts || {}).total ?? candidates.length) || candidates.length;
    const shown = candidates.length;
    if (filter === "all") {
      countEl.textContent = `${shown} shown · ${total} total`;
    } else if (shown < total && (filter === "candidate" || filter === "promoted" || filter === "rejected")) {
      const label = filter === "candidate" ? "pending" : filter === "promoted" ? "accepted" : "rejected";
      const scoped = Number((payload.counts || {})[filter === "candidate" ? "pending" : filter === "promoted" ? "accepted" : "rejected"] || shown);
      countEl.textContent = `${shown} shown · ${scoped} ${label}`;
    } else {
      const noun = shown === 1 ? "item" : "items";
      countEl.textContent = `${shown} ${noun}`;
    }
  }
  if (!candidates.length) {
    container.innerHTML = `
      <div class="candidate-empty">
        <strong>Nothing to review here</strong>
        <p>Run AutoScan, or switch the filter to see accepted / rejected items.</p>
      </div>`;
    return;
  }
  container.innerHTML = "";
  for (const candidate of candidates) {
    container.appendChild(renderCandidateCard(candidate));
  }
  container.querySelectorAll("[data-promote-candidate]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    await api(`/api/memory/candidates/${button.dataset.promoteCandidate}/promote`, { method: "POST", body: "{}" });
    await loadMemoryCandidates();
    await loadMemoryLifecycle();
    await loadAnalytics();
    await runSearch(document.querySelector("#search-query").value || "memory");
    await refreshWorkspace();
    scheduleGraphLoad();
  }));
  container.querySelectorAll("[data-reject-candidate]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    await api(`/api/memory/candidates/${button.dataset.rejectCandidate}/reject`, { method: "POST", body: JSON.stringify({ reason: "Rejected in Memory UI" }) });
    await refreshMemorySurfaces();
  }));
}

function renderCandidateStatusStats(counts) {
  const root = document.querySelector("#candidates-status-stats");
  if (!root) return;
  const pending = Number(counts?.pending || 0);
  const accepted = Number(counts?.accepted || 0);
  const rejected = Number(counts?.rejected || 0);
  const duplicate = Number(counts?.duplicate || 0);
  const set = (id, value) => {
    const el = document.querySelector(id);
    if (el) el.textContent = String(value);
  };
  set("#candidates-stat-pending", pending);
  set("#candidates-stat-accepted", accepted);
  set("#candidates-stat-rejected", rejected);
  set("#candidates-stat-duplicate", duplicate);
  root.querySelectorAll(".candidates-stat").forEach(el => {
    const key = el.dataset.stat;
    const value = key === "pending" ? pending : key === "accepted" ? accepted : key === "rejected" ? rejected : duplicate;
    el.classList.toggle("is-zero", value === 0);
    el.classList.toggle("is-active", value > 0);
  });
}

function candidateSourceLabel(sourceType) {
  const map = {
    docs: "Repo docs",
    code: "Code",
    chat: "App chat",
    git: "Git",
    adr: "ADR",
    issues: "Issue files",
    prs: "PR notes",
    meetings: "Meeting notes",
    granola: "Granola",
    "azure-boards": "Azure Boards",
    "azure-git": "Azure Git",
    "azure-wiki": "Azure Wiki",
    "teams-meetings": "Teams",
  };
  return map[String(sourceType || "").toLowerCase()] || String(sourceType || "Memory");
}

function candidateIconLetter(sourceType) {
  const label = candidateSourceLabel(sourceType);
  return (label.trim().charAt(0) || "M").toUpperCase();
}

function candidateTitle(candidate) {
  const label = String(candidate.label || "").trim();
  const cleaned = label.replace(/^(Code|Doc|Docs|ADR|Issue|PR|Meeting|Git|Chat|Wiki|Teams)\s*:\s*/i, "").trim();
  const path = String(candidate.source_ref || (candidate.metadata || {}).path || cleaned);
  const base = path.split(/[\\/]/).filter(Boolean).pop() || cleaned || "Memory item";
  return base;
}

function candidatePath(candidate) {
  const meta = candidate.metadata || {};
  const ref = String(candidate.source_ref || meta.path || meta.relative_path || "").trim();
  if (!ref) return "";
  const title = candidateTitle(candidate);
  if (ref === title) return "";
  return ref.length > 72 ? `…${ref.slice(-70)}` : ref;
}

function candidateWhy(candidate) {
  const meta = candidate.metadata || {};
  const source = String(candidate.source_type || meta.source_type || "").toLowerCase();
  if (meta.duplicate) {
    const similar = meta.duplicate_label ? ` Similar to “${meta.duplicate_label}”.` : "";
    return `Possible duplicate.${similar}`;
  }
  const reasons = {
    code: "Found in project source files.",
    docs: "Found in local repository docs.",
    git: "Extracted from recent git history.",
    chat: "Captured from ArchitectOS chat.",
    adr: "Architecture decision record.",
    issues: "Local issue/bug markdown file.",
    prs: "Local PR notes or template.",
    meetings: "Local meeting notes file.",
    granola: "Imported from Granola.",
    "azure-boards": "Azure Boards work item.",
    "azure-git": "Azure Repos repository or pull request.",
    "azure-wiki": "Azure Wiki page.",
    "teams-meetings": "Teams transcript or AI insights.",
  };
  return reasons[source] || "Suggested by AutoScan for durable memory.";
}

function candidatePreviewText(candidate) {
  let text = String(candidate.text || "").trim();
  text = text
    .replace(/^File\s+.+\s+imported into ArchitectOS memory\.?\s*/i, "")
    .replace(/^Code:\s*.+\n+/i, "")
    .replace(/\r/g, "");
  const lines = text.split("\n").map(line => line.trimEnd()).filter(line => line.trim());
  const useful = lines.filter(line => {
    const trimmed = line.trim();
    if (!trimmed) return false;
    if (/^package\s+/.test(trimmed)) return false;
    if (/^import\s+/.test(trimmed)) return false;
    if (/^\/\//.test(trimmed) && trimmed.length < 40) return false;
    if (/^\*\s*Copyright/i.test(trimmed)) return false;
    if (/^#\s*!/.test(trimmed)) return false;
    return true;
  });
  const preview = (useful.length ? useful : lines).slice(0, 4).join("\n").trim();
  if (!preview) return "No readable preview.";
  return preview.length > 220 ? `${preview.slice(0, 217)}…` : preview;
}

function renderCandidateCard(candidate) {
  const el = document.createElement("article");
  const meta = candidate.metadata || {};
  const sourceType = String(candidate.source_type || meta.source_type || "manual");
  const isDuplicate = Boolean(meta.duplicate);
  const canPromote = candidate.status !== "promoted";
  const canReject = candidate.status === "candidate";
  const status = String(candidate.status || "candidate");
  el.className = `candidate-card${isDuplicate ? " is-duplicate" : ""}`;
  const path = candidatePath(candidate);
  const fullText = String(candidate.text || "").trim();
  el.innerHTML = `
    <div class="candidate-card-layout">
      <div class="candidate-card-icon" data-source="${escapeHtml(sourceType)}" aria-hidden="true">${escapeHtml(candidateIconLetter(sourceType))}</div>
      <div class="candidate-card-body">
        <div class="candidate-card-top">
          <div class="candidate-card-heading">
            <h5 title="${escapeHtml(candidate.label || "")}">${escapeHtml(candidateTitle(candidate))}</h5>
            <div class="candidate-card-chips">
              <span class="candidate-chip">${escapeHtml(candidateSourceLabel(sourceType))}</span>
              ${candidate.type ? `<span class="candidate-chip subtle">${escapeHtml(candidate.type)}</span>` : ""}
              ${status !== "candidate" ? `<span class="candidate-chip subtle">${escapeHtml(status === "promoted" ? "accepted" : status)}</span>` : ""}
              ${isDuplicate ? `<span class="candidate-chip warning">duplicate</span>` : ""}
            </div>
          </div>
          <div class="candidate-card-actions">
            ${canPromote ? `<button data-promote-candidate="${escapeHtml(candidate.id)}" type="button" class="candidate-action candidate-action-accept">Accept</button>` : ""}
            ${canReject ? `<button data-reject-candidate="${escapeHtml(candidate.id)}" type="button" class="candidate-action candidate-action-reject">Reject</button>` : ""}
          </div>
        </div>
        ${path ? `<p class="candidate-card-path" title="${escapeHtml(candidate.source_ref || path)}">${escapeHtml(path)}</p>` : ""}
        <p class="candidate-card-why">${escapeHtml(candidateWhy(candidate))}</p>
        <pre class="candidate-card-preview">${escapeHtml(candidatePreviewText(candidate))}</pre>
        ${fullText.length > 220 ? `<details class="candidate-card-more"><summary>View full text</summary><pre>${escapeHtml(fullText)}</pre></details>` : ""}
      </div>
    </div>`;
  return el;
}

async function batchUpdateCandidates(body) {
  closeCandidatesMoreMenu();
  const summary = document.querySelector("#candidate-batch-summary") || document.querySelector("#candidate-summary");
  const acceptBtn = document.querySelector("#candidates-accept-filtered");
  const rejectBtn = document.querySelector("#candidates-reject-filtered");
  const pendingHint = Number(document.querySelector("#candidates-stat-pending")?.textContent || 0);
  if (summary) {
    summary.className = "provider-test candidates-batch-summary";
    summary.hidden = false;
    summary.textContent = pendingHint > 200
      ? `Updating full queue (${pendingHint.toLocaleString()} items)… this can take a while`
      : "Updating full queue…";
  }
  if (acceptBtn) acceptBtn.disabled = true;
  if (rejectBtn) rejectBtn.disabled = true;
  let payload;
  try {
    payload = await api("/api/memory/candidates/batch", {
      method: "POST",
      body: JSON.stringify({ project_id: state.projectId, all: true, ...body })
    });
  } finally {
    syncCandidateBatchActions();
  }
  if (summary) {
    const errCount = payload.error_count || (payload.errors && payload.errors.length) || 0;
    summary.className = errCount ? "provider-test error candidates-batch-summary" : "provider-test ok candidates-batch-summary";
    const parts = [
      payload.action === "promote" ? `Accepted ${payload.promoted || 0}` : `Rejected ${payload.rejected || 0}`,
      payload.matched != null ? `matched ${payload.matched}` : "",
      payload.skipped ? `skipped ${payload.skipped}` : "",
      payload.counts ? `pending now ${payload.counts.pending ?? payload.pending ?? 0}` : "",
      errCount ? `errors ${errCount}` : ""
    ].filter(Boolean);
    summary.textContent = parts.join(" · ");
  }
  if (payload.counts) renderCandidateStatusStats(payload.counts);
  await refreshMemorySurfaces();
  if (payload.promoted) {
    await runSearch(document.querySelector("#search-query").value || "memory");
    await refreshWorkspace();
    scheduleGraphLoad();
  }
  return payload;
}

function candidateBatchFilterFromUi() {
  const filter = document.querySelector("#candidate-status-filter")?.value || "candidate";
  if (filter === "duplicate") return { status: "duplicate", duplicate_only: true };
  if (filter === "all") return { status: "all" };
  return { status: filter };
}

function syncCandidateBatchActions() {
  const filter = document.querySelector("#candidate-status-filter")?.value || "candidate";
  const acceptBtn = document.querySelector("#candidates-accept-filtered");
  const rejectBtn = document.querySelector("#candidates-reject-filtered");
  const labels = {
    candidate: { accept: "Accept all", reject: "Reject all" },
    duplicate: { accept: "Accept duplicates", reject: "Reject duplicates" },
    rejected: { accept: "Accept rejected", reject: "Reject again" },
    promoted: { accept: "Accept promoted", reject: "Reject promoted" },
    all: { accept: "Accept all", reject: "Reject all" },
  };
  const pair = labels[filter] || labels.candidate;
  if (acceptBtn) {
    acceptBtn.textContent = pair.accept;
    acceptBtn.disabled = filter === "promoted";
    acceptBtn.title = filter === "promoted"
      ? "Already accepted items stay in memory"
      : `Accept the entire ${filter === "candidate" ? "pending" : filter} queue (not only the ${document.querySelector("#candidates-count")?.textContent || "shown"} list)`;
  }
  if (rejectBtn) {
    rejectBtn.textContent = pair.reject;
    rejectBtn.disabled = filter === "promoted";
    rejectBtn.title = filter === "promoted"
      ? "Accepted memory is not removed by batch reject"
      : `Reject the entire ${filter === "candidate" ? "pending" : filter} queue (not only the shown list)`;
  }
}

function closeCandidatesMoreMenu() {
  document.querySelectorAll(".candidates-more[open]").forEach(item => {
    item.open = false;
  });
}
function renderMemoryFiles() {
  const list = document.querySelector("#memory-file-list");
  if (!list) return;

  if (!state.memoryFiles.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">📁</div>
        <div class="empty-state-title">No files selected</div>
        <div class="empty-state-text">Choose text, docs, or code files to write into memory.</div>
      </div>
    `;
    return;
  }

  list.innerHTML = "";
  for (const file of state.memoryFiles) {
    const el = document.createElement("article");
    el.className = "result";
    el.innerHTML = `<div class="row"><strong>${escapeHtml(file.name)}</strong><button data-remove-memory-file="${escapeHtml(file.id)}" type="button">Remove</button></div><p>${escapeHtml(formatBytes(file.size))}${file.text_extracted ? ` · ${escapeHtml(String(file.chars || 0))} chars` : " · no text extracted"}</p><span class="badge">${escapeHtml(file.mime || "file")}</span>`;
    list.appendChild(el);
  }
  list.querySelectorAll("[data-remove-memory-file]").forEach(button => button.addEventListener("click", () => {
    state.memoryFiles = state.memoryFiles.filter(file => file.id !== button.dataset.removeMemoryFile);
    renderMemoryFiles();
  }));
}
async function handleMemoryFileSelect(fileList) {
  const summary = document.querySelector("#memory-file-summary");
  const files = Array.from(fileList || []);
  if (!files.length) return;
  if (summary) { summary.className = "provider-test"; summary.textContent = "uploading files..."; }
  for (const file of files) {
    const content = await readFileAsDataUrl(file);
    const result = await api("/api/files", { method: "POST", body: JSON.stringify({ project_id: state.projectId, name: file.name, content }) });
    if (result.file) state.memoryFiles.push(result.file);
  }
  renderMemoryFiles();
  if (summary) { summary.className = "provider-test ok"; summary.textContent = `${state.memoryFiles.length} file(s) ready`; }
}
async function importMemoryFiles() {
  const summary = document.querySelector("#memory-file-summary");
  if (!state.memoryFiles.length) throw new Error("Choose at least one file first.");
  if (summary) { summary.className = "provider-test"; summary.textContent = "writing files to memory..."; }
  const payload = await api("/api/memory/files", {
    method: "POST",
    body: JSON.stringify({
      project_id: state.projectId,
      file_ids: state.memoryFiles.map(file => file.id),
      type: document.querySelector("#memory-file-type")?.value || "Artifact",
      scope: document.querySelector("#memory-file-scope")?.value || "project",
    }),
  });
  state.memoryFiles = [];
  renderMemoryFiles();
  if (summary) {
    summary.className = payload.skipped && payload.skipped.length ? "provider-test error" : "provider-test ok";
    summary.textContent = `${payload.count} file memory item(s) added${payload.skipped && payload.skipped.length ? `, ${payload.skipped.length} skipped` : ""}`;
  }
  await runSearch("file memory");
  await refreshWorkspace();
  scheduleGraphLoad();
}
let voiceRecognition = null;
let voiceListening = false;
function voiceRecognitionLanguage() {
  return ({ en: "en-US", ru: "ru-RU", uk: "uk-UA", he: "he-IL" })[state.language] || "en-US";
}
function voiceStatus(message, tone = "") {
  const status = document.querySelector("#voice-status");
  if (!status) return;
  status.className = `provider-test ${tone}`.trim();
  status.textContent = message;
}
function setVoiceListening(listening) {
  voiceListening = listening;
  const start = document.querySelector("#voice-start");
  const stop = document.querySelector("#voice-stop");
  if (start) start.disabled = listening;
  if (stop) stop.disabled = !listening;
}
function speechRecognitionCtor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}
function autoVoiceLabel(text) {
  return (text || "")
    .trim()
    .split(/\s+/)
    .slice(0, 8)
    .join(" ")
    .replace(/[.,;:!?]+$/, "");
}
function appendVoiceTranscript(text) {
  const transcript = document.querySelector("#voice-memory-text");
  const label = document.querySelector("#voice-memory-label");
  if (!transcript || !text) return;
  const spacer = transcript.value.trim() ? " " : "";
  transcript.value = `${transcript.value.trim()}${spacer}${text.trim()}`.trim();
  if (label && !label.value.trim()) label.value = autoVoiceLabel(transcript.value);
}
function ensureVoiceRecognition() {
  const Recognition = speechRecognitionCtor();
  if (!Recognition) return null;
  if (voiceRecognition) return voiceRecognition;
  voiceRecognition = new Recognition();
  voiceRecognition.continuous = true;
  voiceRecognition.interimResults = true;
  voiceRecognition.onstart = () => {
    setVoiceListening(true);
    voiceStatus("Listening...", "ok");
  };
  voiceRecognition.onend = () => {
    setVoiceListening(false);
    voiceStatus("Stopped");
  };
  voiceRecognition.onerror = event => {
    setVoiceListening(false);
    voiceStatus(`Voice error: ${event.error || "unknown"}`, "error");
  };
  voiceRecognition.onresult = event => {
    let finalText = "";
    let interimText = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const text = event.results[index][0].transcript || "";
      if (event.results[index].isFinal) finalText += `${text} `;
      else interimText += text;
    }
    if (finalText.trim()) appendVoiceTranscript(finalText);
    voiceStatus(interimText.trim() ? `Listening: ${interimText.trim()}` : "Listening...", "ok");
  };
  return voiceRecognition;
}
function startVoiceMemory() {
  const recognition = ensureVoiceRecognition();
  if (!recognition) {
    voiceStatus("Voice input is not supported in this browser.", "error");
    const start = document.querySelector("#voice-start");
    if (start) start.disabled = true;
    return;
  }
  if (voiceListening) return;
  recognition.lang = voiceRecognitionLanguage();
  try {
    recognition.start();
  } catch (error) {
    voiceStatus(error.message || "Could not start voice input.", "error");
  }
}
function stopVoiceMemory() {
  if (voiceRecognition && voiceListening) voiceRecognition.stop();
}
function clearVoiceMemory() {
  setElementValue("#voice-memory-label", "");
  setElementValue("#voice-memory-text", "");
  voiceStatus(speechRecognitionCtor() ? "Ready" : "Voice input is not supported in this browser.", speechRecognitionCtor() ? "" : "error");
}
async function saveVoiceMemory(event) {
  event.preventDefault();
  const text = (document.querySelector("#voice-memory-text")?.value || "").trim();
  const labelInput = document.querySelector("#voice-memory-label");
  if (!text) throw new Error("Record or type a transcript first.");
  const label = (labelInput?.value || "").trim() || autoVoiceLabel(text) || "Voice memory";
  const payload = {
    project_id: state.projectId,
    label,
    type: document.querySelector("#voice-memory-type")?.value || "Note",
    scope: document.querySelector("#voice-memory-scope")?.value || "project",
    text,
  };
  voiceStatus("Saving voice memory...");
  await api("/api/memory", { method: "POST", body: JSON.stringify(payload) });
  document.querySelector("#voice-memory-form")?.reset();
  voiceStatus("Voice memory saved", "ok");
  await refreshWorkspace();
  await runSearch("voice memory");
  scheduleGraphLoad();
}
function initVoiceMemory() {
  const start = document.querySelector("#voice-start");
  if (!start) return;
  on(start, "click", startVoiceMemory);
  on("#voice-stop", "click", stopVoiceMemory);
  on("#voice-clear", "click", clearVoiceMemory);
  on("#voice-memory-form", "submit", event => saveVoiceMemory(event).catch(showError));
  if (!speechRecognitionCtor()) {
    start.disabled = true;
    voiceStatus("Voice input is not supported in this browser.", "error");
  }
}
function ingestTimeoutPayload(sources = []) {
  const itemTimeout = Number(document.querySelector("#ingest-item-timeout")?.value || 25);
  const sourceTimeout = Number(document.querySelector("#ingest-source-timeout")?.value || 600);
  const timeouts = {};
  for (const source of sources) {
    timeouts[source] = { item: itemTimeout, source: sourceTimeout };
  }
  return {
    item_timeout: itemTimeout,
    source_timeout: sourceTimeout,
    timeouts,
  };
}

async function ingestMemorySources() {
  const summary = document.querySelector("#candidate-summary");
  const sources = [...document.querySelectorAll(".ingest-source")].filter(input => input.checked).map(input => input.value);
  const limit = Number(document.querySelector("#ingest-limit")?.value || 12);
  const ingestMode = document.querySelector("#ingest-direct-memory")?.checked ? "memory" : "candidates";
  const allItemsEl = document.querySelector("#ingest-boards-all-items");
  const allItems = allItemsEl ? allItemsEl.checked : false;
  const boardsTypes = selectedBoardsTypes();
  if (!sources.length) {
    if (summary) { summary.className = "provider-test error"; summary.textContent = "Select at least one source."; }
    return;
  }
  if (summary) { summary.className = "provider-test"; summary.textContent = "ingesting..."; }
  showIngestProgress(true);
  renderIngestProgress({
    running: true,
    current: "queued",
    sources,
    project_id: state.projectId,
    logs: [{ level: "info", message: `Starting AutoScan for ${sources.length} source group(s) · ${ingestMode === "memory" ? "direct memory" : "review candidates"}` }],
  });
  try {
    const scheduled = await api("/api/memory/ingest", {
      method: "POST",
      body: JSON.stringify({
        project_id: state.projectId,
        sources,
        limit,
        async: true,
        all_items: allItems,
        work_item_types: boardsTypes,
        ingest_mode: ingestMode,
        ...ingestTimeoutPayload(sources),
      })
    });
    if (!scheduled.scheduled && scheduled.reason === "already_running") {
      if (summary) { summary.className = "provider-test"; summary.textContent = "Ingest already running..."; }
    }
    const status = await waitForMemoryIngest(summary);
    if (status?.error) return;
    await refreshMemorySurfaces();
    scheduleGraphLoad();
  } catch (error) {
    if (summary) { summary.className = "provider-test error"; summary.textContent = error.message; }
    renderIngestProgress({ running: false, current: "error", error: error.message, logs: [{ level: "error", message: error.message }] });
  }
}

function showIngestProgress(visible) {
  const panel = document.querySelector("#ingest-progress");
  if (panel) panel.hidden = !visible;
}

const INGEST_STEP_META = {
  queued: { title: "Queued", mark: "…" },
  starting: { title: "Starting", mark: "1" },
  files: { title: "Project files", mark: "F" },
  chat: { title: "App chat", mark: "C" },
  git: { title: "Git history", mark: "G" },
  granola: { title: "Granola meetings", mark: "N" },
  "azure-boards": { title: "Azure Boards", mark: "B" },
  "azure-git": { title: "Azure Git", mark: "R" },
  "azure-wiki": { title: "Azure Wiki", mark: "W" },
  "teams-meetings": { title: "Teams meetings", mark: "T" },
  prepare: { title: "Prepare review queue", mark: "P" },
  done: { title: "Finished", mark: "✓" },
  error: { title: "Failed", mark: "!" },
};

function ingestStepTitle(key) {
  if (INGEST_STEP_META[key]) return INGEST_STEP_META[key].title;
  if (String(key).startsWith("project:")) return "Project scan";
  return String(key || "Step").replace(/-/g, " ");
}

function ingestStepMark(key, state) {
  if (state === "done") return "✓";
  if (state === "error") return "!";
  if (state === "warn") return "!";
  if (state === "running") return "●";
  return (INGEST_STEP_META[key] && INGEST_STEP_META[key].mark) || "·";
}

function friendlyIngestMessage(entry) {
  const message = String(entry?.message || "").trim();
  if (!message) return "Waiting…";
  return message
    .replace(/^Starting ingest · /, "Starting · ")
    .replace(/^Scanning project files for /, "Looking through ")
    .replace(/^File scan done · /, "Found ")
    .replace(/^Reading chat history…$/, "Reading recent chats…")
    .replace(/^Chat done · /, "Chat: ")
    .replace(/^Scanning git history…$/, "Reading git commits…")
    .replace(/^Git done · /, "Git: ")
    .replace(/^Calling Granola MCP…$/, "Asking Granola for meetings…")
    .replace(/^Granola done · /, "Granola: ")
    .replace(/^Importing Azure Boards work items…$/, "Fetching Azure Boards work items…")
    .replace(/^Azure Boards done · /, "Boards: ")
    .replace(/^Importing Azure Git repos and pull requests…$/, "Fetching Azure Git repos and PRs…")
    .replace(/^Azure Git done · /, "Azure Git: ")
    .replace(/^Importing Azure Wiki pages…$/, "Fetching Azure Wiki pages…")
    .replace(/^Azure Wiki done · /, "Wiki: ")
    .replace(/^Importing Teams meetings \(Graph transcripts \/ AI Insights\)…$/, "Fetching Teams transcripts & insights…")
    .replace(/^Teams done · /, "Teams: ")
    .replace(/^Preparing review queue from /, "Building review queue from ")
    .replace(/^Ingest finished · /, "Done · ");
}

function buildIngestSteps(status) {
  const sources = Array.isArray(status.sources) ? status.sources.slice() : [];
  const fileGroup = ["docs", "code", "adr", "issues", "prs", "meetings"];
  const steps = [];
  const hasFiles = sources.some(item => fileGroup.includes(item));
  if (hasFiles) steps.push("files");
  for (const source of ["chat", "git", "granola", "azure-boards", "azure-git", "azure-wiki", "teams-meetings"]) {
    if (sources.includes(source)) steps.push(source);
  }
  if (!steps.length) steps.push("starting");
  steps.push("prepare");
  return steps;
}

function inferIngestStepStates(status) {
  const steps = buildIngestSteps(status);
  const logs = status.logs || [];
  const current = String(status.current || "");
  const states = {};
  const summaries = {};
  for (const key of steps) {
    states[key] = "pending";
    summaries[key] = "Waiting…";
  }
  for (const entry of logs) {
    let key = entry.source || "";
    const msg = String(entry.message || "");
    if (!key) {
      if (/prepare|review queue/i.test(msg)) key = "prepare";
      else if (/starting ingest|queued ingest/i.test(msg)) key = steps[0];
      else if (/finished/i.test(msg)) key = "prepare";
    }
    if (key === "docs" || key === "code" || key === "adr" || key === "issues" || key === "prs" || key === "meetings") key = "files";
    if (!steps.includes(key)) continue;
    const level = entry.level || "info";
    if (level === "error") states[key] = "error";
    else if (level === "warn") states[key] = states[key] === "error" ? "error" : "warn";
    else if (/done|finished|found |candidate|written/i.test(msg)) states[key] = states[key] === "error" ? "error" : (states[key] === "warn" ? "warn" : "done");
    else if (/scanning|reading|calling|importing|fetching|preparing|starting|looking/i.test(msg) || /…$/.test(msg)) {
      if (states[key] === "pending") states[key] = "running";
    }
    summaries[key] = friendlyIngestMessage(entry);
  }
  if (status.running && current) {
    let active = current;
    if (active.startsWith("project:")) active = steps[0] || "files";
    if (active === "starting" || active === "queued" || active === "done") active = steps.find(key => states[key] === "pending" || states[key] === "running") || steps[steps.length - 1];
    if (steps.includes(active) && states[active] !== "done" && states[active] !== "error" && states[active] !== "warn") {
      states[active] = "running";
    }
    // Mark earlier steps done if we've moved past them.
    const idx = steps.indexOf(active);
    if (idx > 0) {
      for (let i = 0; i < idx; i += 1) {
        if (states[steps[i]] === "pending" || states[steps[i]] === "running") states[steps[i]] = "done";
      }
    }
  }
  if (!status.running && !status.error) {
    for (const key of steps) {
      if (states[key] === "pending" || states[key] === "running") states[key] = "done";
    }
  }
  if (status.error) {
    const active = steps.find(key => states[key] === "running" || states[key] === "pending") || steps[steps.length - 1];
    states[active] = "error";
    summaries[active] = status.error;
  }
  return { steps, states, summaries };
}

function renderIngestProgress(status) {
  const panel = document.querySelector("#ingest-progress");
  const statusEl = document.querySelector("#ingest-agent-status");
  const stepsEl = document.querySelector("#ingest-agent-steps");
  const barFill = document.querySelector("#ingest-agent-bar-fill");
  const countEl = document.querySelector("#ingest-agent-count");
  const latestEl = document.querySelector("#ingest-agent-latest");
  const feedEl = document.querySelector("#ingest-agent-feed");
  if (!panel || !statusEl || !stepsEl) return;
  panel.hidden = false;

  const { steps, states, summaries } = inferIngestStepStates(status);
  const doneCount = steps.filter(key => ["done", "warn"].includes(states[key])).length;
  const total = steps.length || 1;
  const pct = status.running ? Math.round((doneCount / total) * 100) : (status.error ? Math.round((doneCount / total) * 100) : 100);

  if (status.running) {
    statusEl.textContent = `Working on ${ingestStepTitle(status.current || steps.find(key => states[key] === "running") || "scan")}…`;
    statusEl.dataset.tone = "running";
  } else if (status.error) {
    statusEl.textContent = "Stopped with an error";
    statusEl.dataset.tone = "error";
  } else {
    statusEl.textContent = "Finished";
    statusEl.dataset.tone = "ok";
  }

  if (barFill) barFill.style.width = `${Math.max(status.running ? 8 : 0, pct)}%`;
  if (countEl) countEl.textContent = `${Math.min(doneCount + (status.running ? 1 : 0), total)} / ${total}`;

  stepsEl.innerHTML = steps.map(key => {
    const state = states[key] || "pending";
    const badge = state === "running" ? "now" : state === "done" ? "done" : state === "warn" ? "skipped" : state === "error" ? "error" : "queued";
    return `<div class="ingest-agent-step" data-state="${escapeHtml(state)}" role="listitem">
      <span class="ingest-agent-step-mark">${escapeHtml(ingestStepMark(key, state))}</span>
      <div class="ingest-agent-step-body">
        <strong>${escapeHtml(ingestStepTitle(key))}</strong>
        <p>${escapeHtml(summaries[key] || "Waiting…")}</p>
      </div>
      <span class="ingest-agent-step-badge">${escapeHtml(badge)}</span>
    </div>`;
  }).join("");

  const latest = (status.logs || []).slice().reverse().find(Boolean);
  if (latestEl) latestEl.textContent = latest ? friendlyIngestMessage(latest) : "";

  if (feedEl) {
    feedEl.textContent = (status.logs || []).map(entry => {
      const level = (entry.level || "info").toUpperCase();
      const source = entry.source ? `[${entry.source}] ` : "";
      return `${level} ${source}${entry.message || ""}`;
    }).join("\n");
    feedEl.scrollTop = feedEl.scrollHeight;
  }
}

async function waitForMemoryIngest(summary, attempts = 180) {
  for (let i = 0; i < attempts; i += 1) {
    const status = await api("/api/memory/ingest");
    renderIngestProgress(status);
    if (status.running) {
      if (summary) {
        summary.className = "provider-test";
        summary.textContent = `AutoScan working (${ingestStepTitle(status.current || "scan")})…`;
      }
      await new Promise(resolve => setTimeout(resolve, 400));
      continue;
    }
    if (status.error) {
      if (summary) { summary.className = "provider-test error"; summary.textContent = status.error; }
      return status;
    }
    const result = status.result || {};
    const warnings = result.warnings && result.warnings.length ? `; ${result.warnings.join("; ")}` : "";
    const boards = result.boards_count ? `, ${result.boards_count} Boards→memory` : "";
    const azureGit = result.azure_git_count ? `, ${result.azure_git_count} Azure Git→memory` : "";
    const wiki = result.wiki_count ? `, ${result.wiki_count} Wiki→memory` : "";
    const teams = result.teams_count ? `, ${result.teams_count} Teams→memory` : "";
    const direct = result.direct_count ? `, ${result.direct_count} direct→memory` : "";
    if (summary) {
      summary.className = "provider-test ok";
      summary.textContent = `${result.count || 0} candidate(s), ${result.duplicates || 0} duplicate hint(s), ${result.pending || 0} pending${direct}${boards}${azureGit}${wiki}${teams}${warnings}`;
    }
    return status;
  }
  if (summary) {
    summary.className = "provider-test";
    summary.textContent = "Ingest is still running in the background.";
  }
  return null;
}
async function rescanAllMemorySources() {
  const summary = document.querySelector("#candidate-summary");
  const limit = Number(document.querySelector("#ingest-limit")?.value || 24);
  const ingestMode = document.querySelector("#ingest-direct-memory")?.checked ? "memory" : "candidates";
  const allItemsEl = document.querySelector("#ingest-boards-all-items");
  const allItems = allItemsEl ? allItemsEl.checked : false;
  const boardsTypes = selectedBoardsTypes();
  setIngestSourcesSelected(true);
  if (summary) { summary.className = "provider-test"; summary.textContent = "rescanning all sources..."; }
  try {
    const scheduled = await api("/api/memory/rescan", {
      method: "POST",
      body: JSON.stringify({
        trigger: "manual",
        project_id: state.projectId,
        all_projects: false,
        sources: "all",
        limit,
        include_mcp: true,
        all_items: allItems,
        work_item_types: boardsTypes,
        ingest_mode: ingestMode,
        ...ingestTimeoutPayload([
          "docs", "code", "chat", "git", "adr", "issues", "prs", "meetings",
          "granola", "azure-boards", "azure-git", "azure-wiki", "teams-meetings",
        ]),
      })
    });
    if (!scheduled.scheduled && scheduled.reason === "already_running") {
      if (summary) { summary.className = "provider-test"; summary.textContent = "Rescan already running..."; }
    }
    await waitForMemoryRescan(summary);
  } catch (error) {
    if (summary) { summary.className = "provider-test error"; summary.textContent = error.message; }
  }
}
async function waitForMemoryRescan(summary, attempts = 120) {
  showIngestProgress(true);
  for (let i = 0; i < attempts; i += 1) {
    const [status, ingest] = await Promise.all([
      api("/api/memory/rescan"),
      api("/api/memory/ingest").catch(() => null),
    ]);
    if (ingest) renderIngestProgress(ingest);
    if (status.running) {
      if (summary) {
        summary.className = "provider-test";
        summary.textContent = `rescanning (${status.trigger || "manual"})...`;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
      continue;
    }
    if (status.error) {
      if (summary) { summary.className = "provider-test error"; summary.textContent = status.error; }
      return status;
    }
    const result = status.result || {};
    const warnings = result.warnings && result.warnings.length ? `; ${result.warnings.join("; ")}` : "";
    const boards = result.boards_count ? `, ${result.boards_count} Boards→memory` : "";
    const azureGit = result.azure_git_count ? `, ${result.azure_git_count} Azure Git→memory` : "";
    const wiki = result.wiki_count ? `, ${result.wiki_count} Wiki→memory` : "";
    const teams = result.teams_count ? `, ${result.teams_count} Teams→memory` : "";
    const direct = result.memory_written ? `, ${result.memory_written} memory write(s)` : "";
    if (summary) {
      summary.className = "provider-test ok";
      summary.textContent = `${result.count || 0} candidate(s), ${result.duplicates || 0} duplicate hint(s)${direct}${boards}${azureGit}${wiki}${teams}${warnings}`;
    }
    await refreshMemorySurfaces();
    scheduleGraphLoad();
    return status;
  }
  if (summary) {
    summary.className = "provider-test";
    summary.textContent = "Rescan is still running in the background. Refresh candidates later.";
  }
  return null;
}

async function refreshMemorySurfaces() {
  await Promise.all([
    loadMemoryCandidates(),
    loadMemoryLifecycle(),
    loadAnalytics(),
  ]);
}
async function syncStartupMemoryRescan() {
  try {
    const status = await api("/api/memory/rescan");
    if (!status.running && !status.result && !status.error) return;
    const summary = document.querySelector("#candidate-summary");
    if (status.running) {
      await waitForMemoryRescan(summary);
      return;
    }
    if (status.result && summary) {
      summary.className = "provider-test ok";
      summary.textContent = `Startup rescan: ${status.result.count || 0} candidate(s)`;
      await refreshMemorySurfaces();
    }
  } catch (_error) {
    // Startup rescan is best-effort; UI stays usable if it fails.
  }
}
function setIngestSourcesSelected(selected) {
  document.querySelectorAll(".ingest-source").forEach(input => {
    input.checked = Boolean(selected);
  });
  syncBoardsOptionsVisibility();
}

function selectedBoardsTypes() {
  return [...document.querySelectorAll(".boards-type-option")]
    .filter(input => input.checked)
    .map(input => input.value);
}

function syncBoardsTypeSummary() {
  const summary = document.querySelector("#boards-type-summary");
  if (!summary) return;
  const all = [...document.querySelectorAll(".boards-type-option")];
  const selected = all.filter(input => input.checked);
  if (!selected.length) {
    summary.textContent = "No types";
  } else if (selected.length === all.length) {
    summary.textContent = "All types";
  } else {
    summary.textContent = selected.map(input => input.value).join(", ");
  }
}

function syncBoardsOptionsVisibility() {
  const panel = document.querySelector("#ingest-boards-options");
  if (!panel) return;
  const enabled = Boolean(document.querySelector('.ingest-source[value="azure-boards"]')?.checked);
  panel.hidden = !enabled;
  syncBoardsTypeSummary();
}

async function syncIngestSourcesWithMcp() {
  try {
    const payload = await api("/api/mcp/servers");
    const servers = payload.servers || [];
    const byId = Object.fromEntries(servers.map(server => [server.id, server]));
    const ado = byId["azure-devops"];
    const adoGit = byId["azure-devops-git"];
    const granola = byId.granola;
    const adoOn = Boolean(ado?.enabled);
    const adoGitOn = Boolean(adoGit?.enabled) || adoOn;
    const granolaOn = Boolean(granola?.enabled);
    const mark = (value, enabled) => {
      const input = document.querySelector(`.ingest-source[value="${value}"]`);
      if (!input) return;
      const label = input.closest(".source-checkbox");
      if (enabled) {
        input.checked = true;
        input.disabled = false;
        if (label) label.title = label.dataset.readyTitle || label.title;
      } else {
        input.disabled = false;
        if (label && !label.dataset.readyTitle) label.dataset.readyTitle = label.title;
      }
    };
    mark("azure-boards", adoOn);
    mark("azure-wiki", adoOn);
    mark("azure-git", adoGitOn);
    mark("granola", granolaOn);
    syncBoardsOptionsVisibility();
  } catch (_error) {
    // MCP list is best-effort for AutoScan defaults.
    syncBoardsOptionsVisibility();
  }
}
async function loadTasks() {
  const payload = await api(`/api/tasks?project_id=${projectParam()}`);
  const board = document.querySelector("#task-board");
  if (!board) return;
  board.innerHTML = "";
  for (const status of ["todo", "doing", "blocked", "done"]) {
    const column = document.createElement("section");
    column.className = "task-column";
    column.innerHTML = `<h4>${status}</h4><div class="task-column-cards" data-status="${status}"></div>`;
    const cards = column.querySelector(".task-column-cards");
    for (const task of payload.tasks.filter(item => item.status === status)) {
      const el = document.createElement("article");
      el.className = "task-card";
      el.innerHTML = `<strong>${escapeHtml(task.title)}</strong><p>${escapeHtml(task.detail)}</p><select data-task="${escapeHtml(task.id)}"><option>todo</option><option>doing</option><option>blocked</option><option>done</option></select>`;
      el.querySelector("select").value = task.status;
      cards.appendChild(el);
    }
    if (status === "todo") {
      const composer = document.createElement("form");
      composer.className = "task-inline-composer";
      composer.innerHTML = `
        <input class="task-inline-title" type="text" placeholder="+ Add a task" aria-label="New task title" required>
        <div class="task-inline-extra" hidden>
          <select class="task-inline-priority" aria-label="Priority">
            <option value="high">high</option>
            <option value="medium" selected>medium</option>
            <option value="low">low</option>
          </select>
          <textarea class="task-inline-detail" rows="2" placeholder="Optional detail"></textarea>
          <div class="task-inline-actions">
            <button type="submit" class="btn btn-primary btn-sm">Add</button>
            <button type="button" class="btn btn-secondary btn-sm" data-cancel-task>Cancel</button>
          </div>
        </div>`;
      const title = composer.querySelector(".task-inline-title");
      const extra = composer.querySelector(".task-inline-extra");
      title.addEventListener("focus", () => { extra.hidden = false; });
      composer.querySelector("[data-cancel-task]").addEventListener("click", () => {
        composer.reset();
        extra.hidden = true;
      });
      composer.addEventListener("submit", async (event) => {
        event.preventDefault();
        const value = title.value.trim();
        if (!value) return;
        await api("/api/tasks", {
          method: "POST",
          body: JSON.stringify({
            project_id: state.projectId,
            title: value,
            priority: composer.querySelector(".task-inline-priority").value || "medium",
            status: "todo",
            detail: composer.querySelector(".task-inline-detail").value || "",
          }),
        });
        await loadTasks();
        await refreshWorkspace();
      });
      column.appendChild(composer);
    }
    board.appendChild(column);
  }
  board.querySelectorAll("[data-task]").forEach(select => select.addEventListener("change", async () => { await api(`/api/tasks/${select.dataset.task}`, { method: "PATCH", body: JSON.stringify({ status: select.value }) }); await loadTasks(); await refreshWorkspace(); }));
}
async function loadChats(options = {}) {
  const openDefault = options.openDefault !== false;
  const payload = await api(`/api/chats?project_id=${projectParam()}&limit=80`);
  const list = document.querySelector("#chat-list");
  list.innerHTML = "";
  for (const chat of payload.chats) {
    const el = document.createElement("article");
    el.className = "result";
    const keeper = chat.keeper_status || {};
    const rolling = (chat.context_summaries || []).find(item => item.summary_type === "rolling") || {};
    const status = keeper.status ? `<span class="badge">${escapeHtml(keeper.status)}</span>` : "";
    const summary = rolling.summary_text ? `<p class="chat-summary-preview">${escapeHtml(String(rolling.summary_text).split("\n").slice(-1)[0] || "")}</p>` : "";
    el.innerHTML = `<strong>${escapeHtml(chat.title)}</strong><p>${Number(chat.message_count || 0)} messages ${status}</p>${summary}<div class="chat-card-actions"><button type="button" data-chat-context="${escapeHtml(chat.id)}">Context</button><button type="button" data-chat-retry="${escapeHtml(chat.id)}">Retry Keeper</button></div>`;
    el.addEventListener("click", event => {
      if (event.target.closest("button")) return;
      openChat(chat.id).catch(showError);
    });
    list.appendChild(el);
  }
  if (openDefault && !state.chatId && payload.chats[0]) await openChat(payload.chats[0].id);
  else if (!state.chatId) updateChatEmpty();
}
async function fetchChat(chatId) {
  const payload = await api(`/api/chats/${encodeURIComponent(chatId)}`);
  return payload.chat;
}
async function openChat(chatId) {
  const chat = await fetchChat(chatId);
  state.chatId = chat.id;
  renderChat(chat);
  return chat;
}
const CHAT_SUGGESTIONS = [
  "Explain adapter routing",
  "Summarize project memory",
  "What are the current tasks?",
  "Review the architecture decisions",
];
function chatEmptyHtml() {
  const chips = CHAT_SUGGESTIONS.map(text => `<button type="button" class="chat-suggestion" data-suggest="${escapeHtml(text)}">${escapeHtml(text)}</button>`).join("");
  return `<div id="chat-empty" class="chat-empty"><div class="chat-empty-inner"><div class="chat-empty-mark">AO</div><p class="chat-empty-title">Ask ArchitectOS using project memory</p><p class="chat-empty-sub">Pick the model below, or start with a suggestion.</p><div class="chat-suggestions">${chips}</div></div></div>`;
}
function updateChatEmpty() {
  const thread = document.querySelector("#chat-thread");
  if (!thread) return;
  if (!thread.querySelector(".message")) {
    if (!thread.querySelector("#chat-empty")) thread.innerHTML = chatEmptyHtml();
  } else {
    const empty = thread.querySelector("#chat-empty");
    if (empty) empty.remove();
  }
}
function renderChat(chat) {
  const thread = document.querySelector("#chat-thread");
  thread.innerHTML = "";
  (chat.messages || []).forEach((message, index) => appendChatBubble(message.role, message.text, false, message.provider, {
    chatId: chat.id,
    messageIndex: index,
    favorite: Boolean(message.favorite),
    retrievalRating: Number(message.retrieval_rating || 0),
    memoryHitIds: Array.isArray(message.memory_hit_ids) ? message.memory_hit_ids : [],
    toolTrace: Array.isArray(message.tool_trace) ? message.tool_trace : [],
    structured: message.structured || null,
    rawText: message.raw_text || "",
    usage: message.usage || null,
  }));
  updateChatEmpty();
  thread.scrollTop = thread.scrollHeight;
  if (typeof workspaceChat !== "undefined" && workspaceChat.renderFromChat) {
    workspaceChat.renderFromChat(chat);
  }
}
function providerLabel(provider) {
  if (!provider) return "";
  const selected = provider.selected || {};
  const label = selected.label || provider.label || provider.id || "";
  const model = selected.model || provider.model || "";
  if (model && label && !String(label).toLowerCase().includes(String(model).toLowerCase())) {
    return `${label} · ${model}`;
  }
  return label || model || "";
}
function formatTokenCount(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n >= 1000000) return `${(n / 1000000).toFixed(n >= 10000000 ? 0 : 1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  return String(Math.round(n));
}
function formatUsageCost(cost) {
  const n = Number(cost);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n === 0) return "$0";
  if (n < 0.001) return `$${n.toFixed(5)}`;
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(3)}`;
}
function formatUsageLabel(usage) {
  if (!usage || typeof usage !== "object") return "";
  const prompt = Number(usage.prompt_tokens || 0);
  const completion = Number(usage.completion_tokens || 0);
  const total = Number(usage.total_tokens || (prompt + completion));
  if (!prompt && !completion && !total) return "";
  const parts = [
    `${formatTokenCount(prompt)} in`,
    `${formatTokenCount(completion)} out`,
  ];
  if (total) parts.push(`${formatTokenCount(total)} tot`);
  const cost = formatUsageCost(usage.cost_usd);
  if (cost) parts.push(usage.cost_estimated ? `~${cost}` : cost);
  return parts.join(" · ");
}
function setBubbleUsage(el, usage) {
  if (!el) return;
  let footer = el.querySelector(".message-usage");
  const label = formatUsageLabel(usage);
  if (!label) {
    if (footer) footer.remove();
    return;
  }
  if (!footer) {
    footer = document.createElement("div");
    footer.className = "message-usage";
    const actions = el.querySelector(".message-actions");
    if (actions) el.insertBefore(footer, actions);
    else el.appendChild(footer);
  }
  footer.textContent = label;
  footer.title = t("chat.usageTitle");
}
function setAgentActivityModel(bubble, provider) {
  const panel = bubble && bubble.querySelector(".agent-activity");
  if (!panel) return;
  const label = providerLabel(provider);
  if (label) panel.dataset.modelLabel = label;
  refreshAgentActivityTitle(panel);
}
function setBubbleProvider(el, provider) {
  const persona = el && el._persona;
  if (!persona) return;
  const label = providerLabel(provider) || "AI";
  const routing = (provider && provider.routing) || {};
  const tag = String(routing.role || routing.strategy || "").trim();
  const reason = String(routing.reason || "").trim();
  persona.title = reason || label;
  persona.innerHTML = `<span class="msg-avatar">${escapeHtml((label.trim().charAt(0) || "A").toUpperCase())}</span><span class="msg-persona-name">${escapeHtml(label)}</span>${tag ? `<span class="msg-persona-tag">${escapeHtml(tag)}</span>` : ""}`;
  setAgentActivityModel(el, provider);
}
function isProviderError(provider) {
  return !!(provider && provider.status === "error");
}
function providerErrorAction(text) {
  const value = String(text || "").toLowerCase();
  if (value.includes("approval")) {
    return { label: "Enable CLI runs", action: "cli" };
  }
  return { label: "Configure providers", action: "providers" };
}
function handleProviderErrorAction(action) {
  if (action === "cli") {
    const checkbox = document.querySelector("#chat-approve-cli");
    if (checkbox) checkbox.checked = true;
    showSnackbar("CLI runs enabled for this dialog. Send your message again.", "info");
    return;
  }
  if (action === "retry-auto") {
    retryChatWithAuto(state.lastFailedMessage).catch(showError);
    return;
  }
  switchView("providers");
}
function askProviderId() {
  if (state.askMode === "memory") return "local-memory";
  if (state.askMode === "memory-mcp") {
    const value = document.querySelector("#chat-provider")?.value || "auto";
    return value === "local-memory" ? "auto" : value;
  }
  return document.querySelector("#chat-provider")?.value || "auto";
}
function usedExplicitProvider() {
  const providerValue = askProviderId();
  return providerValue && providerValue !== "auto" && providerValue !== "local-memory"
    && state.askMode !== "memory";
}
async function retryChatWithAuto(message) {
  const text = String(message || "").trim();
  if (!text) {
    showSnackbar("No message to retry.", "info");
    return;
  }
  const selects = [document.querySelector("#chat-provider"), document.querySelector("#workspace-chat-provider")].filter(Boolean);
  for (const select of selects) select.value = "auto";
  syncAskMode();
  const input = document.querySelector("#chat-message");
  if (input) {
    input.value = text;
    autoGrowChatInput();
  }
  showSnackbar(t("provider.retryAuto"), "info");
  await sendChatMessage(new Event("submit"));
}
function renderProviderErrorBubble(el, text, provider) {
  el.classList.remove("streaming");
  el.classList.add("error");
  el.textContent = "";
  const body = document.createElement("div");
  body.className = "provider-error-text";
  body.textContent = text || "The selected provider could not respond.";
  el.appendChild(body);
  const cta = providerErrorAction(text);
  const actions = document.createElement("div");
  actions.className = "provider-error-actions";
  if (usedExplicitProvider()) {
    const retryButton = document.createElement("button");
    retryButton.type = "button";
    retryButton.className = "btn btn-primary btn-sm";
    retryButton.textContent = t("provider.retryAuto");
    retryButton.addEventListener("click", () => handleProviderErrorAction("retry-auto"));
    actions.appendChild(retryButton);
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-secondary btn-sm";
  button.textContent = cta.label;
  button.addEventListener("click", () => handleProviderErrorAction(cta.action));
  actions.appendChild(button);
  el.appendChild(actions);
}
function getMessageTextElement(bubble) {
  return bubble.querySelector(".message-text") || bubble;
}
function renderAssistantRichContent(bubble, text, structuredHint) {
  const textNode = getMessageTextElement(bubble);
  if (!textNode) return;
  if (window.ArchitectOSRich && typeof window.ArchitectOSRich.renderInto === "function") {
    window.ArchitectOSRich.renderInto(textNode, text || "", structuredHint || null);
    return;
  }
  textNode.textContent = text || "";
}
function appendChatBubble(role, text, streaming = false, provider = null, meta = {}) {
  const thread = document.querySelector("#chat-thread");
  const empty = thread.querySelector("#chat-empty");
  if (empty) empty.remove();
  let persona = null;
  if (role === "assistant") {
    persona = document.createElement("div");
    persona.className = "msg-persona";
    thread.appendChild(persona);
  }
  const el = document.createElement("div");
  el.className = `message ${role}${streaming ? " streaming" : ""}`;
  if (role === "assistant") {
    const textNode = document.createElement("div");
    textNode.className = "message-text";
    el.appendChild(textNode);
    if (streaming) {
      textNode.textContent = text || "";
    } else {
      renderAssistantRichContent(el, meta.rawText || text || "", meta.structured || null);
    }
    if (!streaming && Array.isArray(meta.toolTrace) && meta.toolTrace.length) {
      finalizeAgentActivity(el, meta.toolTrace);
    }
    if (!streaming && meta.chatId && Number.isInteger(meta.messageIndex)) {
      const actions = document.createElement("div");
      actions.className = "message-actions";
      const rating = Number(meta.retrievalRating || 0);
      const upActive = rating > 0 ? "active" : "";
      const downActive = rating < 0 ? "active" : "";
      actions.innerHTML = [
        `<button type="button" class="message-action-btn ${meta.favorite ? "active" : ""}" data-favorite-message="${escapeHtml(meta.chatId)}" data-message-index="${escapeHtml(String(meta.messageIndex))}" title="${meta.favorite ? "Saved as favorite" : "Save reply as memory candidate"}">★</button>`,
        `<button type="button" class="message-action-btn ${upActive}" data-feedback-rating="1" data-feedback-chat="${escapeHtml(meta.chatId)}" data-message-index="${escapeHtml(String(meta.messageIndex))}" title="Memory helped">👍</button>`,
        `<button type="button" class="message-action-btn ${downActive}" data-feedback-rating="-1" data-feedback-chat="${escapeHtml(meta.chatId)}" data-message-index="${escapeHtml(String(meta.messageIndex))}" title="Memory did not help">👎</button>`,
      ].join("");
      el.appendChild(actions);
    }
    if (!streaming) setBubbleUsage(el, meta.usage);
  } else {
    el.textContent = text || "";
  }
  thread.appendChild(el);
  if (persona) { el._persona = persona; setBubbleProvider(el, provider); }
  if (role === "assistant" && !streaming && isProviderError(provider)) {
    renderProviderErrorBubble(el, text, provider);
  }
  thread.scrollTop = thread.scrollHeight;
  return el;
}
async function sendChatMessage(event, options = {}) {
  event.preventDefault();
  const workspaceMirror = Boolean(options.workspaceMirror);
  const input = document.querySelector("#chat-message");
  const message = input.value.trim();
  if (!message) return;
  if (state.askMode === "council") {
    await runAskCouncil(message);
    return;
  }
  const providerValue = askProviderId();
  state.lastFailedMessage = message;
  const payload = {
    project_id: state.projectId,
    chat_id: state.chatId,
    message,
    provider_id: providerValue,
    ask_mode: state.askMode,
    remember: document.querySelector("#chat-remember").checked,
    allow_cli: document.querySelector("#chat-approve-cli").checked,
    attachments: state.attachments.map(file => file.id),
  };
  input.value = "";
  autoGrowChatInput();
  closeChatMenu();
  state.attachments = [];
  renderAttachments();
  appendChatBubble("user", message);
  const assistant = appendChatBubble("assistant", "", true);
  beginAgentActivity(assistant);
  if (workspaceMirror && typeof workspaceChat !== "undefined") {
    workspaceChat.beginStream(message);
  }
  try {
    const response = await fetch("/api/chat/message/stream", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(payload) });
    if (!response.ok || !response.body) {
      const fallback = await response.json();
      throw new Error(fallback.error || `Stream failed: ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalChat = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const line = frame.split("\n").find(item => item.startsWith("data:"));
        if (!line) continue;
        let event;
        try {
          event = JSON.parse(line.slice(5).trim());
        } catch (_parseError) {
          continue; // skip a malformed SSE frame instead of killing the stream
        }
        if (event.type === "start") {
          state.activeRunId = event.run_id || "";
          setElementDisabled("#chat-stop", !state.activeRunId);
          if (event.provider) setBubbleProvider(assistant, event.provider);
          beginAgentActivity(assistant);
          if (workspaceMirror) workspaceChat.onStreamStart?.(event);
        } else if (event.type === "delta") {
          getMessageTextElement(assistant).textContent += event.text || "";
          if (workspaceMirror) workspaceChat.onStreamDelta?.(event.text || "");
        } else if (event.type === "progress") {
          updateAgentActivity(assistant, event);
          if (workspaceMirror) workspaceChat.onStreamProgress?.(event);
        } else if (event.type === "done") {
          assistant.classList.remove("streaming");
          state.activeRunId = "";
          setElementDisabled("#chat-stop", true);
          if (event.provider) setBubbleProvider(assistant, event.provider);
          if (event.usage) setBubbleUsage(assistant, event.usage);
          if (isProviderError(event.provider)) {
            state.lastFailedMessage = message;
            renderProviderErrorBubble(assistant, event.response || getMessageTextElement(assistant).textContent, event.provider);
          } else {
            const finalText = event.response || getMessageTextElement(assistant).textContent || "";
            const withStderr = event.raw && event.raw.stderr ? `${finalText}\n\nstderr: ${event.raw.stderr}` : finalText;
            renderAssistantRichContent(assistant, withStderr, event.structured || null);
            finalizeAgentActivity(assistant, event.tool_trace);
          }
          if (event.chat && event.chat.id) {
            state.chatId = event.chat.id;
            finalChat = event.chat;
          }
          if (workspaceMirror) workspaceChat.onStreamDone?.(event);
        } else if (event.type === "error") {
          assistant.classList.remove("streaming");
          stopAgentActivityTimer(assistant.querySelector(".agent-activity"));
          getMessageTextElement(assistant).textContent += `\n${event.error || "stream error"}`;
          if (workspaceMirror) workspaceChat.onStreamError?.(event.error || "stream error");
        }
        document.querySelector("#chat-thread").scrollTop = document.querySelector("#chat-thread").scrollHeight;
      }
    }
    if (workspaceMirror) {
      if (finalChat) workspaceChat.renderFromChat(finalChat);
      else if (typeof workspaceChat.refreshThread === "function") await workspaceChat.refreshThread({ allowEmpty: false });
    } else if (typeof workspaceChat !== "undefined" && workspaceChat.refreshThread) {
      await workspaceChat.refreshThread();
    }
  } catch (error) {
    const text = error?.message || t("error.serverUnreachable");
    const target = getMessageTextElement(assistant);
    target.textContent += `${target.textContent ? "\n" : ""}⚠️ ${text}`;
    if (workspaceMirror) workspaceChat.onStreamError?.(text);
  } finally {
    assistant.classList.remove("streaming");
    state.activeRunId = "";
    setElementDisabled("#chat-stop", true);
    stopAgentActivityTimer(assistant.querySelector(".agent-activity"));
  }
  await loadChats({ openDefault: false });
  await loadProviderRuns();
}
const TOOL_ACTION_META = {
  memory_get: { icon: "🧠", verb: "Reading memory node" },
  boards_search: { icon: "🔎", verb: "Searching Azure Boards" },
  boards_my_work: { icon: "📋", verb: "Loading my work items" },
  boards_get_item: { icon: "📄", verb: "Opening work item" },
  boards_list_comments: { icon: "💬", verb: "Reading work item comments" },
  boards_query_wiql: { icon: "🧮", verb: "Running WIQL query" },
  granola_list_meetings: { icon: "📝", verb: "Listing Granola meetings" },
  granola_get_meetings: { icon: "🗒️", verb: "Reading Granola meeting notes" },
  granola_get_transcript: { icon: "🎙️", verb: "Fetching Granola transcript" },
  fs_read: { icon: "📁", verb: "Reading file" },
  fs_list: { icon: "🗂️", verb: "Listing directory" },
  fs_search: { icon: "🔍", verb: "Searching files" },
  fs_write: { icon: "✏️", verb: "Writing file" },
};
function formatActivityDuration(seconds) {
  const sec = Math.max(0, Math.floor(Number(seconds) || 0));
  if (sec < 60) return `${sec}s`;
  const minutes = Math.floor(sec / 60);
  const rem = sec % 60;
  if (minutes < 60) return rem ? `${minutes}m ${rem}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins ? `${hours}h ${mins}m` : `${hours}h`;
}
function humanizeToolAction(name, args) {
  const meta = TOOL_ACTION_META[name] || { icon: "⚙️", verb: name || "Running tool" };
  const a = args || {};
  let detail = "";
  if (name === "memory_get") detail = a.id || a.node_id || "";
  else if (name === "boards_search" || name === "fs_search") detail = a.query || a.text || "";
  else if (name === "boards_get_item" || name === "boards_list_comments") detail = a.id || a.work_item_id || a.item_id || "";
  else if (name === "fs_read" || name === "fs_write" || name === "fs_list") detail = a.path || a.file || "";
  else {
    const first = Object.values(a).find(v => typeof v === "string" && v);
    detail = first || "";
  }
  detail = String(detail).slice(0, 80);
  return { icon: meta.icon, label: detail ? `${meta.verb} ${detail}` : meta.verb };
}
function humanizeProgressEvent(event) {
  const toolName = String(event?.tool_name || "").trim();
  if (toolName) return humanizeToolAction(toolName, event.arguments);
  const phase = String(event?.phase || "");
  const status = String(event?.status || "").trim();
  if (phase === "provider") return { icon: "🤖", label: status || "Model selected" };
  if (phase === "context" || phase === "context_done") return { icon: "📚", label: status || "Searching memory" };
  if (phase === "thinking" || phase === "thinking_done") return { icon: "💭", label: status || "Thinking…" };
  if (status) return { icon: "⚙️", label: status };
  return { icon: "⚙️", label: phase || "Working…" };
}
function humanizeTraceItem(item) {
  if (item && item.kind === "agent") {
    const name = item.role_name || item.role || item.name || "Agent";
    return { icon: item.role === "synthesis" ? "🧩" : "🤖", label: `${name} agent` };
  }
  if (item && item.kind === "status") {
    return humanizeProgressEvent({ phase: item.phase, status: item.summary || item.status, tool_name: "" });
  }
  return humanizeToolAction(item && item.name, item && item.arguments);
}
function getAgentActivity(bubble) {
  if (!bubble) return null;
  let panel = bubble.querySelector(".agent-activity");
  if (panel) return panel;
  panel = document.createElement("div");
  panel.className = "agent-activity running";
  panel.innerHTML = `
    <button type="button" class="agent-activity-header" aria-expanded="true">
      <span class="agent-activity-spinner"></span>
      <span class="agent-activity-title">Working…</span>
      <span class="agent-activity-caret">▾</span>
    </button>
    <div class="agent-activity-steps"></div>`;
  panel.querySelector(".agent-activity-header").addEventListener("click", () => {
    const collapsed = panel.classList.toggle("collapsed");
    panel.querySelector(".agent-activity-header").setAttribute("aria-expanded", String(!collapsed));
  });
  const textNode = getMessageTextElement(bubble);
  bubble.insertBefore(panel, textNode);
  return panel;
}
function refreshAgentActivityTitle(panel) {
  if (!panel) return;
  const title = panel.querySelector(".agent-activity-title");
  if (!title) return;
  const started = Number(panel.dataset.startedAt || 0);
  const elapsed = started ? formatActivityDuration((Date.now() - started) / 1000) : "";
  const model = String(panel.dataset.modelLabel || "").trim();
  if (panel.classList.contains("running")) {
    if (model && elapsed) title.textContent = `${model} · ${elapsed}`;
    else if (model) title.textContent = model;
    else title.textContent = elapsed ? `Working… ${elapsed}` : "Working…";
    return;
  }
  const count = panel.querySelectorAll(".agent-activity-step").length;
  const hasAgentTrace = panel.dataset.kind === "agent";
  const base = count
    ? hasAgentTrace
      ? `Ran ${count} agent step${count === 1 ? "" : "s"}`
      : `Used ${count} step${count === 1 ? "" : "s"}`
    : "Worked";
  title.textContent = [base, model, elapsed].filter(Boolean).join(" · ");
}
function beginAgentActivity(bubble) {
  const panel = getAgentActivity(bubble);
  if (!panel) return panel;
  if (!panel.dataset.startedAt) panel.dataset.startedAt = String(Date.now());
  panel.classList.add("running");
  panel.classList.remove("done", "collapsed");
  panel.querySelector(".agent-activity-header")?.setAttribute("aria-expanded", "true");
  if (panel._elapsedTimer) clearInterval(panel._elapsedTimer);
  refreshAgentActivityTitle(panel);
  panel._elapsedTimer = setInterval(() => {
    // Bubble removed from the DOM (chat switched mid-stream) — stop ticking.
    if (!panel.isConnected) {
      stopAgentActivityTimer(panel);
      return;
    }
    refreshAgentActivityTitle(panel);
  }, 1000);
  return panel;
}
function stopAgentActivityTimer(panel) {
  if (!panel) return;
  if (panel._elapsedTimer) {
    clearInterval(panel._elapsedTimer);
    panel._elapsedTimer = null;
  }
}
function updateAgentActivity(bubble, event) {
  if (!bubble || !event) return;
  const phase = event.phase || (String(event.status || "").startsWith("Finished") ? "tool_finish" : "tool_start");
  const stepId = event.step_id != null ? String(event.step_id) : "";
  const toolName = String(event.tool_name || "").trim();
  if (!toolName && !stepId && !event.status && !event.provider) return;
  const panel = beginAgentActivity(bubble);
  if (event.provider) {
    setBubbleProvider(bubble, event.provider);
    setAgentActivityModel(bubble, event.provider);
  }
  if (phase === "provider" && !toolName) {
    // Provider announcement is reflected in the header/persona; keep a compact step too.
    const steps = panel.querySelector(".agent-activity-steps");
    const providerStepId = stepId || "prep:provider";
    let row = steps.querySelector(`[data-step="${CSS.escape(providerStepId)}"]`);
    const { icon, label } = humanizeProgressEvent({
      phase: "provider",
      status: event.status || `Using ${providerLabel(event.provider)}`,
    });
    if (!row) {
      row = document.createElement("div");
      row.className = "agent-activity-step";
      row.dataset.step = providerStepId;
      row.innerHTML = `
        <span class="step-icon">${escapeHtml(icon === "⚙️" ? "🤖" : icon)}</span>
        <span class="step-body"><span class="step-label"></span><span class="step-detail"></span></span>
        <span class="step-status"></span>`;
      steps.appendChild(row);
    }
    row.dataset.state = "ok";
    row.querySelector(".step-label").textContent = label || providerLabel(event.provider) || "Model selected";
    row.querySelector(".step-status").textContent = "✓";
    refreshAgentActivityTitle(panel);
    return;
  }
  if (!toolName && !stepId && !event.status) return;
  const steps = panel.querySelector(".agent-activity-steps");
  const { icon, label } = humanizeProgressEvent(event);
  let row = stepId ? steps.querySelector(`[data-step="${CSS.escape(stepId)}"]`) : null;
  if (!row) {
    row = document.createElement("div");
    row.className = "agent-activity-step";
    if (stepId) row.dataset.step = stepId;
    row.innerHTML = `
      <span class="step-icon">${escapeHtml(icon)}</span>
      <span class="step-body"><span class="step-label"></span><span class="step-detail"></span></span>
      <span class="step-status"></span>`;
    steps.appendChild(row);
  }
  row.querySelector(".step-label").textContent = label;
  const donePhase = phase === "tool_finish" || phase === "thinking_done" || phase === "context_done" || phase.endsWith("_done");
  if (donePhase) {
    const ok = event.ok !== false;
    row.dataset.state = ok ? "ok" : "error";
    row.querySelector(".step-status").textContent = ok ? "✓" : "✗";
    const entry = Array.isArray(event.tool_trace)
      ? [...event.tool_trace].reverse().find(item => String(item.step_id || "") === stepId)
      : null;
    let detail = "";
    if (entry) {
      detail = entry.ok ? String(entry.summary || "") : String(entry.error || "failed");
    } else if (event.status && event.status !== label) {
      detail = String(event.status);
    }
    if (detail) row.querySelector(".step-detail").textContent = detail.slice(0, 180);
  } else {
    row.dataset.state = "running";
    row.querySelector(".step-status").textContent = "";
    if (event.status && event.status !== label) {
      row.querySelector(".step-detail").textContent = String(event.status).slice(0, 180);
    }
  }
  refreshAgentActivityTitle(panel);
  bubble.scrollIntoView?.({ block: "nearest" });
}
function updateCouncilActivity(bubble, event) {
  const agent = event && event.agent;
  if (!bubble || !agent) return;
  const role = String(agent.role || "model");
  const roleName = String(agent.role_name || role || "Model");
  const isJudge = role === "judge";
  const done = String(agent.status || "").toLowerCase() === "done";
  const panel = beginAgentActivity(bubble);
  panel.dataset.kind = "agent";
  const steps = panel.querySelector(".agent-activity-steps");
  let row = steps.querySelector(`[data-step="${CSS.escape(`council:${role}`)}"]`);
  if (!row) {
    row = document.createElement("div");
    row.className = "agent-activity-step";
    row.dataset.step = `council:${role}`;
    row.innerHTML = `
      <span class="step-icon">${isJudge ? "⚖️" : "🤖"}</span>
      <span class="step-body"><span class="step-label"></span><span class="step-detail"></span></span>
      <span class="step-status"></span>`;
    row.querySelector(".step-label").textContent = isJudge ? "Judge" : roleName;
    steps.appendChild(row);
  }
  row.dataset.state = done ? "ok" : "running";
  row.querySelector(".step-status").textContent = done ? "✓" : "";
  const text = String(agent.text || "").trim();
  const detail = done
    ? (text ? text.slice(0, 140) : "Responded")
    : String(event.status || "Running").replace(/\.\.\.$/, "");
  row.querySelector(".step-detail").textContent = detail;
  refreshAgentActivityTitle(panel);
  bubble.scrollIntoView?.({ block: "nearest" });
}
function finalizeAgentActivity(bubble, trace) {
  if (!bubble) return;
  let panel = bubble.querySelector(".agent-activity");
  if (!panel && Array.isArray(trace) && trace.length) {
    panel = getAgentActivity(bubble);
    const steps = panel.querySelector(".agent-activity-steps");
    for (const item of trace) {
      const { icon, label } = humanizeTraceItem(item);
      const row = document.createElement("div");
      row.className = "agent-activity-step";
      const pending = item.pending === true;
      row.dataset.state = pending ? "running" : item.ok === false ? "error" : "ok";
      const detail = item.ok === false ? String(item.error || "failed") : String(item.summary || item.status || "");
      row.innerHTML = `
        <span class="step-icon">${escapeHtml(icon)}</span>
        <span class="step-body"><span class="step-label"></span><span class="step-detail"></span></span>
        <span class="step-status">${pending ? "" : item.ok === false ? "✗" : "✓"}</span>`;
      row.querySelector(".step-label").textContent = label;
      if (detail) row.querySelector(".step-detail").textContent = detail.slice(0, 180);
      steps.appendChild(row);
    }
  }
  if (!panel) return;
  stopAgentActivityTimer(panel);
  panel.classList.remove("running");
  panel.classList.add("done", "collapsed");
  panel.querySelector(".agent-activity-header").setAttribute("aria-expanded", "false");
  refreshAgentActivityTitle(panel);
}
async function runAskCouncil(message) {
  const input = document.querySelector("#chat-message");
  const attachments = state.attachments.map(file => file.id);
  if (input) {
    input.value = "";
    autoGrowChatInput();
  }
  closeChatMenu();
  state.attachments = [];
  renderAttachments();
  appendChatBubble("user", message);
  const assistant = appendChatBubble("assistant", "⚖️ Convening the council...", true, { selected: { label: "Council" }, routing: { role: "council" } });
  beginAgentActivity(assistant);
  try {
    const models = [...document.querySelectorAll(".ask-council-model")].filter(el => el.checked).map(el => el.value);
    const response = await fetch("/api/council/run/stream", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        project_id: state.projectId,
        chat_id: state.chatId,
        message,
        models,
        judge_provider_id: document.querySelector("#ask-council-judge")?.value || "auto",
        allow_cli: Boolean(document.querySelector("#chat-approve-cli")?.checked),
        attachments,
      }),
    });
    if (!response.ok || !response.body) {
      const fallback = await response.json();
      throw new Error(fallback.error || `Stream failed: ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const line = frame.split("\n").find(item => item.startsWith("data:"));
        if (!line) continue;
        const event = JSON.parse(line.slice(5).trim());
        if (event.type === "progress") {
          updateCouncilActivity(assistant, event);
        } else if (event.type === "done") {
          const result = event.result;
          assistant.classList.remove("streaming");
          if (result.chat && result.chat.id) state.chatId = result.chat.id;
          const parts = [];
          if (result.synthesis) parts.push(result.synthesis);
          for (const answer of result.answers || []) {
            const label = answer.label || answer.provider_id;
            parts.push(`\n\n— ${label} —\n${answer.text || "(no answer)"}`);
          }
          const combined = result.response || parts.join("").trim() || "No model produced a response.";
          renderAssistantRichContent(assistant, combined, result.structured || null);
          const messages = result.chat && Array.isArray(result.chat.messages) ? result.chat.messages : [];
          const savedTrace = messages.length ? messages[messages.length - 1].tool_trace : [];
          finalizeAgentActivity(assistant, Array.isArray(savedTrace) ? savedTrace : []);
        }
        document.querySelector("#chat-thread").scrollTop = document.querySelector("#chat-thread").scrollHeight;
      }
    }
  } catch (error) {
    assistant.classList.remove("streaming");
    renderProviderErrorBubble(assistant, `Council failed: ${error.message || error}`, { status: "error" });
  }
  document.querySelector("#chat-thread").scrollTop = document.querySelector("#chat-thread").scrollHeight;
  await loadProviderRuns();
  if (typeof workspaceChat !== "undefined" && workspaceChat.refreshThread) {
    await workspaceChat.refreshThread();
  }
}
async function cancelActiveRun() {
  if (!state.activeRunId) return;
  await api(`/api/runs/${state.activeRunId}/cancel`, { method: "POST", body: "{}" });
  setElementDisabled("#chat-stop", true);
}
const graphState = {
  nodes: [],
  edges: [],
  allNodes: [],
  allEdges: [],
  particles: [],
  selectedId: "",
  hoverId: "",
  draggingId: "",
  dragMoved: false,
  modalOpen: false,
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  userZoomed: false,
  animationId: 0,
  initialized: false,
  expanded: false,
  stageObserver: null,
  physicsTicks: 0,
  physicsMax: 90,
  physicsActive: true,
  searchQuery: "",
  searchTimer: 0,
  groupFilter: "",
  densityLevel: 2,
};

const graphGroupPalette = {
  Projects: "#60a5fa",
  Decisions: "#2563eb",
  Rules: "#f97316",
  Requirements: "#22c55e",
  Knowledge: "#14b8a6",
  Sources: "#f59e0b",
  Operations: "#a78bfa",
  People: "#f472b6",
  Data: "#2dd4bf",
  Other: "#94a3b8",
};

const graphPalette = {
  Project: "#4f46e5",
  Decision: "#2563eb",
  Lesson: "#15803d",
  Constraint: "#b42318",
  Feature: "#b45309",
  Provider: "#7c3aed",
  Doc: "#0891b2",
  Artifact: "#475569",
  Task: "#0f766e",
  Concept: "#6b7280",
};

function graphNodeGroup(node) {
  const meta = node?.metadata || {};
  const source = String(meta.source_type || meta.source || node?.source_type || "").toLowerCase();
  const type = String(node?.type || "Concept");
  if (type === "Project") return "Projects";
  if (["Decision", "Constraint"].includes(type)) return "Decisions";
  if (type === "Rule") return "Rules";
  if (["Requirement", "Feature"].includes(type)) return "Requirements";
  if (["Doc", "Artifact", "Meeting"].includes(type) || /docs?|code|git|wiki|boards|teams|granola|pr|issue/.test(source)) return "Sources";
  if (["Provider", "Task"].includes(type)) return "Operations";
  if (/user|people|person|team/.test(source)) return "People";
  if (/data|metric|dataset/.test(source)) return "Data";
  if (["Lesson", "Concept"].includes(type)) return "Knowledge";
  return "Other";
}

/** Canonical ingest/source key for a memory node (mirrors backend _source_key_for_node). */
function graphNodeSourceKey(node) {
  const meta = node?.metadata || {};
  if (meta.source_key) return String(meta.source_key).toLowerCase();
  const raw = String(meta.source_type || meta.source || node?.source_type || "").trim().toLowerCase();
  if (!raw || raw === "source_hub" || raw === "scope_root" || raw === "project_profile") return "other";
  const aliases = {
    doc: "docs", documentation: "docs", ui: "manual", memory_candidate: "manual",
    promoted_candidate: "manual", azure_boards: "azure-boards", boards: "azure-boards",
    azure_wiki: "azure-wiki", wiki: "azure-wiki", azure_git: "azure-git",
    azure_repos: "azure-git", ado_git: "azure-git", ado_repos: "azure-git",
    teams: "teams-meetings", teams_meetings: "teams-meetings", ms_teams: "teams-meetings",
    facilitator: "teams-meetings", pr: "prs", pull_request: "prs", pull_requests: "prs",
  };
  return aliases[raw] || raw;
}

function graphGroupColor(group) {
  return graphGroupPalette[group] || graphPalette[group] || "#64748b";
}

function enrichGraphNode(node) {
  const meta = node.metadata || {};
  const group = graphNodeGroup(node);
  return {
    ...node,
    group,
    color: graphGroupColor(group),
    pinned: Boolean(meta.favorite || meta.pinned || node.pinned),
  };
}

/** Density budget: large graphs show a readable subset, fair-split by source. */
function graphVisibleBudget(total, densityLevel = graphState.densityLevel) {
  const level = Math.max(1, Math.min(5, Number(densityLevel) || 2));
  if (level >= 5) return Math.min(total, 500);
  if (level === 4) return Math.min(total, 350);
  if (level === 3) return Math.min(total, 280);
  if (level === 2) return Math.min(total, 200);
  return Math.min(total, 100);
}

function graphBackendLimit() {
  return { 1: 100, 2: 200, 3: 280, 4: 360, 5: 520 }[Math.max(1, Math.min(5, Number(graphState.densityLevel) || 2))] || 200;
}

function selectVisibleGraphNodes(nodes, edges, budget, selectedId) {
  if (nodes.length <= budget) return nodes.slice();
  const degree = new Map();
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  }
  const neighborIds = new Set();
  if (selectedId) {
    for (const edge of edges) {
      if (edge.source === selectedId) neighborIds.add(edge.target);
      if (edge.target === selectedId) neighborIds.add(edge.source);
    }
  }
  const must = [];
  const pools = new Map();
  const seen = new Set();
  for (const node of nodes) {
    if (!node?.id || seen.has(node.id)) continue;
    seen.add(node.id);
    const isMust =
      node.type === "Project"
      || node.pinned
      || node.id === selectedId
      || neighborIds.has(node.id);
    if (isMust) {
      must.push(node);
      continue;
    }
    const key = graphNodeSourceKey(node) || "other";
    if (!pools.has(key)) pools.set(key, []);
    pools.get(key).push(node);
  }
  const rank = (a, b) => {
    const da = degree.get(a.id) || 0;
    const db = degree.get(b.id) || 0;
    if (db !== da) return db - da;
    return String(a.label || "").localeCompare(String(b.label || ""));
  };
  for (const bucket of pools.values()) bucket.sort(rank);

  const out = [];
  const outIds = new Set();
  for (const node of must) {
    if (outIds.has(node.id)) continue;
    out.push(node);
    outIds.add(node.id);
  }
  const hardCap = Math.max(budget, must.length);
  const remaining = Math.max(0, hardCap - out.length);
  const sourceKeys = [...pools.keys()].sort();
  if (!sourceKeys.length || remaining <= 0) return out.slice(0, hardCap);

  const base = Math.floor(remaining / sourceKeys.length);
  const bonus = remaining % sourceKeys.length;
  const leftovers = new Map();
  sourceKeys.forEach((key, index) => {
    const quota = base + (index < bonus ? 1 : 0);
    const bucket = pools.get(key) || [];
    const take = Math.min(quota, bucket.length);
    for (let i = 0; i < take; i += 1) {
      const node = bucket[i];
      if (outIds.has(node.id)) continue;
      out.push(node);
      outIds.add(node.id);
    }
    leftovers.set(key, bucket.slice(take));
  });

  let slotsLeft = hardCap - out.length;
  while (slotsLeft > 0) {
    let progressed = false;
    for (const key of sourceKeys) {
      const queue = leftovers.get(key) || [];
      if (!queue.length) continue;
      const node = queue.shift();
      if (!node || outIds.has(node.id)) continue;
      out.push(node);
      outIds.add(node.id);
      slotsLeft -= 1;
      progressed = true;
      if (slotsLeft <= 0) break;
    }
    if (!progressed) break;
  }
  return out.slice(0, hardCap);
}

function applyGraphVisibility() {
  const query = graphState.searchQuery.trim().toLowerCase();
  const group = graphState.groupFilter || "";
  let baseNodes = graphState.allNodes;
  // Groups → Projects: show the project root plus its immediate hubs/children,
  // otherwise the filter collapses to a single lonely node and looks "broken".
  if (group === "Projects") {
    const projectIds = new Set(
      graphState.allNodes
        .filter(node => node.type === "Project" || node.group === "Projects")
        .map(node => node.id)
    );
    const keep = new Set(projectIds);
    for (const edge of graphState.allEdges) {
      if (projectIds.has(edge.source)) keep.add(edge.target);
      if (projectIds.has(edge.target)) keep.add(edge.source);
    }
    baseNodes = graphState.allNodes.filter(node => keep.has(node.id));
  }
  const filteredNodes = baseNodes.filter(node => {
    if (group && group !== "Projects" && node.group !== group) return false;
    if (!query) return true;
    const meta = node.metadata || {};
    const haystack = [
      node.id,
      node.label,
      node.text,
      node.type,
      node.scope,
      node.group,
      meta.source,
      meta.work_item_id,
      meta.meeting_id,
      meta.wiki_page_key,
    ].map(value => String(value || "").toLowerCase()).join(" ");
    return haystack.includes(query);
  });
  const filteredIds = new Set(filteredNodes.map(node => node.id));
  const filteredEdges = graphState.allEdges.filter(edge => filteredIds.has(edge.source) && filteredIds.has(edge.target));
  const budget = graphVisibleBudget(filteredNodes.length);
  graphState.nodes = selectVisibleGraphNodes(
    filteredNodes,
    filteredEdges,
    budget,
    graphState.selectedId,
  );
  const visible = new Set(graphState.nodes.map(node => node.id));
  graphState.edges = filteredEdges.filter(edge => visible.has(edge.source) && visible.has(edge.target));
  renderGraphLegend();
  updateGraphDensityHint();
}

function updateGraphDensityHint() {
  const el = document.querySelector("#graph-density-hint");
  if (!el) return;
  const total = graphState.totalNodesCount || graphState.allNodes.length;
  const shown = graphState.nodes.length;
  if (!total || shown >= total) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = `Showing ${shown} of ${total} node(s) · density ${graphState.densityLevel}/5 · fair by source`;
}

async function loadGraph() {
  await syncGraphFilters();
  const taskFilterValue = document.querySelector("#graph-task-filter")?.value || "";
  const providerFilterValue = document.querySelector("#graph-provider-filter")?.value || "";
  const pinnedOnly = document.querySelector("#graph-pinned-filter")?.checked;
  graphState.searchQuery = document.querySelector("#graph-search")?.value || "";
  graphState.groupFilter = document.querySelector("#graph-group-filter")?.value || "";
  graphState.densityLevel = Number(document.querySelector("#graph-density-level")?.value || graphState.densityLevel || 2);
  const sourceFilter = document.querySelector("#graph-source-filter");
  const scopeFilter = document.querySelector("#graph-scope-filter");
  const selectedSource = sourceFilter ? sourceFilter.value : "";
  const selectedScope = scopeFilter ? scopeFilter.value : "";
  const searchQuery = String(graphState.searchQuery || "").trim();
  
  const params = [`project_id=${projectParam()}`];
  if (taskFilterValue) params.push(`task_id=${encodeURIComponent(taskFilterValue)}`);
  if (providerFilterValue) params.push(`provider_id=${encodeURIComponent(providerFilterValue)}`);
  if (pinnedOnly) params.push("pinned=1");
  if (selectedSource) params.push(`source=${encodeURIComponent(selectedSource)}`);
  if (selectedScope) params.push(`scope=${encodeURIComponent(selectedScope)}`);
  if (searchQuery) params.push(`q=${encodeURIComponent(searchQuery)}`);
  params.push(`limit=${graphBackendLimit()}`);
  
  const payload = await api(`/api/graph?${params.join("&")}`);
  const sourceOptions = Array.isArray(payload.sources) ? payload.sources : [];
  if (sourceFilter) {
    const current = sourceFilter.value;
    sourceFilter.innerHTML = '<option value="">All sources</option>' + sourceOptions.map(item => {
      const id = typeof item === "string" ? item : (item.id || "");
      const label = typeof item === "string" ? item : (item.label || item.id || "");
      if (!id) return "";
      return `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`;
    }).join("");
    sourceFilter.value = sourceOptions.some(item => (typeof item === "string" ? item : item.id) === current) ? current : "";
  }
  
  graphState.totalNodesCount = payload.total_nodes || payload.nodes.length;
  
  graphState.allNodes = payload.nodes
    .map(enrichGraphNode)
    .filter(node => {
      if (!selectedSource) return true;
      if (node.type === "Project" || node.group === "Projects") return true;
      return graphNodeSourceKey(node) === selectedSource;
    })
    .filter(node => !selectedScope || node.scope === selectedScope);
  const allVisible = new Set(graphState.allNodes.map(node => node.id));
  graphState.allEdges = payload.edges.filter(edge => allVisible.has(edge.source) && allVisible.has(edge.target));
  if (graphState.selectedId && !allVisible.has(graphState.selectedId)) graphState.selectedId = "";
  applyGraphVisibility();
  graphState.physicsTicks = 0;
  graphState.physicsMax = Math.max(50, Math.round(140 / Math.sqrt(Math.max(1, graphState.nodes.length) / 50)));
  graphState.physicsActive = true;
  seedGraphParticles();
  bindGraphOnce();
  const needle = searchQuery.toLowerCase();
  let focusNode = null;
  if (needle) {
    focusNode =
      graphState.nodes.find(node => String(node.id || "").toLowerCase() === needle)
      || graphState.allNodes.find(node => String(node.id || "").toLowerCase() === needle)
      || graphState.nodes.find(node => String(node.id || "").toLowerCase().startsWith(needle))
      || graphState.allNodes.find(node => String(node.id || "").toLowerCase().startsWith(needle))
      || graphState.nodes.find(node => String(node.label || "").toLowerCase().includes(needle))
      || null;
  }
  if (focusNode) {
    graphState.selectedId = focusNode.id;
    applyGraphVisibility();
    seedGraphParticles();
    renderGraphSelection(focusNode);
  } else {
    renderGraphSelection(graphState.nodes.find(node => node.id === graphState.selectedId) || graphState.nodes[0] || null);
  }
  startGraphAnimation();
  graphState.userZoomed = false;
  requestAnimationFrame(() => {
    fitGraphToView();
    requestAnimationFrame(() => fitGraphToView());
  });
}

function seedGraphParticles() {
  const canvas = document.querySelector("#graph-canvas");
  const width = canvas?.width || 800;
  const height = canvas?.height || 600;
  const existing = new Map(graphState.particles.map(item => [item.id, item]));
  const byType = new Map();
  for (const node of graphState.nodes) {
    const key = node.group || node.type || "Other";
    if (!byType.has(key)) byType.set(key, []);
    byType.get(key).push(node);
  }
  const typeKeys = [...byType.keys()];
  const cx = width / 2;
  const cy = height / 2;
  const groupRadius = Math.min(width, height) * 0.28;
  const next = [];
  typeKeys.forEach((type, typeIndex) => {
    const group = byType.get(type) || [];
    const baseAngle = (typeIndex / Math.max(typeKeys.length, 1)) * Math.PI * 2;
    const gx = cx + Math.cos(baseAngle) * groupRadius;
    const gy = cy + Math.sin(baseAngle) * groupRadius;
    group.forEach((node, index) => {
      const old = existing.get(node.id);
      if (old && Number.isFinite(old.x) && Number.isFinite(old.y)) {
        next.push({ ...old, node, vx: 0, vy: 0 });
        return;
      }
      const angle = (index / Math.max(group.length, 1)) * Math.PI * 2;
      const radius = 28 + (index % 9) * 10;
      next.push({
        id: node.id,
        node,
        x: gx + Math.cos(angle) * radius,
        y: gy + Math.sin(angle) * radius,
        z: ((index % 7) - 3) / 3,
        vx: 0,
        vy: 0,
      });
    });
  });
  graphState.particles = next;
}

function bindGraphOnce() {
  if (graphState.initialized) return;
  const canvas = document.querySelector("#graph-canvas");
  if (!canvas) return;
  graphState.initialized = true;
  const sourceFilter = document.querySelector("#graph-source-filter");
  const scopeFilter = document.querySelector("#graph-scope-filter");
  const taskFilter = document.querySelector("#graph-task-filter");
  const providerFilter = document.querySelector("#graph-provider-filter");
  const pinnedFilter = document.querySelector("#graph-pinned-filter");
  const groupFilter = document.querySelector("#graph-group-filter");
  const searchInput = document.querySelector("#graph-search");
  const densityInput = document.querySelector("#graph-density-level");
  const fitButton = document.querySelector("#graph-fit");
  const expandButton = document.querySelector("#graph-expand");
  const rebuildButton = document.querySelector("#graph-rebuild-links");
  const inspectHint = document.querySelector("#graph-inspect-hint");
  if (inspectHint) inspectHint.textContent = t("graph.inspectHint");
  on(sourceFilter, "change", () => loadGraph().catch(showError));
  on(scopeFilter, "change", () => loadGraph().catch(showError));
  on(taskFilter, "change", () => loadGraph().catch(showError));
  on(providerFilter, "change", () => loadGraph().catch(showError));
  on(pinnedFilter, "change", () => loadGraph().catch(showError));
  on(groupFilter, "change", () => {
    graphState.groupFilter = groupFilter.value || "";
    graphState.physicsTicks = 0;
    graphState.physicsActive = true;
    applyGraphVisibility();
    seedGraphParticles();
    fitGraphToView();
    wakeGraphAnimation();
  });
  on(searchInput, "input", () => {
    graphState.searchQuery = searchInput.value || "";
    graphState.physicsTicks = 0;
    graphState.physicsActive = true;
    // Local filter for already-loaded nodes (label/text), then debounced backend
    // fetch so ID search can find nodes outside the density budget.
    applyGraphVisibility();
    seedGraphParticles();
    fitGraphToView();
    wakeGraphAnimation();
    clearTimeout(graphState.searchTimer);
    graphState.searchTimer = window.setTimeout(() => {
      loadGraph().catch(showError);
    }, 280);
  });
  on(searchInput, "keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    clearTimeout(graphState.searchTimer);
    loadGraph().catch(showError);
  });
  if (densityInput) {
    const densityValue = document.querySelector("#graph-density-value");
    const syncDensityBadge = () => { if (densityValue) densityValue.textContent = String(densityInput.value || "2"); };
    syncDensityBadge();
    on(densityInput, "input", syncDensityBadge);
    on(densityInput, "change", () => { syncDensityBadge(); loadGraph().catch(showError); });
  }
  on(fitButton, "click", () => {
    graphState.userZoomed = false;
    fitGraphToView();
  });
  on(expandButton, "click", () => toggleGraphExpand(true));
  on("#graph-fit-expanded", "click", () => {
    graphState.userZoomed = false;
    fitGraphToView();
  });
  on("#graph-close-expand", "click", () => toggleGraphExpand(false));
  document.querySelectorAll("[data-close-graph-node-modal]").forEach(el => {
    el.addEventListener("click", () => closeGraphNodeModal());
  });
  on(rebuildButton, "click", async () => {
    const summary = document.querySelector("#graph-link-summary");
    if (summary) {
      summary.hidden = false;
      summary.textContent = "linking...";
    }
    const payload = await api("/api/graph/rebuild-links", { method: "POST", body: JSON.stringify({ project_id: state.projectId }) });
    if (summary) {
      summary.hidden = false;
      summary.textContent = `${payload.created} new link(s), ${payload.linked_nodes} node(s) touched`;
    }
    await loadGraph();
  });
  canvas.addEventListener("pointerdown", event => {
    const hit = hitGraphNode(event);
    if (hit) {
      graphState.draggingId = hit.id;
      graphState.dragMoved = false;
      selectGraphNode(hit.node, { openModal: false });
      canvas.setPointerCapture(event.pointerId);
      wakeGraphAnimation();
    }
  });
  canvas.addEventListener("pointermove", event => {
    const hit = hitGraphNode(event);
    const nextHoverId = hit ? hit.id : "";
    if (nextHoverId !== graphState.hoverId) {
      graphState.hoverId = nextHoverId;
      wakeGraphAnimation();
    }
    if (graphState.draggingId) {
      const point = graphPointer(event);
      const particle = graphState.particles.find(item => item.id === graphState.draggingId);
      if (particle) {
        const dx = point.x - particle.x;
        const dy = point.y - particle.y;
        if (Math.hypot(dx, dy) > 2) graphState.dragMoved = true;
        particle.x = point.x;
        particle.y = point.y;
        particle.vx = 0;
        particle.vy = 0;
      }
      wakeGraphAnimation();
    }
  });
  canvas.addEventListener("pointerleave", () => {
    graphState.hoverId = "";
    wakeGraphAnimation();
  });
  canvas.addEventListener("pointerup", event => {
    graphState.draggingId = "";
    try { canvas.releasePointerCapture(event.pointerId); } catch (_) {}
  });
  canvas.addEventListener("dblclick", event => {
    event.preventDefault();
    if (graphState.dragMoved) return;
    const hit = hitGraphNode(event);
    if (hit) openGraphNodeModal(hit.node);
  });
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    const canvasEl = document.querySelector("#graph-canvas");
    if (!canvasEl) return;
    const rect = canvasEl.getBoundingClientRect();
    const ratioX = canvasEl.width / Math.max(rect.width, 1);
    const ratioY = canvasEl.height / Math.max(rect.height, 1);
    const screenX = (event.clientX - rect.left) * ratioX;
    const screenY = (event.clientY - rect.top) * ratioY;
    const worldX = (screenX - graphState.offsetX) / Math.max(graphState.scale, 0.001);
    const worldY = (screenY - graphState.offsetY) / Math.max(graphState.scale, 0.001);
    const direction = event.deltaY > 0 ? -0.08 : 0.08;
    const next = Math.max(0.2, Math.min(2.8, graphState.scale * (1 + direction)));
    graphState.scale = next;
    graphState.offsetX = screenX - worldX * next;
    graphState.offsetY = screenY - worldY * next;
    graphState.userZoomed = true;
    wakeGraphAnimation();
  }, { passive: false });
  window.addEventListener("resize", () => {
    resizeGraphCanvas();
    if (!graphState.userZoomed) fitGraphToView();
    wakeGraphAnimation();
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (graphState.modalOpen) {
      closeGraphNodeModal();
      event.preventDefault();
      return;
    }
    if (graphState.expanded) toggleGraphExpand(false);
  });
  observeGraphStage();
}

function toggleGraphExpand(open) {
  const overlay = document.querySelector("#graph-expand-overlay");
  const canvas = document.querySelector("#graph-canvas");
  const normalStage = document.querySelector(".memory-graph-section .graph-stage");
  const expandStage = document.querySelector(".graph-expand-stage");
  if (!overlay || !canvas || !normalStage || !expandStage) return;
  graphState.expanded = open;
  if (open) {
    expandStage.appendChild(canvas);
    overlay.removeAttribute("hidden");
    document.body.style.overflow = "hidden";
  } else {
    normalStage.appendChild(canvas);
    overlay.setAttribute("hidden", "");
    document.body.style.overflow = "";
  }
  resizeGraphCanvas();
  graphState.userZoomed = false;
  fitGraphToView();
}

function resizeGraphCanvas() {
  const canvas = document.querySelector("#graph-canvas");
  const stage = canvas?.closest(".graph-stage, .graph-expand-stage");
  if (!canvas || !stage) return;
  const rect = stage.getBoundingClientRect();
  if (rect.width < 8 || rect.height < 8) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(320, Math.floor(rect.width * ratio));
  const height = Math.max(240, Math.floor(rect.height * ratio));
  if (canvas.width === width && canvas.height === height) return;
  canvas.width = width;
  canvas.height = height;
  if (graphState.particles.length && !graphState.userZoomed) {
    // Keep world layout; only reseed when empty.
  } else if (!graphState.particles.length) {
    seedGraphParticles();
  }
}

function observeGraphStage() {
  if (graphState.stageObserver) return;
  graphState.stageObserver = new ResizeObserver(() => {
    resizeGraphCanvas();
    if (!graphState.userZoomed) fitGraphToView();
  });
  const normal = document.querySelector(".memory-graph-section .graph-stage");
  const expanded = document.querySelector(".graph-expand-stage");
  if (normal) graphState.stageObserver.observe(normal);
  if (expanded) graphState.stageObserver.observe(expanded);
}

function sanitizeGraphParticles() {
  let broken = false;
  for (const particle of graphState.particles) {
    if (![particle.x, particle.y, particle.vx, particle.vy, particle.z].every(Number.isFinite)) {
      broken = true;
      break;
    }
  }
  if (broken) {
    seedGraphParticles();
    return true;
  }
  return false;
}

function fitGraphToView() {
  resizeGraphCanvas();
  const canvas = document.querySelector("#graph-canvas");
  if (!canvas || !graphState.particles.length) {
    graphState.scale = 1;
    graphState.offsetX = 0;
    graphState.offsetY = 0;
    if (!graphState.particles.length) seedGraphParticles();
    return;
  }
  sanitizeGraphParticles();
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const particle of graphState.particles) {
    const pad = graphNodeRadius(particle) + 14;
    minX = Math.min(minX, particle.x - pad);
    maxX = Math.max(maxX, particle.x + pad);
    minY = Math.min(minY, particle.y - pad);
    maxY = Math.max(maxY, particle.y + pad + 12);
  }
  if (![minX, minY, maxX, maxY].every(Number.isFinite)) {
    seedGraphParticles();
    graphState.scale = 1;
    graphState.offsetX = 0;
    graphState.offsetY = 0;
    return;
  }
  const graphW = Math.max(1, maxX - minX);
  const graphH = Math.max(1, maxY - minY);
  const dpr = window.devicePixelRatio || 1;
  // Keep a slim margin so the graph fills most of the stage but still fits.
  const padding = 12 * dpr;
  const availW = Math.max(1, canvas.width - padding * 2);
  const availH = Math.max(1, canvas.height - padding * 2);
  const fitScale = Math.min(availW / graphW, availH / graphH, 2.6);
  const scale = Math.max(0.12, Number.isFinite(fitScale) ? fitScale : 1);
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  graphState.scale = scale;
  graphState.offsetX = canvas.width / 2 - centerX * scale;
  graphState.offsetY = canvas.height / 2 - centerY * scale;
  if (![graphState.scale, graphState.offsetX, graphState.offsetY].every(Number.isFinite)) {
    graphState.scale = 1;
    graphState.offsetX = 0;
    graphState.offsetY = 0;
  }
}

function graphPointer(event) {
  const canvas = document.querySelector("#graph-canvas");
  const rect = canvas.getBoundingClientRect();
  const ratioX = canvas.width / Math.max(rect.width, 1);
  const ratioY = canvas.height / Math.max(rect.height, 1);
  const screenX = (event.clientX - rect.left) * ratioX;
  const screenY = (event.clientY - rect.top) * ratioY;
  return {
    x: (screenX - graphState.offsetX) / Math.max(graphState.scale, 0.001),
    y: (screenY - graphState.offsetY) / Math.max(graphState.scale, 0.001),
  };
}

function hitGraphNode(event) {
  const point = graphPointer(event);
  for (const particle of [...graphState.particles].reverse()) {
    const radius = graphNodeRadius(particle);
    const dx = point.x - particle.x;
    const dy = point.y - particle.y;
    if (Math.sqrt(dx * dx + dy * dy) <= radius + 6) return particle;
  }
  return null;
}

function startGraphAnimation() {
  cancelAnimationFrame(graphState.animationId);
  graphState.animationId = 0;
  resizeGraphCanvas();
  const tick = () => {
    graphState.animationId = 0;
    // MF0-style cooldown: simulate briefly, then freeze so the view stays readable.
    if (graphState.physicsActive || graphState.draggingId) {
      stepGraphPhysics();
      if (!graphState.draggingId) {
        graphState.physicsTicks += 1;
        if (graphState.physicsTicks >= graphState.physicsMax) {
          graphState.physicsActive = false;
          if (!graphState.userZoomed) fitGraphToView();
        }
      }
    }
    drawGraph();
    // Keep ticking only while there is visible work: active physics, a drag,
    // or a hovered node. Otherwise the canvas is static — no idle CPU burn.
    if (graphState.physicsActive || graphState.draggingId || graphState.hoverId) {
      graphState.animationId = requestAnimationFrame(tick);
    }
  };
  tick();
}

function wakeGraphAnimation() {
  if (!graphState.animationId) startGraphAnimation();
}

function stepGraphPhysics() {
  const canvas = document.querySelector("#graph-canvas");
  if (!canvas) return;
  const particles = graphState.particles;
  if (!particles.length) return;
  sanitizeGraphParticles();
  const centerX = canvas.width / 2;
  const centerY = canvas.height / 2;
  const byId = new Map(particles.map(item => [item.id, item]));
  const n = particles.length;
  // MF0 adaptive charge: consistent density across graph sizes.
  const charge = -140 * Math.sqrt(Math.max(1, n) / 100);
  const linkDistance = n > 80 ? 90 : 110;
  if (n <= 140) {
    for (let i = 0; i < n; i++) {
      const a = particles[i];
      for (let j = i + 1; j < n; j++) {
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distance = Math.max(24, Math.sqrt(dx * dx + dy * dy));
        const force = Math.abs(charge) / (distance * distance);
        const fx = (dx / distance) * force;
        const fy = (dy / distance) * force;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
  } else {
    const windowSize = 16;
    for (let i = 0; i < n; i++) {
      const a = particles[i];
      const limit = Math.min(n, i + 1 + windowSize);
      for (let j = i + 1; j < limit; j++) {
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distance = Math.max(24, Math.sqrt(dx * dx + dy * dy));
        const force = Math.abs(charge) * 0.55 / (distance * distance);
        const fx = (dx / distance) * force;
        const fy = (dy / distance) * force;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
  }
  for (const edge of graphState.edges) {
    const a = byId.get(edge.source);
    const b = byId.get(edge.target);
    if (!a || !b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy));
    if (!Number.isFinite(distance)) continue;
    const force = (distance - linkDistance) * 0.01;
    const fx = (dx / distance) * force;
    const fy = (dy / distance) * force;
    a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
  }
  // MF0 uses velocity decay ~0.22; here we apply per tick after integration.
  const damp = 0.78;
  for (const particle of particles) {
    if (particle.id !== graphState.draggingId) {
      particle.vx += (centerX - particle.x) * 0.00045;
      particle.vy += (centerY - particle.y) * 0.00045;
      particle.x += particle.vx;
      particle.y += particle.vy;
      particle.vx *= damp;
      particle.vy *= damp;
    }
    if (!Number.isFinite(particle.x) || !Number.isFinite(particle.y)) {
      particle.x = centerX;
      particle.y = centerY;
      particle.vx = 0;
      particle.vy = 0;
    } else {
      const span = Math.max(canvas.width, canvas.height) * 4;
      particle.x = Math.max(centerX - span, Math.min(centerX + span, particle.x));
      particle.y = Math.max(centerY - span, Math.min(centerY + span, particle.y));
      particle.vx = Math.max(-28, Math.min(28, particle.vx));
      particle.vy = Math.max(-28, Math.min(28, particle.vy));
    }
    particle.z = Math.max(-1, Math.min(1, Number.isFinite(particle.z) ? particle.z : 0));
  }
}

function isDarkTheme() { return document.documentElement.dataset.theme === "dark"; }
function graphColors() {
  if (isDarkTheme()) {
    return {
      bg0: "#172033", bg1: "#090d17", grid: "rgba(148, 163, 184, 0.08)",
      edge: "rgba(148, 163, 184, 0.24)", edgeActive: "rgba(125, 211, 252, 0.86)",
      nodeStroke: "rgba(255,255,255,0.72)", nodeStrokeActive: "#e0f2fe",
      nodeShadow: "rgba(15, 23, 42, 0.55)", nodeShadowActive: "rgba(125, 211, 252, 0.78)",
      label: "#f8fafc", labelShadow: "rgba(0,0,0,0.55)",
    };
  }
  return {
    bg0: "#ffffff", bg1: "#e7ecf6", grid: "rgba(71, 85, 105, 0.09)",
    edge: "rgba(71, 85, 105, 0.30)", edgeActive: "rgba(99, 102, 241, 0.85)",
    nodeStroke: "rgba(255,255,255,0.95)", nodeStrokeActive: "#6366f1",
    nodeShadow: "rgba(15, 23, 42, 0.20)", nodeShadowActive: "rgba(99, 102, 241, 0.5)",
    label: "#1e293b", labelShadow: "rgba(255,255,255,0.85)",
  };
}

function drawGraph() {
  const canvas = document.querySelector("#graph-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const colors = graphColors();
  const scale = Math.max(graphState.scale, 0.001);
  const inv = 1 / scale;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const gradient = ctx.createRadialGradient(canvas.width * 0.52, canvas.height * 0.45, 20, canvas.width * 0.5, canvas.height * 0.5, canvas.width * 0.75);
  gradient.addColorStop(0, colors.bg0);
  gradient.addColorStop(1, colors.bg1);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  drawGraphGrid(ctx, canvas, colors);
  if (![scale, graphState.offsetX, graphState.offsetY].every(Number.isFinite)) return;
  ctx.setTransform(scale, 0, 0, scale, graphState.offsetX, graphState.offsetY);
  const byId = new Map(graphState.particles.map(item => [item.id, item]));
  for (const edge of graphState.edges) {
    const a = byId.get(edge.source);
    const b = byId.get(edge.target);
    if (!a || !b) continue;
    const active = graphState.selectedId && (edge.source === graphState.selectedId || edge.target === graphState.selectedId);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.strokeStyle = active ? colors.edgeActive : colors.edge;
    // Keep strokes screen-constant so zoom/fit never produces a thick blur smear.
    ctx.lineWidth = (active ? 2.2 : 0.9) * inv;
    ctx.stroke();
  }
  for (const particle of [...graphState.particles].sort((a, b) => a.z - b.z)) {
    drawGraphNode(ctx, particle, colors, inv);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}

function drawGraphGrid(ctx, canvas, colors) {
  ctx.save();
  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  const gap = 64;
  for (let x = canvas.width % gap; x < canvas.width; x += gap) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height); ctx.stroke();
  }
  for (let y = canvas.height % gap; y < canvas.height; y += gap) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); ctx.stroke();
  }
  ctx.restore();
}

function graphNodeRadius(particle) {
  const base = particle.node.type === "Project" ? 18 : particle.node.pinned ? 14 : 11;
  return base + particle.z * 2;
}

function drawGraphNode(ctx, particle, colors, inv = 1) {
  colors = colors || graphColors();
  const node = particle.node;
  const radius = graphNodeRadius(particle);
  const color = node.color || graphGroupColor(node.group) || graphPalette[node.type] || "#64748b";
  const active = particle.id === graphState.selectedId;
  const hover = particle.id === graphState.hoverId;
  ctx.save();
  if (active || hover) {
    ctx.shadowColor = active ? colors.nodeShadowActive : colors.nodeShadow;
    ctx.shadowBlur = (active ? 14 : 8) * inv;
  }
  ctx.beginPath();
  ctx.arc(particle.x, particle.y, radius + (hover ? 2 : 0), 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.globalAlpha = 0.9;
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.lineWidth = (active ? 3 : 1.5) * inv;
  ctx.strokeStyle = active ? colors.nodeStrokeActive : colors.nodeStroke;
  ctx.stroke();
  const denseGraph = graphState.nodes.length > 55;
  const showLabel = !denseGraph || active || hover || node.type === "Project" || node.pinned;
  if (showLabel) {
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.fillStyle = colors.label;
    ctx.font = `${Math.max(10, 11 * Math.min(inv, 1.4))}px Segoe UI, Arial`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const label = node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label;
    ctx.fillText(label, particle.x, particle.y + radius + 5);
  }
  ctx.restore();
}

function renderGraphLegend() {
  const legend = document.querySelector("#graph-legend");
  const groupFilter = document.querySelector("#graph-group-filter");
  if (!legend) return;
  const groups = [...new Set(graphState.allNodes.map(node => node.group || "Other"))].sort();
  // Always offer Projects when a project root exists in the unfiltered payload,
  // so the Groups → Projects control does not disappear under density/source filters.
  if (!groups.includes("Projects") && graphState.allNodes.some(node => node.type === "Project")) {
    groups.push("Projects");
    groups.sort();
  }
  legend.innerHTML = groups.map(group => `<span class="graph-legend-item"><span class="graph-legend-dot" style="background:${escapeHtml(graphGroupColor(group))}"></span>${escapeHtml(group)}</span>`).join("");
  if (groupFilter) {
    const current = graphState.groupFilter || groupFilter.value || "";
    const known = new Set(groups);
    if (current && !known.has(current)) known.add(current);
    const options = ['<option value="">All groups</option>']
      .concat([...known].sort().map(group => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`));
    groupFilter.innerHTML = options.join("");
    if (current && [...groupFilter.options].some(opt => opt.value === current)) {
      groupFilter.value = current;
      graphState.groupFilter = current;
    } else {
      groupFilter.value = "";
      graphState.groupFilter = "";
    }
  }
}

async function syncGraphFilters() {
  const taskFilter = document.querySelector("#graph-task-filter");
  const providerFilter = document.querySelector("#graph-provider-filter");
  if (!taskFilter || !providerFilter || taskFilter.dataset.ready) return;
  const [tasksPayload, providersPayload] = await Promise.all([api(`/api/tasks?project_id=${projectParam()}`), api("/api/providers")]);
  taskFilter.innerHTML = '<option value="">All tasks</option>' + tasksPayload.tasks.map(task => `<option value="${escapeHtml(task.id)}">${escapeHtml(task.title)}</option>`).join("");
  providerFilter.innerHTML = '<option value="">All providers</option>' + providersPayload.providers.map(provider => `<option value="${escapeHtml(provider.id)}">${escapeHtml(provider.label)}</option>`).join("");
  taskFilter.dataset.ready = "1";
}

function graphNodeOptions(excludeId = "") {
  const source = graphState.allNodes.length ? graphState.allNodes : graphState.nodes;
  return source
    .filter(node => node.id !== excludeId && !(node.metadata && node.metadata.synthetic))
    .map(node => `<option value="${escapeHtml(node.id)}">${escapeHtml(node.label)}</option>`)
    .join("");
}

function graphEdgeLabel(type) {
  return ({
    HAS_MEMORY: "Has memory",
    DOCUMENTED_IN: "Documents",
    IMPLEMENTS: "Implements",
    RELATED_TO: "Related",
    SUPPORTS: "Supports",
    DEPENDS_ON: "Depends on",
    DECIDED_IN: "Decided in",
    TASK_LINK: "Task link",
    PROVIDER_RELATED: "Provider related",
  })[type] || String(type || "Related").replaceAll("_", " ").toLowerCase().replace(/^./, char => char.toUpperCase());
}

function graphEdgeDirection(edge, selectedNode, otherNode) {
  if (edge.source === selectedNode.id) return `${escapeHtml(selectedNode.label)} -> ${escapeHtml(otherNode.label)}`;
  return `${escapeHtml(otherNode.label)} -> ${escapeHtml(selectedNode.label)}`;
}

function looksLikeProjectPath(value) {
  const text = String(value || "").trim();
  if (!text) return false;
  if (/^https?:\/\//i.test(text)) return false;
  if (text.startsWith("evidence/")) return false;
  return /[./\\]/.test(text) || /\.[a-z0-9]{1,8}$/i.test(text);
}

function graphNodeRelatedFiles(node) {
  const seen = new Set();
  const files = [];
  const push = (raw, kind) => {
    const value = String(raw || "").trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    files.push({ path: value, kind, openable: looksLikeProjectPath(value) && !value.startsWith("evidence/") });
  };
  const meta = node?.metadata || {};
  for (const key of ["path", "relative_path", "source_ref", "file", "source_path"]) {
    if (meta[key]) push(meta[key], key === "source_ref" ? "source" : "path");
  }
  for (const item of node?.evidence || []) push(item, "evidence");
  if (looksLikeProjectPath(node?.label)) push(node.label, "artifact");
  return files;
}

function isGraphNodeModalOpen() {
  const modal = document.querySelector("#graph-node-modal");
  return Boolean(modal && !modal.hasAttribute("hidden"));
}

function openGraphNodeModal(node) {
  if (!node) return;
  const prevSelected = graphState.selectedId;
  graphState.selectedId = node.id;
  graphState.physicsActive = true;
  graphState.physicsTicks = Math.min(graphState.physicsTicks, Math.floor(graphState.physicsMax * 0.6));
  if (prevSelected !== node.id || !graphState.particles.some(item => item.id === node.id)) {
    applyGraphVisibility();
    seedGraphParticles();
    graphState.physicsTicks = 0;
    graphState.physicsActive = true;
    graphState.userZoomed = false;
  }
  wakeGraphAnimation();
  renderGraphDetail(node);
  const modal = document.querySelector("#graph-node-modal");
  if (!modal) return;
  modal.removeAttribute("hidden");
  graphState.modalOpen = true;
  modal.querySelector(".modal-close")?.focus();
}

function closeGraphNodeModal() {
  const modal = document.querySelector("#graph-node-modal");
  if (modal) modal.setAttribute("hidden", "");
  graphState.modalOpen = false;
}

function selectGraphNode(node, options = {}) {
  if (!node) {
    graphState.selectedId = "";
    return;
  }
  const prevSelected = graphState.selectedId;
  graphState.selectedId = node.id;
  graphState.physicsActive = true;
  graphState.physicsTicks = Math.min(graphState.physicsTicks, Math.floor(graphState.physicsMax * 0.6));
  if (prevSelected !== node.id || !graphState.particles.some(item => item.id === node.id)) {
    applyGraphVisibility();
    seedGraphParticles();
    graphState.physicsTicks = 0;
    graphState.physicsActive = true;
    graphState.userZoomed = false;
  }
  wakeGraphAnimation();
  if (options.openModal) {
    openGraphNodeModal(node);
    return;
  }
  if (isGraphNodeModalOpen()) renderGraphDetail(node);
}

function renderGraphSelection(node) {
  if (!node) {
    graphState.selectedId = "";
    return;
  }
  selectGraphNode(node, { openModal: false });
}

function renderGraphDetail(node) {
  const title = document.querySelector("#graph-detail-title");
  const text = document.querySelector("#graph-detail-text");
  const meta = document.querySelector("#graph-detail-meta");
  const actions = document.querySelector("#graph-actions");
  const neighbors = document.querySelector("#graph-neighbors");
  const filesEl = document.querySelector("#graph-related-files");
  if (!title || !text || !meta || !actions || !neighbors) return;
  if (!node) {
    title.textContent = "No nodes";
    text.textContent = "Scan or add memory to populate the graph.";
    meta.innerHTML = "";
    actions.innerHTML = "";
    neighbors.innerHTML = "";
    if (filesEl) filesEl.innerHTML = "";
    return;
  }
  title.textContent = node.label;
  text.textContent = node.text || "No description for this memory node.";
  const synthetic = Boolean(node.metadata && node.metadata.synthetic);
  const pinned = Boolean(node.metadata && (node.metadata.favorite || node.metadata.pinned));
  const edgeSource = graphState.allEdges.length ? graphState.allEdges : graphState.edges;
  const nodeSource = graphState.allNodes.length ? graphState.allNodes : graphState.nodes;
  const linked = edgeSource.filter(edge => edge.source === node.id || edge.target === node.id);
  meta.innerHTML = `<span class="badge">${escapeHtml(node.type)}</span><span class="badge">${escapeHtml(node.scope)}</span><span class="badge">${linked.length} links</span>${pinned ? '<span class="badge">pinned</span>' : ""}`;
  const options = graphNodeOptions(node.id);
  actions.innerHTML = synthetic
    ? '<div class="provider-test">Synthetic filter nodes cannot be edited.</div>'
    : `<div class="provider-actions graph-node-primary-actions"><button data-graph-open="${escapeHtml(node.id)}" type="button">Open in Search</button><button data-graph-pin="${escapeHtml(node.id)}" type="button">${pinned ? "Unpin" : "Pin"}</button></div><details class="graph-node-advanced"><summary>Advanced edges</summary><label>Target<select data-graph-target><option value="">Select node</option>${options}</select></label><label>Edge<select data-graph-edge-type><option>RELATED_TO</option><option>SUPPORTS</option><option>DEPENDS_ON</option><option>IMPLEMENTS</option><option>DOCUMENTED_IN</option></select></label><div class="provider-actions"><button data-graph-edge="${escapeHtml(node.id)}" type="button">Create Edge</button><button data-graph-path="${escapeHtml(node.id)}" type="button">Explain Path</button><button data-graph-merge="${escapeHtml(node.id)}" type="button">Merge Into Target</button></div></details><div class="provider-test" data-graph-action-result></div>`;
  bindGraphActions(actions, node);
  neighbors.innerHTML = linked.length
    ? ""
    : '<div class="result"><strong>No linked memory</strong><p>Use Rebuild Links or create an edge.</p></div>';
  const byId = new Map(nodeSource.map(item => [item.id, item]));
  for (const edge of linked) {
    const other = byId.get(edge.source === node.id ? edge.target : edge.source);
    if (!other) continue;
    const item = document.createElement("article");
    item.className = "result graph-link-card";
    item.tabIndex = 0;
    const label = graphEdgeLabel(edge.type);
    const direction = graphEdgeDirection(edge, node, other);
    const confidence = edge.confidence ? Math.round(Number(edge.confidence) * 100) : 0;
    item.innerHTML = `<div class="row"><strong>${escapeHtml(other.label)}</strong><span class="badge">${escapeHtml(label)}</span></div><p>${direction}</p><span class="badge">${escapeHtml(other.type)}</span><span class="badge">${escapeHtml(other.scope)}</span>${confidence ? `<span class="badge">${confidence}% confidence</span>` : ""}`;
    const openLinked = () => openGraphNodeModal(other);
    item.addEventListener("click", openLinked);
    item.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openLinked();
      }
    });
    neighbors.appendChild(item);
  }
  if (filesEl) {
    const files = graphNodeRelatedFiles(node);
    if (!files.length) {
      filesEl.innerHTML = '<div class="result"><strong>No related files</strong><p>Evidence paths and source refs will show up here when available.</p></div>';
    } else {
      filesEl.innerHTML = files.map(file => {
        const kind = file.kind === "evidence" ? "Evidence" : file.kind === "source" ? "Source" : "File";
        if (file.openable) {
          return `<button type="button" class="graph-file-row" data-graph-file="${escapeHtml(file.path)}"><span class="badge">${kind}</span><span class="graph-file-path">${escapeHtml(file.path)}</span></button>`;
        }
        return `<div class="graph-file-row is-static"><span class="badge">${kind}</span><span class="graph-file-path">${escapeHtml(file.path)}</span></div>`;
      }).join("");
      filesEl.querySelectorAll("[data-graph-file]").forEach(button => {
        button.addEventListener("click", () => {
          const path = button.getAttribute("data-graph-file");
          if (!path) return;
          closeGraphNodeModal();
          switchView("workspace");
          if (typeof fileEditor !== "undefined" && fileEditor?.openFile) {
            fileEditor.openFile(path).catch(showError);
          }
        });
      });
    }
  }
}

function bindGraphActions(container, node) {
  const result = container.querySelector("[data-graph-action-result]");
  const targetSelect = container.querySelector("[data-graph-target]");
  const edgeType = container.querySelector("[data-graph-edge-type]");
  const setResult = (message, ok = true) => { if (result) { result.className = `provider-test ${ok ? "ok" : "error"}`; result.textContent = message; } };
  const open = container.querySelector("[data-graph-open]");
  if (open) open.addEventListener("click", () => {
    closeGraphNodeModal();
    switchView("memory");
    setElementValue("#search-query", node.label);
    runSearch(node.label).catch(showError);
  });
  const pin = container.querySelector("[data-graph-pin]");
  if (pin) pin.addEventListener("click", async () => { await api(`/api/graph/nodes/${node.id}/pin`, { method: "POST", body: "{}" }); await loadGraph(); });
  const createEdge = container.querySelector("[data-graph-edge]");
  if (createEdge) createEdge.addEventListener("click", async () => {
    if (!targetSelect.value) return setResult("Select a target node.", false);
    await api("/api/graph/edges", { method: "POST", body: JSON.stringify({ source: node.id, target: targetSelect.value, type: edgeType.value, scope: node.scope }) });
    setResult("Edge created.");
    await loadGraph();
  });
  const explain = container.querySelector("[data-graph-path]");
  if (explain) explain.addEventListener("click", async () => {
    if (!targetSelect.value) return setResult("Select a target node.", false);
    const payload = await api(`/api/graph/path?source=${encodeURIComponent(node.id)}&target=${encodeURIComponent(targetSelect.value)}`);
    setResult(payload.explanation, payload.found);
  });
  const merge = container.querySelector("[data-graph-merge]");
  if (merge) merge.addEventListener("click", async () => {
    if (!targetSelect.value) return setResult("Select a target node.", false);
    await api(`/api/graph/nodes/${node.id}/merge`, { method: "POST", body: JSON.stringify({ target_id: targetSelect.value }) });
    setResult("Node merged.");
    graphState.selectedId = targetSelect.value;
    await loadGraph();
  });
}
function providerStatusLabel(status, enabled) {
  const tone = providerStatusClass(status, enabled);
  const labels = {
    ready: t("providers.status.ready"),
    error: t("providers.status.error"),
    disabled: t("providers.status.disabled"),
    planned: t("providers.status.planned"),
  };
  return labels[tone] || tone;
}
function renderProvidersStatus(providers) {
  const message = document.querySelector("#providers-status-message");
  const badge = document.querySelector("#providers-status-badge");
  if (!providers.length) {
    if (message) message.textContent = t("providers.status.empty");
    if (badge) { badge.textContent = t("providers.readiness.idle"); badge.dataset.tone = "idle"; }
    return;
  }
  const ready = providers.filter(provider => providerStatusClass(provider.status, provider.enabled) === "ready").length;
  const enabled = providers.filter(provider => provider.enabled).length;
  if (message) {
    message.textContent = t("providers.status.summary")
      .replace("{ready}", String(ready))
      .replace("{enabled}", String(enabled))
      .replace("{total}", String(providers.length));
  }
  if (badge) {
    if (ready === providers.length) {
      badge.textContent = t("providers.readiness.ready");
      badge.dataset.tone = "ok";
    } else if (ready > 0) {
      badge.textContent = t("providers.readiness.partial");
      badge.dataset.tone = "warn";
    } else {
      badge.textContent = t("providers.readiness.idle");
      badge.dataset.tone = "idle";
    }
  }
}
async function connectEnvProviders() {
  const summary = document.querySelector("#provider-summary");
  if (summary) summary.textContent = "connecting environment providers...";
  const payload = await api("/api/providers/connect-env", { method: "POST", body: "{}" });
  const missing = (payload.missing || []).map(item => item.api_key_env).join(", ");
  if (summary) summary.textContent = `${payload.connected.length} connected${missing ? `; missing ${missing}` : ""}`;
  showSnackbar(payload.message || "Environment providers checked.", payload.connected.length ? "success" : "info");
  await loadProviders();
  await loadCouncil();
}
async function testAllProviders() {
  const summary = document.querySelector("#provider-summary");
  if (summary) summary.textContent = "checking providers...";
  const payload = await api("/api/providers/test-all", { method: "POST", body: "{}" });
  const ready = payload.checks.filter(check => check.provider.ready).length;
  if (summary) summary.textContent = `${ready}/${payload.checks.length} ready`;
  await loadProviders();
}
function buildProviderCard(provider) {
  const command = Array.isArray(provider.command) ? provider.command.join(" ") : (provider.command || "");
  const statusClass = providerStatusClass(provider.status, provider.enabled);
  const statusLabel = providerStatusLabel(provider.status, provider.enabled);
  const lastCheck = provider.last_check ? `<span class="provider-last-check">last check: ${escapeHtml(provider.last_check.status)} at ${escapeHtml(provider.last_check.checked_at || "")}</span>` : "";
  const modelOptions = (provider.available_models || []).map(model => `<option value="${escapeHtml(model.name)}">${escapeHtml(model.name)}</option>`).join("");
  const hasModelPicker = provider.id === "ollama" || (provider.id === "openrouter" && provider.available_models && provider.available_models.length);
  const modelControl = hasModelPicker
    ? `<label>Model<select data-model="${escapeHtml(provider.id)}"><option value="">default</option>${modelOptions}</select></label>`
    : `<label>Model<input data-model="${escapeHtml(provider.id)}" value="${escapeHtml(provider.model)}" placeholder="model"></label>`;
  const modelMeta = ["ollama", "openrouter"].includes(provider.id) && provider.models_checked_at ? `<span class="provider-last-check">models checked: ${escapeHtml(provider.models_checked_at)}</span>` : "";
  const modelActions = ["ollama", "openrouter"].includes(provider.id) ? `<button data-refresh-models="${escapeHtml(provider.id)}" type="button">Refresh Models</button><span class="provider-test" data-model-result></span>` : "";
  const loginLabel = providerLoginLabel(provider);
  const loginAction = loginLabel
    ? `<button data-login-provider="${escapeHtml(provider.id)}" type="button" class="btn-secondary">${escapeHtml(loginLabel)}</button>`
    : "";
  return `<div class="provider-head"><div><strong>${escapeHtml(provider.label)}</strong><p>${escapeHtml(provider.provider_type)} · ${escapeHtml(statusLabel)}</p></div><span class="provider-status ${statusClass}">${escapeHtml(statusLabel)}</span></div><p class="provider-hint">${escapeHtml(providerHint(provider))}</p>${lastCheck}${modelMeta}<div class="provider-card-actions"><label class="inline-check"><input type="checkbox" data-provider="${escapeHtml(provider.id)}" ${provider.enabled ? "checked" : ""}> enabled</label><label class="inline-check"><input type="checkbox" data-approval-required="${escapeHtml(provider.id)}" ${provider.approval_required ? "checked" : ""}> require approval</label>${loginAction}<button data-test-provider="${escapeHtml(provider.id)}" type="button" class="btn-secondary">Test</button>${modelActions}<span class="provider-test" data-test-result></span></div><details class="provider-advanced"><summary>Configuration</summary><div class="provider-advanced-body">${modelControl}<label>Command<input data-command="${escapeHtml(provider.id)}" value="${escapeHtml(command)}" placeholder="command"></label><label>Workdir policy<select data-workdir-policy="${escapeHtml(provider.id)}"><option value="project-root">project-root</option><option value="custom">custom under project</option></select></label><label>Workdir<input data-workdir="${escapeHtml(provider.id)}" value="${escapeHtml(provider.workdir || "")}" placeholder="optional project subdirectory"></label><label>Base URL<input data-base-url="${escapeHtml(provider.id)}" value="${escapeHtml(provider.base_url || "")}" placeholder="http://127.0.0.1:11434"></label><label>API key env<input data-api-key-env="${escapeHtml(provider.id)}" value="${escapeHtml(provider.api_key_env || "")}" placeholder="${provider.id === "gemini-cli" ? "GEMINI_API_KEY" : "OPENAI_API_KEY"}"></label><label>Timeout<input data-timeout="${escapeHtml(provider.id)}" type="number" min="1" value="${escapeHtml(provider.timeout_seconds || 120)}"></label></div></details><div class="provider-actions-list" data-action-list></div>`;
}
async function loadProviders() {
  const payload = await api("/api/providers");
  syncChatProviderSelect(payload.providers);
  renderProvidersStatus(payload.providers);
  const list = document.querySelector("#provider-list");
  list.innerHTML = "";
  for (const provider of payload.providers) {
    const el = document.createElement("article");
    el.className = `provider provider-card-compact ${providerStatusClass(provider.status, provider.enabled)}`;
    el.innerHTML = buildProviderCard(provider);
    list.appendChild(el);
    const modelSelect = el.querySelector("select[data-model]");
    if (modelSelect) modelSelect.value = provider.model || "";
    const policy = el.querySelector("[data-workdir-policy]");
    if (policy) policy.value = provider.workdir_policy || "project-root";
  }
  list.querySelectorAll("[data-provider]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.provider}`, { method: "PATCH", body: JSON.stringify({ enabled: input.checked }) }); await loadProviders(); }));
  list.querySelectorAll("[data-model]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.model}`, { method: "PATCH", body: JSON.stringify({ model: input.value }) }); }));
  list.querySelectorAll("[data-command]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.command}`, { method: "PATCH", body: JSON.stringify({ command: input.value }) }); }));
  list.querySelectorAll("[data-approval-required]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.approvalRequired}`, { method: "PATCH", body: JSON.stringify({ approval_required: input.checked }) }); }));
  list.querySelectorAll("[data-workdir-policy]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.workdirPolicy}`, { method: "PATCH", body: JSON.stringify({ workdir_policy: input.value }) }); }));
  list.querySelectorAll("[data-workdir]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.workdir}`, { method: "PATCH", body: JSON.stringify({ workdir: input.value }) }); }));
  list.querySelectorAll("[data-base-url]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.baseUrl}`, { method: "PATCH", body: JSON.stringify({ base_url: input.value }) }); }));
  list.querySelectorAll("[data-api-key-env]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.apiKeyEnv}`, { method: "PATCH", body: JSON.stringify({ api_key_env: input.value }) }); }));
  list.querySelectorAll("[data-timeout]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.timeout}`, { method: "PATCH", body: JSON.stringify({ timeout_seconds: input.value }) }); }));
  list.querySelectorAll("[data-refresh-models]").forEach(button => button.addEventListener("click", async () => {
    const card = button.closest(".provider");
    const result = card.querySelector("[data-model-result]");
    result.className = "provider-test";
    result.textContent = "refreshing models...";
    try {
      const payload = await api(`/api/providers/${button.dataset.refreshModels}/models`);
      result.className = `provider-test ${payload.provider.ready ? "ok" : "error"}`;
      result.textContent = payload.hint ? `${payload.message} ${payload.hint}` : payload.message;
      await loadProviders();
    } catch (error) {
      result.className = "provider-test error";
      result.textContent = error.message;
    }
  }));
  list.querySelectorAll("[data-test-provider]").forEach(button => button.addEventListener("click", async () => {
    const card = button.closest(".provider");
    const result = card.querySelector("[data-test-result]");
    const actions = card.querySelector("[data-action-list]");
    result.className = "provider-test";
    result.textContent = "checking...";
    actions.innerHTML = "";
    try {
      const payload = await api(`/api/providers/${button.dataset.testProvider}/test`, { method: "POST", body: "{}" });
      result.className = `provider-test ${payload.provider.ready ? "ok" : "error"}`;
      result.textContent = payload.hint ? `${payload.message} ${payload.hint}` : payload.message;
      actions.innerHTML = (payload.actions || []).map(action => `<span class="badge">${escapeHtml(action)}</span>`).join("");
      await loadProviders();
    } catch (error) {
      result.className = "provider-test error";
      result.textContent = error.message;
    }
  }));
  list.querySelectorAll("[data-login-provider]").forEach(button => button.addEventListener("click", async () => {
    const card = button.closest(".provider");
    const result = card.querySelector("[data-test-result]");
    const actions = card.querySelector("[data-action-list]");
    result.className = "provider-test";
    result.textContent = "opening sign-in…";
    if (actions) actions.innerHTML = "";
    try {
      const payload = await api(`/api/providers/${button.dataset.loginProvider}/login`, { method: "POST", body: "{}" });
      result.className = `provider-test ${payload.provider?.ok ? "ok" : "error"}`;
      result.textContent = payload.hint ? `${payload.message} ${payload.hint}` : (payload.message || "Sign-in started.");
      showSnackbar(payload.message || "Sign-in started.", payload.provider?.ok ? "success" : "info");
    } catch (error) {
      result.className = "provider-test error";
      result.textContent = error.message;
    }
  }));
}
function isProviderSelectable(provider) {
  if (!provider || !provider.enabled) return false;
  if (provider.last_check && provider.last_check.ready) return true;
  return providerStatusClass(provider.status, provider.enabled) === "ready";
}
function syncChatProviderSelect(providers) {
  const selects = [document.querySelector("#chat-provider"), document.querySelector("#workspace-chat-provider")].filter(Boolean);
  if (!selects.length) return;
  const current = selects[0].value || "auto";
  const html = '<option value="auto">Auto route</option><option value="local-memory">Local memory</option>' + providers.map(provider => {
    const disabled = isProviderSelectable(provider) ? "" : " disabled";
    const suffix = disabled ? " (unavailable)" : "";
    return `<option value="${escapeHtml(provider.id)}"${disabled}>${escapeHtml(provider.label)}${escapeHtml(suffix)}</option>`;
  }).join("");
  for (const select of selects) {
    const keep = select.value || current;
    select.innerHTML = html;
    select.value = keep;
    if (!select.value || select.selectedOptions[0]?.disabled) select.value = "auto";
  }
  syncAskMode();
}
async function loadProviderRuns() {
  const container = document.querySelector("#provider-run-list");
  if (!container) return;
  const payload = await api(`/api/provider-runs?project_id=${projectParam()}&limit=12`);
  container.innerHTML = payload.runs.length ? "" : '<div class="result"><strong>No provider runs</strong><p>Run chat or AI routing to populate the audit trail.</p></div>';
  for (const run of payload.runs) {
    const el = document.createElement("article");
    el.className = "result";
    const usageLabel = formatUsageLabel(run.usage || {});
    const model = run.model || (run.selected_provider && run.selected_provider.model) || "";
    el.innerHTML = `<strong>${escapeHtml(run.provider_label || run.provider_id)}${model ? ` · ${escapeHtml(model)}` : ""}</strong><p>${escapeHtml(run.message_preview || "")}</p><span class="badge">${escapeHtml(run.status)}</span><span class="badge">${escapeHtml(run.updated_at || "")}</span>${usageLabel ? `<span class="badge">${escapeHtml(usageLabel)}</span>` : ""}${run.stderr_preview ? `<p>stderr: ${escapeHtml(run.stderr_preview)}</p>` : ""}`;
    container.appendChild(el);
  }
}
async function loadAnalytics() {
  const payload = await api(`/api/analytics?project_id=${projectParam()}`);
  const grids = [
    document.querySelector("#analytics-grid"),
    document.querySelector("#analytics-view-grid"),
  ].filter(Boolean);
  if (!grids.length) return;
  const life = payload.memory_lifecycle || {};
  const tier = life.by_tier || {};
  const stateCounts = life.by_state || {};
  const cov = (payload.embeddings && payload.embeddings.coverage) || {};
  const indexed = Number(cov.indexed || 0);
  const active = Number(cov.active_nodes || 0);
  const embValue = active ? `${indexed}/${active}` : String(indexed);
  const usage = payload.provider_usage || {};
  const totals = usage.totals || {};
  const byModel = Array.isArray(usage.by_model) ? usage.by_model : [];
  const tokenTotal = Number(totals.total_tokens || 0);
  const costTotal = totals.cost_usd != null ? Number(totals.cost_usd) : null;
  const metrics = [
    ["Nodes", payload.nodes],
    ["Edges", payload.edges],
    [t("memory.embeddingsMetric"), embValue],
    [t("usage.tokensMetric"), formatTokenCount(tokenTotal)],
    [t("usage.costMetric"), costTotal != null && costTotal > 0 ? formatUsageCost(costTotal) : "—"],
    ["Tasks", payload.tasks],
    ["Dialogs", payload.chats],
    ["Favorites", payload.favorites],
    ["Long-term", tier.long_term || 0],
    ["Short-term", tier.short_term || 0],
    ["Fresh", stateCounts.fresh || 0],
    ["Stale", stateCounts.stale || 0],
    ["Archived", stateCounts.archived || 0],
  ];
  const modelRows = byModel.slice(0, 8).map(row => {
    const name = `${row.provider_id || ""}${row.model ? ` / ${row.model}` : ""}`.replace(/^ \/ /, "");
    const tokens = formatTokenCount(row.total_tokens || 0);
    const cost = row.cost_usd != null ? formatUsageCost(row.cost_usd) : "—";
    return `<div class="usage-model-row"><strong>${escapeHtml(name || "unknown")}</strong><span>${escapeHtml(String(row.runs || 0))} runs · ${escapeHtml(tokens)} tok · ${escapeHtml(cost)}</span></div>`;
  }).join("");
  const html = [
    ...metrics.map(([label, value]) => `<article class="metric"><strong>${value}</strong><span>${label}</span></article>`),
    `<article class="metric metric-wide usage-by-model"><strong>${t("usage.byModel")}</strong><div class="usage-model-list">${modelRows || `<div class="usage-model-empty">${t("usage.empty")}</div>`}</div></article>`,
  ].join("");
  for (const grid of grids) grid.innerHTML = html;
}
async function loadSettings() {
  const settings = await api("/api/settings");
  const ui = settings.ui || {};
  const lifecycle = settings.memory_lifecycle || {};
  const workspace = settings.workspace || {};
  if (workspace.current_project_id) state.projectId = workspace.current_project_id;

  // Default to system theme if not set or on first load
  const theme = ui.theme === "dark" ? "system" : (ui.theme || "system");

  const themeSelect = document.querySelector("#theme-select");
  const densitySelect = document.querySelector("#density-select");
  const languageSelect = document.querySelector("#settings-language-select");
  const memoryEnabled = document.querySelector("#memory-enabled");
  const lifecycleEnabled = document.querySelector("#lifecycle-enabled");
  const refreshOnAccess = document.querySelector("#refresh-on-access");
  const autoRescanOnStartup = document.querySelector("#auto-rescan-on-startup");
  const chatMemoryMode = document.querySelector("#chat-memory-mode");
  const chatCandidateTtl = document.querySelector("#chat-candidate-ttl");
  const chatStoreFactsOnly = document.querySelector("#chat-store-facts-only");
  const shortTermTtl = document.querySelector("#short-term-ttl");
  const archiveAfter = document.querySelector("#archive-after");
  const deleteAfter = document.querySelector("#delete-after");
  const promoteAfterHits = document.querySelector("#promote-after-hits");

  if (themeSelect) themeSelect.value = theme;
  if (densitySelect) densitySelect.value = ui.density || "comfortable";
  if (languageSelect) languageSelect.value = ui.language || state.language || "en";
  if (memoryEnabled) memoryEnabled.checked = ui.memory_enabled !== false;
  applyTheme(theme);
  applyDensity(ui.density || "comfortable");
  applyLanguage(ui.language || state.language || "en");
  if (lifecycleEnabled) lifecycleEnabled.checked = lifecycle.enabled !== false;
  if (refreshOnAccess) refreshOnAccess.checked = lifecycle.refresh_on_access !== false;
  if (autoRescanOnStartup) autoRescanOnStartup.checked = lifecycle.auto_rescan_on_startup !== false;
  if (chatMemoryMode) chatMemoryMode.value = lifecycle.chat_memory_mode || "strict";
  if (chatCandidateTtl) chatCandidateTtl.value = lifecycle.chat_candidate_ttl_days ?? 7;
  if (chatStoreFactsOnly) chatStoreFactsOnly.checked = lifecycle.chat_store_facts_only !== false;
  if (shortTermTtl) shortTermTtl.value = lifecycle.short_term_ttl_days || 14;
  if (archiveAfter) archiveAfter.value = lifecycle.archive_after_days || 30;
  if (deleteAfter) deleteAfter.value = lifecycle.delete_after_days || 0;
  if (promoteAfterHits) promoteAfterHits.value = lifecycle.promote_after_hits || 5;
  state.onboardingComplete = ui.onboarding_complete === true;
  fillEmbeddingsSettings(settings);
}

function fillEmbeddingsSettings(settings) {
  const retrieval = settings.memory_retrieval || {};
  const status = settings.memory_embeddings || {};
  state.embeddingCatalog = Array.isArray(status.catalog) ? status.catalog : (state.embeddingCatalog || []);
  const enabled = document.querySelector("#embeddings-enabled");
  const provider = document.querySelector("#embedding-provider");
  const dims = document.querySelector("#embedding-dimensions");
  const pool = document.querySelector("#vector-pool");
  const minScore = document.querySelector("#vector-min-score");
  const timeout = document.querySelector("#vector-query-timeout");
  const reindex = document.querySelector("#reindex-on-startup");
  const statusEl = document.querySelector("#embeddings-status");
  if (enabled) enabled.checked = retrieval.embeddings_enabled !== false;
  if (provider) provider.value = retrieval.embedding_provider || status.provider || "auto";
  if (dims) dims.value = retrieval.embedding_dimensions || status.dimensions || 1024;
  if (pool) pool.value = retrieval.vector_pool || 64;
  if (minScore) minScore.value = retrieval.vector_min_score ?? 0.22;
  if (timeout) timeout.value = retrieval.vector_query_timeout_ms || 2500;
  if (reindex) reindex.checked = retrieval.reindex_on_startup === true;
  refreshEmbeddingModelOptions(retrieval.embedding_model || status.model || "");
  if (statusEl) {
    const env = status.env || {};
    const envBits = [
      env.cloudflare_token && env.cloudflare_account ? "CF✓" : "CF✗",
      env.gemini_key ? "Gemini✓" : "Gemini✗",
      env.ollama_host ? "Ollama✓" : "Ollama✗",
      env.azure_key ? "Azure✓" : "Azure✗",
    ].join(" · ");
    const cov = status.coverage || {};
    const indexed = Number(cov.indexed || 0);
    const active = Number(cov.active_nodes || 0);
    const pct = cov.coverage_pct != null ? Number(cov.coverage_pct) : (active ? Math.round((indexed / active) * 1000) / 10 : 0);
    const covBit = active ? `${indexed}/${active} (${pct}%)` : `${indexed} indexed`;
    statusEl.textContent = `${status.provider || "—"} / ${status.model || "—"} · ${status.dimensions || 0}d · ${status.enabled === false ? "off" : "on"} · ${covBit} · ${envBits}`;
  }
}

function refreshEmbeddingModelOptions(selectedModel = "") {
  const providerSelect = document.querySelector("#embedding-provider");
  const modelSelect = document.querySelector("#embedding-model");
  if (!modelSelect) return;
  const providerId = providerSelect?.value || "auto";
  const catalog = state.embeddingCatalog || [];
  let models = [];
  if (providerId === "auto") {
    models = catalog.flatMap(item => (item.models || []).map(model => ({ ...model, provider: item.id })));
  } else {
    const match = catalog.find(item => item.id === providerId);
    models = (match?.models || []).map(model => ({ ...model, provider: providerId }));
  }
  const current = selectedModel || modelSelect.value || "";
  modelSelect.innerHTML = "";
  if (!models.length) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = current || "(default)";
    modelSelect.appendChild(opt);
    return;
  }
  for (const model of models) {
    const opt = document.createElement("option");
    opt.value = model.id;
    opt.textContent = providerId === "auto" ? `${model.provider}: ${model.label || model.id}` : (model.label || model.id);
    modelSelect.appendChild(opt);
  }
  if (current && [...modelSelect.options].some(opt => opt.value === current)) {
    modelSelect.value = current;
  } else {
    modelSelect.selectedIndex = 0;
  }
  const selected = models.find(item => item.id === modelSelect.value);
  const dims = document.querySelector("#embedding-dimensions");
  if (dims && selected?.default_dims && !selectedModel) {
    dims.value = selected.default_dims;
  }
}

function readEmbeddingsForm() {
  return {
    embeddings_enabled: document.querySelector("#embeddings-enabled")?.checked ?? true,
    embedding_provider: document.querySelector("#embedding-provider")?.value || "auto",
    embedding_model: document.querySelector("#embedding-model")?.value || "",
    embedding_dimensions: Number(document.querySelector("#embedding-dimensions")?.value || 1024),
    vector_pool: Number(document.querySelector("#vector-pool")?.value || 64),
    vector_min_score: Number(document.querySelector("#vector-min-score")?.value || 0.22),
    vector_query_timeout_ms: Number(document.querySelector("#vector-query-timeout")?.value || 2500),
    reindex_on_startup: document.querySelector("#reindex-on-startup")?.checked ?? false,
  };
}

async function saveEmbeddingsSettings() {
  const payload = readEmbeddingsForm();
  const settings = await api("/api/settings", {
    method: "PATCH",
    body: JSON.stringify({ memory_retrieval: payload }),
  });
  fillEmbeddingsSettings(settings);
}

async function rebuildEmbeddingsIndex() {
  await saveEmbeddingsSettings();
  const result = await api("/api/memory/embeddings/rebuild", {
    method: "POST",
    body: JSON.stringify({ clear: true, async: true }),
  });
  const statusEl = document.querySelector("#embeddings-status");
  if (statusEl) {
    statusEl.textContent = `Rebuild started · ${result.provider || "?"} / ${result.model || "?"} (async)`;
  }
}

async function runSecurityPreview() {
  const input = document.querySelector("#security-preview-input");
  const result = document.querySelector("#security-preview-result");
  if (!input || !result) return;
  const payload = await api("/api/security/preview", { method: "POST", body: JSON.stringify({ text: input.value }) });
  const findings = (payload.findings || []).map(item => `<span class="badge">${escapeHtml(item.kind)} x${escapeHtml(String(item.count))}</span>`).join("");
  result.innerHTML = `<article class="result"><strong>${payload.redacted ? "Redaction applied" : "No sensitive patterns"}</strong><p>${escapeHtml(payload.text || "")}</p>${findings}</article>`;
}
const ROUTER_PRESETS = {
  cheap: { quality: 0.15, cost: 0.55, speed: 0.2, availability: 0.1 },
  fast: { quality: 0.2, cost: 0.15, speed: 0.55, availability: 0.1 },
  quality: { quality: 0.55, cost: 0.15, speed: 0.2, availability: 0.1 },
  balanced: { quality: 0.4, cost: 0.3, speed: 0.2, availability: 0.1 },
};
function routerWeightInputs() {
  return {
    quality: document.querySelector("#router-w-quality"),
    cost: document.querySelector("#router-w-cost"),
    speed: document.querySelector("#router-w-speed"),
    availability: document.querySelector("#router-w-availability"),
  };
}
function setRouterWeights(weights, strategy) {
  const inputs = routerWeightInputs();
  for (const [key, input] of Object.entries(inputs)) {
    if (!input) continue;
    const value = Number(weights?.[key] ?? ROUTER_PRESETS.balanced[key]);
    input.value = String(Math.round(value * 100));
  }
  if (strategy) {
    const select = document.querySelector("#router-strategy");
    if (select) select.value = strategy;
  }
  syncRouterWeightLabels();
}
function readRouterWeightsRaw() {
  const inputs = routerWeightInputs();
  return {
    quality: Number(inputs.quality?.value || 0) / 100,
    cost: Number(inputs.cost?.value || 0) / 100,
    speed: Number(inputs.speed?.value || 0) / 100,
    availability: Number(inputs.availability?.value || 0) / 100,
  };
}
function normalizeRouterWeights(weights) {
  const total = Object.values(weights).reduce((sum, value) => sum + Number(value || 0), 0) || 1;
  return Object.fromEntries(Object.entries(weights).map(([key, value]) => [key, Number(value || 0) / total]));
}
function syncRouterWeightLabels() {
  const raw = readRouterWeightsRaw();
  const sum = Object.values(raw).reduce((a, b) => a + b, 0);
  for (const key of Object.keys(raw)) {
    const label = document.querySelector(`#router-w-${key}-val`);
    if (label) label.textContent = raw[key].toFixed(2);
  }
  const sumEl = document.querySelector("#router-weight-sum");
  if (sumEl) sumEl.textContent = sum.toFixed(2);
  document.querySelectorAll("[data-router-preset]").forEach(btn => {
    const preset = ROUTER_PRESETS[btn.dataset.routerPreset];
    const active = preset && Object.keys(preset).every(key => Math.abs(preset[key] - raw[key]) < 0.03);
    btn.classList.toggle("active", Boolean(active));
  });
}
async function loadRouterSettings() {
  const payload = await api("/api/router/settings");
  const router = payload.router || {};
  setRouterWeights(router.weights || ROUTER_PRESETS.balanced, router.strategy || "balanced");
}
async function saveRouterSettings() {
  const weights = normalizeRouterWeights(readRouterWeightsRaw());
  setRouterWeights(weights);
  const body = {
    strategy: document.querySelector("#router-strategy").value,
    weights,
  };
  await api("/api/router/settings", { method: "PATCH", body: JSON.stringify(body) });
  await previewRouting(document.querySelector("#router-preview-query").value || "");
  showSnackbar("Router settings saved.", "success");
}
async function previewRouting(query) {
  const container = document.querySelector("#router-preview");
  if (!container) return;
  const payload = await api("/api/router/preview", { method: "POST", body: JSON.stringify({ message: query, project_id: state.projectId }) });
  const decision = payload.decision || {};
  const ranked = (decision.ranked || []).map(item => `<article class="result${item.provider_id === decision.selected ? " selected" : ""}"><div class="row"><strong>${escapeHtml(item.label)}</strong><span class="badge">score ${escapeHtml(String(item.score))}</span></div><span class="badge">quality ${escapeHtml(String(item.quality))}</span><span class="badge">cost ${escapeHtml(String(item.cost))}</span><span class="badge">latency ${escapeHtml(String(item.latency))}</span><span class="badge">${item.availability > 0 ? "available" : "offline"}</span>${item.matches_role ? '<span class="badge">role match</span>' : ""}</article>`).join("");
  container.innerHTML = `<article class="result"><strong>Role: ${escapeHtml(payload.role || "auto")} · Strategy: ${escapeHtml(decision.strategy || "")}</strong><p>${escapeHtml(decision.reason || "")}</p></article>${ranked}`;
}
async function loadCouncil() {
  let config;
  try {
    config = await api("/api/council");
  } catch (_err) {
    config = { models: [], default: [] };
  }
  const models = (Array.isArray(config.models) ? config.models : []).filter(m => m && m.id !== "auto");
  const defaults = new Set(Array.isArray(config.default) ? config.default : []);
  const container = document.querySelector("#ask-council-models");
  if (container) {
    if (!models.length) {
      container.innerHTML = `<p class="ask-team-sub">No API models connected yet. Add providers in Setup → Providers to use the council.</p>`;
    } else {
      container.innerHTML = models.map(model => {
        const disabled = !model.ready;
        const checked = model.ready && defaults.has(model.id);
        const status = model.ready ? "" : "unavailable";
        return `<label class="ask-council-model-chip${disabled ? " is-disabled" : ""}">
          <input class="ask-council-model" type="checkbox" value="${escapeHtml(model.id)}"${checked ? " checked" : ""}${disabled ? " disabled" : ""}>
          <span class="ask-council-model-name">${escapeHtml(model.label || model.id)}</span>
          ${status ? `<span class="ask-council-model-status">${escapeHtml(status)}</span>` : ""}
        </label>`;
      }).join("");
    }
  }
  const judge = document.querySelector("#ask-council-judge");
  if (judge) {
    const options = [`<option value="auto">Auto (router)</option>`].concat(
      models.map(model => `<option value="${escapeHtml(model.id)}"${model.ready ? "" : " disabled"}>${escapeHtml(model.label || model.id)}${model.ready ? "" : " (unavailable)"}</option>`)
    );
    judge.innerHTML = options.join("");
    judge.value = "auto";
  }
  document.querySelectorAll(".ask-council-model").forEach(input => {
    input.addEventListener("change", () => updateAskCouncilCount());
  });
  bindAskCouncilToggle();
  applyAskCouncilCollapsed(readAskCouncilCollapsed());
  updateAskCouncilCount();
}

function readAskCouncilCollapsed() {
  try {
    const raw = localStorage.getItem("architectos_ask_council_collapsed");
    if (raw == null) return true;
    return raw !== "0";
  } catch (_err) {
    return true;
  }
}

function writeAskCouncilCollapsed(collapsed) {
  try {
    localStorage.setItem("architectos_ask_council_collapsed", collapsed ? "1" : "0");
  } catch (_err) {
    /* ignore */
  }
}

function applyAskCouncilCollapsed(collapsed) {
  const panel = document.querySelector("#ask-council-options");
  const toggle = document.querySelector("#ask-council-toggle");
  const body = document.querySelector("#ask-council-body");
  if (!panel || !toggle || !body) return;
  panel.classList.toggle("is-collapsed", collapsed);
  body.hidden = collapsed;
  toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  writeAskCouncilCollapsed(collapsed);
}

function bindAskCouncilToggle() {
  const toggle = document.querySelector("#ask-council-toggle");
  if (!toggle || toggle.dataset.bound === "1") return;
  toggle.dataset.bound = "1";
  toggle.addEventListener("click", () => {
    const panel = document.querySelector("#ask-council-options");
    const next = !(panel && panel.classList.contains("is-collapsed"));
    applyAskCouncilCollapsed(next);
  });
}

function updateAskCouncilCount() {
  const countEl = document.querySelector("#ask-council-count");
  if (!countEl) return;
  const selected = [...document.querySelectorAll(".ask-council-model")].filter(el => el.checked).length;
  countEl.textContent = selected ? `${selected} model${selected === 1 ? "" : "s"}` : "auto panel";
}

function openSetupNav(forceOpen = true) {
  const group = document.querySelector(".nav-group-setup");
  const toggle = document.querySelector("#nav-setup-toggle");
  const items = document.querySelector("#nav-setup-items");
  if (!group || !toggle || !items) return;
  const open = forceOpen === true ? true : forceOpen === false ? false : !group.classList.contains("open");
  group.classList.toggle("open", open);
  items.hidden = !open;
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
}
function syncAskMode(mode = state.askMode || "quick") {
  state.askMode = ASK_MODES.includes(mode) ? mode : "quick";
  document.querySelectorAll("[data-ask-mode]").forEach(btn => {
    const active = btn.dataset.askMode === state.askMode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  const hint = document.querySelector("#ask-mode-hint");
  if (hint) {
    const text = t(`ask.hint.${state.askMode}`) || ASK_MODE_HINTS[state.askMode];
    hint.textContent = text;
    hint.hidden = state.askMode === "council";
  }
  const options = document.querySelector("#ask-council-options");
  if (options) options.hidden = state.askMode !== "council";
  if (state.askMode === "council") updateAskCouncilCount();
  const provider = document.querySelector("#chat-provider");
  if (provider) {
    // Memory = forced local stub. Council uses its own model picker.
    const lockProvider = state.askMode === "memory" || state.askMode === "council";
    provider.disabled = lockProvider;
    if (state.askMode === "memory") provider.value = "local-memory";
    else if (state.askMode === "memory-mcp" && (provider.value === "local-memory" || !provider.value)) {
      provider.value = "auto";
    }
  }
}
function renderCodeLanguages(payload) {
  const languages = payload.languages || [];
  const servers = state.codeServersCache?.servers || [];
  renderCodeStatus(languages, servers);
  renderCodeLanguageSupport(languages, servers);
}
function codeLanguageTone(item) {
  if (item.ready) return "ok";
  if (item.fallback) return "fallback";
  if (item.status === "planned") return "planned";
  return "warn";
}
function codeLanguageStatusLabel(item) {
  if (item.ready) return t("code.lang.ready");
  if (item.fallback) return t("code.lang.fallback");
  if (item.status === "planned") return t("code.lang.planned");
  return t("code.lang.needsInstall");
}
function renderCodeStatus(languages, servers) {
  const message = document.querySelector("#code-status-message");
  const badge = document.querySelector("#code-status-badge");
  const pills = document.querySelector("#code-status-pills");
  const connectBtn = document.querySelector("#code-connect-folder");
  const analyzeBtn = document.querySelector("#code-analyze-project");
  const noProject = needsProjectOnboarding() || state.projectFilesStatus === "no_root" || state.projectFilesStatus === "missing_root";

  if (connectBtn) connectBtn.hidden = !noProject;
  if (analyzeBtn) analyzeBtn.hidden = noProject;

  if (!languages.length) {
    if (message) message.textContent = noProject ? t("code.status.noProject") : t("code.status.noLanguages");
    if (badge) {
      badge.textContent = t("code.readiness.idle");
      badge.dataset.tone = "idle";
    }
    if (pills) pills.innerHTML = "";
    return;
  }

  const readyCount = languages.filter(item => item.ready).length;
  const fallbackCount = languages.filter(item => !item.ready && item.fallback).length;
  const effectiveReady = readyCount + fallbackCount;

  if (message) {
    if (readyCount === languages.length) message.textContent = t("code.status.ready");
    else if (effectiveReady === languages.length && fallbackCount > 0) message.textContent = t("code.status.fallbackOnly");
    else message.textContent = t("code.status.partial").replace("{ready}", String(readyCount)).replace("{total}", String(languages.length));
  }
  if (badge) {
    if (readyCount === languages.length) {
      badge.textContent = t("code.readiness.ready");
      badge.dataset.tone = "ok";
    } else if (effectiveReady === languages.length) {
      badge.textContent = t("code.readiness.fallback");
      badge.dataset.tone = "fallback";
    } else {
      badge.textContent = t("code.readiness.partial");
      badge.dataset.tone = "warn";
    }
  }
  if (pills) {
    pills.innerHTML = languages.slice(0, 6).map(item => {
      const tone = codeLanguageTone(item);
      return `<span class="code-status-pill" data-tone="${tone}">${escapeHtml(item.server_label || item.language)} · ${escapeHtml(t("code.lang.files").replace("{count}", String(item.count)))} · ${escapeHtml(codeLanguageStatusLabel(item))}</span>`;
    }).join("");
  }

  const setupBadge = document.querySelector("#code-setup-badge");
  if (setupBadge) setupBadge.textContent = t("code.setup.summary").replace("{ready}", String(effectiveReady)).replace("{total}", String(Math.max(languages.length, servers.length || languages.length)));
}
function renderCodeLanguageSupport(languages, servers) {
  const container = document.querySelector("#code-language-support");
  if (!container) return;
  const serverById = Object.fromEntries((servers || []).map(server => [server.id, server]));
  if (!languages.length) {
    container.innerHTML = `<article class="code-support-card empty"><p>${escapeHtml(t("code.lang.noData"))}</p></article>`;
    return;
  }
  const grouped = new Map();
  for (const item of languages) {
    const key = item.server_id || item.extension;
    const existing = grouped.get(key);
    if (!existing) {
      grouped.set(key, { ...item, extensions: [item.extension], count: item.count || 0 });
      continue;
    }
    existing.count += item.count || 0;
    if (!existing.extensions.includes(item.extension)) existing.extensions.push(item.extension);
    existing.ready = existing.ready || item.ready;
  }
  container.innerHTML = [...grouped.values()].map(item => {
    const server = serverById[item.server_id] || {};
    const tone = codeLanguageTone({ ...item, status: server.status || item.status });
    const install = item.install_command || server.install_command || "";
    const runnable = item.install_runnable ?? server.install_runnable ?? Boolean(install);
    const extLabel = (item.extensions || [item.extension]).filter(Boolean).join(", ");
    const installActions = install ? `<div class="code-support-actions">
        ${runnable && !item.ready ? `<button type="button" class="btn-secondary" data-code-install="${escapeHtml(item.server_id || "")}">${escapeHtml(t("code.lang.install"))}</button>` : ""}
        <button type="button" class="btn-text" data-install-toggle>${escapeHtml(t("code.lang.showCommand"))}</button>
      </div>
      <div class="code-install-command" hidden>
        <code>${escapeHtml(install)}</code>
        <div class="provider-actions install-actions">
          ${runnable ? `<button data-code-install="${escapeHtml(item.server_id || "")}" type="button">${escapeHtml(t("code.lang.run"))}</button>` : ""}
          <button data-install-copy type="button" data-command="${escapeHtml(install)}">${escapeHtml(t("code.lang.copy"))}</button>
          <button data-agent-install-command="${escapeHtml(install)}" type="button">Ask Agent</button>
        </div>
        <p class="code-install-result" data-install-result hidden></p>
      </div>` : "";
    return `<article class="code-support-card" data-tone="${tone}" data-server-id="${escapeHtml(item.server_id || "")}">
      <div class="code-support-head">
        <div>
          <strong>${escapeHtml(item.server_label || item.language)}</strong>
          <p>${escapeHtml(extLabel)} · ${escapeHtml(t("code.lang.files").replace("{count}", String(item.count)))}</p>
        </div>
        <span class="code-support-status">${escapeHtml(codeLanguageStatusLabel({ ...item, status: server.status }))}</span>
      </div>
      ${server.notes ? `<p class="code-support-note">${escapeHtml(server.notes)}</p>` : ""}
      ${installActions}
    </article>`;
  }).join("");
  container.querySelectorAll("[data-install-toggle]").forEach(button => {
    button.addEventListener("click", () => {
      const command = button.closest(".code-support-card")?.querySelector(".code-install-command");
      if (!command) return;
      command.hidden = !command.hidden;
    });
  });
}
async function installCodeLanguageServer(serverId, trigger) {
  if (!serverId) return;
  const card = trigger?.closest?.(".code-support-card") || document.querySelector(`.code-support-card[data-server-id="${CSS.escape(serverId)}"]`);
  const resultEl = card?.querySelector("[data-install-result]");
  const buttons = card ? [...card.querySelectorAll("[data-code-install]")] : [];
  buttons.forEach(btn => { btn.disabled = true; btn.textContent = t("code.lang.installing"); });
  if (resultEl) {
    resultEl.hidden = false;
    resultEl.className = "code-install-result";
    resultEl.textContent = t("code.lang.installing");
  }
  const commandPanel = card?.querySelector(".code-install-command");
  if (commandPanel) commandPanel.hidden = false;
  try {
    const payload = await api(`/api/code/servers/${encodeURIComponent(serverId)}/install`, {
      method: "POST",
      body: JSON.stringify({ project_id: state.projectId, timeout_seconds: 600 }),
    });
    if (resultEl) {
      const detail = [payload.message, payload.stderr, payload.stdout].filter(Boolean).join("\n").trim();
      resultEl.className = `code-install-result ${payload.ready || payload.status === "ok" ? "ok" : "error"}`;
      resultEl.textContent = detail.slice(0, 1200) || (payload.ready ? t("code.lang.installOk") : t("code.lang.installFailed"));
    }
    await loadCodeServers();
  } catch (error) {
    if (resultEl) {
      resultEl.className = "code-install-result error";
      resultEl.textContent = error.message || t("code.lang.installFailed");
    }
    buttons.forEach(btn => { btn.disabled = false; btn.textContent = t("code.lang.install"); });
    throw error;
  }
}
function renderCodeServerList(servers) {
  const list = document.querySelector("#code-server-list");
  if (!list) return;
  list.innerHTML = "";
  for (const server of servers) {
    const command = Array.isArray(server.command) ? server.command.join(" ") : (server.command || "");
    const el = document.createElement("article");
    el.className = `provider code-server-card ${server.status === "available" ? "ready" : server.status === "missing_executable" ? "error" : "planned"}`;
    el.innerHTML = `<div class="provider-head"><div><strong>${escapeHtml(server.label)}</strong><p>${escapeHtml((server.extensions || []).join(", "))}</p></div><span class="provider-status">${escapeHtml(codeLanguageStatusLabel({ ready: server.status === "available", fallback: false, status: server.status }))}</span></div><p class="provider-hint">${escapeHtml(server.notes || "")}</p>${installCommandHtml(server.install_command, server.id)}<label class="code-command-field">Command<input data-code-command="${escapeHtml(server.id)}" value="${escapeHtml(command)}"></label><div class="provider-actions code-test-actions"><button data-code-test="${escapeHtml(server.id)}" type="button">Test</button><span class="provider-test" data-code-result></span></div>`;
    list.appendChild(el);
  }
  list.querySelectorAll("[data-code-command]").forEach(input => input.addEventListener("change", async () => { await api("/api/code/servers", { method: "PATCH", body: JSON.stringify({ id: input.dataset.codeCommand, command: input.value.split(" ").filter(Boolean) }) }); }));
  list.querySelectorAll("[data-code-test]").forEach(button => button.addEventListener("click", async () => {
    const card = button.closest(".provider");
    const result = card.querySelector("[data-code-result]");
    result.className = "provider-test"; result.textContent = "checking...";
    try {
      const payload = await api(`/api/code/servers/${button.dataset.codeTest}/test`, { method: "POST", body: "{}" });
      result.className = `provider-test ${payload.ready ? "ok" : "error"}`;
      result.textContent = payload.message || "";
      await loadCodeServers();
    } catch (error) { result.className = "provider-test error"; result.textContent = error.message; }
  }));
}
function syncCodeInsightTab() {
  const tab = state.codeInsightTab || "symbols";
  document.querySelectorAll("[data-code-tab]").forEach(button => {
    const active = button.dataset.codeTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  const position = document.querySelector("#code-position-controls");
  if (position) position.hidden = tab === "symbols" || tab === "diagnostics";
  const symbols = document.querySelector("#code-symbols");
  const insight = document.querySelector("#code-insight");
  if (symbols) symbols.hidden = tab !== "symbols";
  if (insight) insight.hidden = tab === "symbols";
}
function syncCodeExploreEmpty(hasResults) {
  const empty = document.querySelector("#code-explore-empty");
  if (empty) empty.hidden = Boolean(hasResults);
}
function setCodeInsightTab(tab) {
  state.codeInsightTab = tab;
  syncCodeInsightTab();
}
async function runCodeInsight() {
  const tab = state.codeInsightTab || "symbols";
  if (tab === "symbols") await loadCodeSymbols();
  else if (tab === "diagnostics") await loadCodeDiagnostics();
  else if (tab === "hover") await loadCodeHover();
  else if (tab === "references") await loadCodeReferences();
}
async function analyzeCodeProject() {
  const status = document.querySelector("#code-status-message");
  if (status) status.textContent = t("action.scanProject") + "...";
  try {
    await api("/api/project/scan", { method: "POST", body: JSON.stringify({ project_id: state.projectId }) });
    await loadCodeServers();
    showSnackbar(t("action.scanProject"), "ok");
  } catch (error) {
    showError(error);
    await loadCodeServers();
  }
}
function installCommandHtml(command, serverId = "") {
  if (!command) return "";
  const value = escapeHtml(command);
  const id = escapeHtml(serverId || "");
  return `<div class="install-command"><strong>Missing/install:</strong><div class="command-row"><code title="${value}">${value}</code><div class="provider-actions install-actions">${serverId ? `<button data-code-install="${id}" type="button">${escapeHtml(t("code.lang.run"))}</button>` : `<button data-terminal-install-run="${value}" type="button">${escapeHtml(t("code.lang.run"))}</button>`}<button data-install-copy type="button" data-command="${value}">${escapeHtml(t("code.lang.copy"))}</button><button data-agent-install-command="${value}" type="button">Ask Agent</button></div></div></div>`;
}
function fillCodeFileFromSelection(overwrite = true) {
  const input = document.querySelector("#code-file-path");
  if (!input || !state.selectedFile) return false;
  if (overwrite || !input.value.trim()) input.value = state.selectedFile;
  syncCodeFileHint();
  return true;
}
function syncCodeFileHint() {
  const hint = document.querySelector("#code-selected-file-hint");
  if (!hint) return;
  hint.textContent = state.selectedFile
    ? t("code.usingFile").replace("{path}", state.selectedFile)
    : t("code.noFileSelected");
}
function toggleCodeAdvanced() {
  const panel = document.querySelector(".code-lsp-advanced");
  if (!panel) return;
  panel.open = !panel.open;
}
function codeRequestPayload() {
  const input = document.querySelector("#code-file-path");
  const path = (input.value || state.selectedFile || "").trim();
  if (input && path) input.value = path;
  return {
    project_id: state.projectId,
    path,
    line: Number(document.querySelector("#code-line")?.value || 0),
    character: Number(document.querySelector("#code-character")?.value || 0),
    query: document.querySelector("#code-reference-query")?.value || "",
  };
}
async function loadCodeServers() {
  const [payload, languages] = await Promise.all([api("/api/code/servers"), api(`/api/code/languages?project_id=${projectParam()}`)]);
  state.codeServersCache = payload;
  const languageItems = languages.languages || [];
  renderCodeStatus(languageItems, payload.servers || []);
  renderCodeLanguageSupport(languageItems, payload.servers || []);
  renderCodeServerList(payload.servers || []);
}
async function loadCodeSymbols() {
  const container = document.querySelector("#code-symbols");
  const request = codeRequestPayload();
  if (!request.path) { container.innerHTML = `<article class="result"><strong>${escapeHtml(t("code.noFileSelected"))}</strong></article>`; syncCodeExploreEmpty(false); return; }
  container.innerHTML = '<article class="result"><strong>Analyzing</strong><p>...</p></article>';
  syncCodeExploreEmpty(true);
  try {
    const payload = await api("/api/code/symbols", { method: "POST", body: JSON.stringify(request) });
    const symbols = payload.symbols || [];
    container.innerHTML = `<article class="result code-insight-summary"><strong>${escapeHtml(payload.language)} · ${symbols.length} symbol(s)</strong><p>${escapeHtml(payload.path)} · ${escapeHtml(payload.source || "lsp")}${payload.message ? ` · ${escapeHtml(payload.message)}` : ""}</p></article>` + (symbols.length ? symbols.map(symbol => `<article class="result code-symbol-row" style="margin-left:${Math.min(symbol.depth, 5) * 14}px"><div class="row"><strong>${escapeHtml(symbol.name)}</strong><span class="badge">${escapeHtml(symbol.kind)}</span></div><span class="badge">line ${escapeHtml(String(symbol.line))}</span>${symbol.detail ? `<span class="muted"> ${escapeHtml(symbol.detail)}</span>` : ""}</article>`).join("") : `<article class="result"><strong>No symbols returned</strong></article>`);
    syncCodeExploreEmpty(true);
  } catch (error) {
    container.innerHTML = `<article class="result"><strong>Symbols failed</strong><p>${escapeHtml(error.message)}</p></article>`;
    syncCodeExploreEmpty(true);
  }
}
async function loadCodeHover() {
  const container = document.querySelector("#code-insight");
  const request = codeRequestPayload();
  if (!request.path) { container.innerHTML = `<article class="result"><strong>${escapeHtml(t("code.noFileSelected"))}</strong></article>`; syncCodeExploreEmpty(false); return; }
  syncCodeExploreEmpty(true);
  const payload = await api("/api/code/hover", { method: "POST", body: JSON.stringify(request) });
  container.innerHTML = `<article class="result code-insight-summary"><strong>Hover (${escapeHtml(payload.source || "lsp")})</strong><p>${escapeHtml(payload.path)}:${escapeHtml(String(Number(payload.line || 0) + 1))}:${escapeHtml(String(payload.character || 0))}</p><pre class="inline-pre">${escapeHtml(payload.hover || "No hover text.")}</pre></article>`;
}
async function loadCodeDiagnostics() {
  const container = document.querySelector("#code-insight");
  const request = codeRequestPayload();
  if (!request.path) { container.innerHTML = `<article class="result"><strong>${escapeHtml(t("code.noFileSelected"))}</strong></article>`; syncCodeExploreEmpty(false); return; }
  syncCodeExploreEmpty(true);
  const payload = await api("/api/code/diagnostics", { method: "POST", body: JSON.stringify(request) });
  const items = payload.diagnostics || [];
  container.innerHTML = `<article class="result code-insight-summary"><strong>Diagnostics (${escapeHtml(payload.source || "basic")})</strong><p>${escapeHtml(payload.path)} · ${items.length} issue(s)</p></article>` + (items.length ? items.map(item => `<article class="result"><div class="row"><strong>${escapeHtml(item.severity || "info")}</strong><span class="badge">line ${escapeHtml(String(item.line || 1))}</span></div><p>${escapeHtml(item.message || "")}</p></article>`).join("") : '<article class="result"><strong>No diagnostics</strong></article>');
}
async function loadCodeReferences() {
  const container = document.querySelector("#code-insight");
  const request = codeRequestPayload();
  if (!request.path) { container.innerHTML = `<article class="result"><strong>${escapeHtml(t("code.noFileSelected"))}</strong></article>`; syncCodeExploreEmpty(false); return; }
  syncCodeExploreEmpty(true);
  const payload = await api("/api/code/references", { method: "POST", body: JSON.stringify(request) });
  const refs = payload.references || [];
  container.innerHTML = `<article class="result code-insight-summary"><strong>References (${escapeHtml(payload.source || "lsp")})</strong><p>${escapeHtml(payload.query || "")} · ${refs.length} result(s)</p></article>` + (refs.length ? refs.map(ref => `<article class="result"><div class="row"><strong>${escapeHtml(ref.path || payload.path)}</strong><span class="badge">line ${escapeHtml(String(ref.line || 1))}</span></div><p>${escapeHtml(ref.preview || "")}</p></article>`).join("") : '<article class="result"><strong>No references</strong></article>');
}
function terminalPayload(commandOverride = "") {
  return {
    project_id: state.projectId,
    command: commandOverride || document.querySelector("#terminal-command")?.value || "",
    shell: document.querySelector("#terminal-shell")?.value || "auto",
    timeout_seconds: Number(document.querySelector("#terminal-timeout")?.value || 20),
    allow_destructive: Boolean(document.querySelector("#terminal-allow-destructive")?.checked),
  };
}
function renderTerminalResult(payload) {
  const output = document.querySelector("#terminal-output");
  const status = document.querySelector("#terminal-status");
  if (!output || !status) return;
  const stdout = payload.stdout || "";
  const stderr = payload.stderr || "";
  const parts = [
    `$ ${payload.command || ""}`,
    `cwd: ${payload.root || ""}`,
    `status: ${payload.status || ""}${payload.returncode !== null && payload.returncode !== undefined ? ` / exit ${payload.returncode}` : ""}${payload.duration_ms ? ` / ${payload.duration_ms}ms` : ""}`,
    stdout ? `\nstdout:\n${stdout}` : "",
    stderr ? `\nstderr:\n${stderr}` : "",
  ].filter(Boolean);
  output.textContent = parts.join("\n");
  status.className = `provider-test ${payload.status === "ok" || payload.status === "opened" ? "ok" : payload.status === "blocked" || payload.status === "error" ? "error" : ""}`;
  status.textContent = payload.status === "blocked" ? "blocked by terminal safety policy" : payload.status || "";
}
function renderTerminalHistory() {
  const list = document.querySelector("#terminal-history");
  if (!list) return;
  list.innerHTML = state.terminalHistory.length ? state.terminalHistory.map((item, index) => `<article class="result"><div class="row"><strong>${escapeHtml(item.command)}</strong><button data-terminal-rerun="${index}" type="button">Use</button></div><span class="badge">${escapeHtml(item.status || "")}</span><span class="badge">${escapeHtml(item.shell || "auto")}</span></article>`).join("") : '<article class="result"><strong>No commands yet</strong></article>';
  list.querySelectorAll("[data-terminal-rerun]").forEach(button => button.addEventListener("click", () => {
    const item = state.terminalHistory[Number(button.dataset.terminalRerun)];
    if (item) setElementValue("#terminal-command", item.command);
  }));
}
async function executeTerminalCommand(commandOverride = "") {
  const status = document.querySelector("#terminal-status");
  const payload = terminalPayload(commandOverride);
  if (!payload.command.trim()) throw new Error("Terminal command is required.");
  const commandInput = document.querySelector("#terminal-command");
  if (commandInput) commandInput.value = payload.command;
  if (status) { status.className = "provider-test"; status.textContent = "running..."; }
  const result = await api("/api/terminal/run", { method: "POST", body: JSON.stringify(payload) });
  state.terminalHistory.unshift({ command: payload.command, shell: result.shell || payload.shell, status: result.status });
  state.terminalHistory = state.terminalHistory.slice(0, 20);
  renderTerminalResult(result);
  renderTerminalHistory();
}
async function runTerminalCommand(event) {
  if (event) event.preventDefault();
  await executeTerminalCommand();
}
async function runInstallCommandInTerminal(command) {
  if (!command) return;
  switchView("terminal");
  await executeTerminalCommand(command);
}
async function copyInstallCommand(command) {
  if (!command) return;
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(command);
  } else {
    const textarea = document.createElement("textarea");
    textarea.value = command;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }
  const summary = document.querySelector("#code-summary");
  if (summary) summary.textContent = "command copied";
}
async function askAgentInstallCommand(command) {
  if (!command) return;
  syncAskMode("quick");
  switchView("chat");
  const message = [
    "Execute or safely guide this install command for the current project.",
    "Check the project context first, explain any risk, then run it only if it is appropriate.",
    "",
    `Command: ${command}`,
  ].join("\n");
  const input = document.querySelector("#chat-message");
  const approve = document.querySelector("#chat-approve-cli");
  if (input) { input.value = message; autoGrowChatInput(); }
  if (approve) approve.checked = true;
  document.querySelector("#chat-form")?.requestSubmit();
}
async function openExternalTerminal() {
  const status = document.querySelector("#terminal-status");
  if (status) { status.className = "provider-test"; status.textContent = "opening..."; }
  const payload = await api("/api/terminal/open", { method: "POST", body: JSON.stringify({ project_id: state.projectId }) });
  renderTerminalResult({ ...payload, command: (payload.command || []).join(" "), stdout: payload.message || "", stderr: "" });
}
function switchView(view) {
  if (view === "analytics") {
    switchView("memory");
    return;
  }
  const target = view === "agents" ? "chat" : view;
  if (view === "agents") syncAskMode("council");
  if (SETUP_VIEWS.has(target)) openSetupNav(true);
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === target));
  document.querySelectorAll(".view").forEach(item => item.classList.remove("active"));
  const viewEl = document.querySelector(`#${target}-view`);
  if (viewEl) viewEl.classList.add("active");
  setTextContent("#view-title", t(titleByView[target]));
  if (target === "workspace") {
    refreshWorkspace();
    loadProjectFiles();
    if (typeof workspaceChat !== "undefined") {
      workspaceChat.syncProviders?.();
      workspaceChat.syncControlsFromAsk?.();
      workspaceChat.refreshThread?.();
    }
  }
  if (target === "chat") { loadProviders(); loadChats(); loadCouncil(); syncAskMode(); }
  if (target === "memory") {
    loadMemoryCandidates();
    renderMemoryFiles();
    scheduleGraphLoad();
    loadMemoryLifecycle();
    loadAnalytics();
    syncIngestSourcesWithMcp().catch(() => {});
  }
  if (target === "tasks") loadTasks();
  if (target === "providers") { loadProviders(); loadProviderRuns(); loadRouterSettings(); }
  if (target === "mcp") { loadMcpMasterDetail(); initMcpMasterDetail(); }
  if (target === "code") { fillCodeFileFromSelection(false); syncCodeFileHint(); syncCodeInsightTab(); loadCodeServers(); }
  if (target === "terminal") renderTerminalHistory();
  if (target === "settings") loadSettings();
}
function autoGrowChatInput() {
  const input = document.querySelector("#chat-message");
  if (!input) return;
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}
function closeChatMenu() {
  const menu = document.querySelector("#chat-menu");
  const plus = document.querySelector("#chat-plus");
  if (menu) menu.hidden = true;
  if (plus) plus.setAttribute("aria-expanded", "false");
}
function toggleChatMenu() {
  const menu = document.querySelector("#chat-menu");
  const plus = document.querySelector("#chat-plus");
  if (!menu) return;
  const open = menu.hidden;
  menu.hidden = !open;
  if (plus) plus.setAttribute("aria-expanded", String(open));
}
function formatBytes(size) {
  if (!size) return "0 B";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
function renderAttachments() {
  const box = document.querySelector("#chat-attachments");
  if (!box) return;
  box.hidden = state.attachments.length === 0;
  box.innerHTML = state.attachments.map(file => {
    const isImage = String(file.mime || "").startsWith("image/");
    const binary = !isImage && file.text_extracted === false;
    const thumb = isImage && file.preview ? `<img class="attachment-thumb" src="${escapeHtml(file.preview)}" alt="">` : "";
    return `<span class="attachment-chip"${binary ? ' data-binary="1"' : ""}${isImage ? ' data-image="1"' : ""}>${thumb}<span class="attachment-name">${escapeHtml(file.name)}</span><span class="attachment-size">${escapeHtml(formatBytes(file.size))}</span><button type="button" class="attachment-remove" data-remove-file="${escapeHtml(file.id)}" aria-label="Remove">&times;</button></span>`;
  }).join("");
}
function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
async function handleFileSelect(fileList) {
  const files = Array.from(fileList || []);
  for (const file of files) {
    try {
      const content = await readFileAsDataUrl(file);
      const result = await api("/api/files", { method: "POST", body: JSON.stringify({ project_id: state.projectId, name: file.name, content }) });
      if (result.file) {
        if (String(result.file.mime || "").startsWith("image/")) result.file.preview = content;
        state.attachments.push(result.file);
        renderAttachments();
      }
    } catch (error) { showError(error); }
  }
}
async function removeAttachment(fileId) {
  const file = state.attachments.find(item => item.id === fileId);
  state.attachments = state.attachments.filter(item => item.id !== fileId);
  renderAttachments();
  if (file) { try { await api("/api/files/delete", { method: "POST", body: JSON.stringify({ project_id: state.projectId, id: fileId }) }); } catch (error) { showError(error); } }
}
function bindChatComposer() {
  const input = document.querySelector("#chat-message");
  if (input) {
    input.addEventListener("input", autoGrowChatInput);
    input.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        document.querySelector("#chat-form").requestSubmit();
      }
    });
  }
  const plus = document.querySelector("#chat-plus");
  if (plus) plus.addEventListener("click", event => { event.stopPropagation(); toggleChatMenu(); });
  document.querySelectorAll("[data-ask-mode]").forEach(btn => {
    btn.addEventListener("click", () => syncAskMode(btn.dataset.askMode));
  });
  const setupToggle = document.querySelector("#nav-setup-toggle");
  if (setupToggle) setupToggle.addEventListener("click", () => openSetupNav());
  const fileInput = document.querySelector("#chat-file-input");
  const openPicker = () => { closeChatMenu(); if (fileInput) fileInput.click(); };
  const attach = document.querySelector("#chat-attach");
  if (attach) attach.addEventListener("click", openPicker);
  const attachMenu = document.querySelector("#chat-attach-menu");
  if (attachMenu) attachMenu.addEventListener("click", openPicker);
  if (fileInput) fileInput.addEventListener("change", event => { handleFileSelect(event.target.files).catch(showError); event.target.value = ""; });
  const attachmentsBox = document.querySelector("#chat-attachments");
  if (attachmentsBox) attachmentsBox.addEventListener("click", event => {
    const btn = event.target.closest("[data-remove-file]");
    if (btn) removeAttachment(btn.dataset.removeFile).catch(showError);
  });
  on("#chat-thread", "click", event => {
    const favoriteBtn = event.target.closest("[data-favorite-message]");
    if (favoriteBtn) {
      api(`/api/chats/${encodeURIComponent(favoriteBtn.dataset.favoriteMessage)}/favorite-message`, {
        method: "POST",
        body: JSON.stringify({ message_index: Number(favoriteBtn.dataset.messageIndex || 0), favorite: true }),
      }).then(async () => {
        await loadChats();
        await loadMemoryCandidates();
      }).catch(showError);
      return;
    }
    const feedbackBtn = event.target.closest("[data-feedback-rating]");
    if (feedbackBtn) {
      api("/api/memory/feedback", {
        method: "POST",
        body: JSON.stringify({
          project_id: state.projectId,
          chat_id: feedbackBtn.dataset.feedbackChat,
          message_index: Number(feedbackBtn.dataset.messageIndex || 0),
          rating: Number(feedbackBtn.dataset.feedbackRating || 0),
        }),
      }).then(async () => {
        await loadChats();
      }).catch(showError);
      return;
    }
    const richAction = event.target.closest("[data-rich-action]");
    if (richAction) {
      event.preventDefault();
      handleRichAction({
        type: richAction.dataset.richAction,
        id: richAction.dataset.richId,
        prompt: richAction.dataset.richPrompt,
        query: richAction.dataset.richQuery,
        label: richAction.textContent,
      }).catch(showError);
      return;
    }
    const richLink = event.target.closest("[data-rich-link]");
    if (richLink) {
      event.preventDefault();
      const href = String(richLink.getAttribute("data-rich-link") || richLink.getAttribute("href") || "");
      if (/^https?:/i.test(href)) {
        window.open(href, "_blank", "noopener");
        return;
      }
      const work = href.match(/^(?:ab|ado):\/\/?#?(\d+)/i) || href.match(/^AB#(\d+)/i);
      if (work) {
        handleRichAction({ type: "open_work_item", id: work[1] }).catch(showError);
        return;
      }
      const memory = href.match(/^memory:\/\/?#?(.+)$/i);
      if (memory) {
        handleRichAction({ type: "memory_get", id: memory[1] }).catch(showError);
        return;
      }
      return;
    }
    const chip = event.target.closest(".chat-suggestion");
    if (!chip || !input) return;
    input.value = chip.dataset.suggest || chip.textContent || "";
    autoGrowChatInput();
    input.focus();
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".composer-menu-wrap")) closeChatMenu();
  });
  on("#chat-list", "click", event => {
    const retry = event.target.closest("[data-chat-retry]");
    if (retry) {
      api(`/api/chats/${encodeURIComponent(retry.dataset.chatRetry)}/keeper-retry`, { method: "POST", body: "{}" })
        .then(async () => { await loadChats(); await loadMemoryCandidates(); })
        .catch(showError);
      return;
    }
    const context = event.target.closest("[data-chat-context]");
    if (context) {
      api(`/api/chats/${encodeURIComponent(context.dataset.chatContext)}/context-pack?project_id=${projectParam()}`)
        .then(pack => showContextPack(pack))
        .catch(showError);
    }
  });
  syncAskMode();
}

function showContextPack(pack) {
  const rolling = pack.rolling_summary?.summary_text || "No rolling summary yet.";
  const decisions = pack.decision_log?.summary_text || "No decisions captured yet.";
  const pending = (pack.pending_candidates || []).map(item => `- ${item.label}`).join("\n") || "No pending chat candidates.";
  const text = `Rolling summary:\n${rolling}\n\nDecision log:\n${decisions}\n\nPending candidates:\n${pending}`;
  appendChatBubble("assistant", text, false, { id: "context-pack", label: "Context Pack" });
}
async function openMemorySearch(query) {
  const q = String(query || "").trim();
  if (!q) return;
  if (typeof searchPalette !== "undefined" && searchPalette.open) {
    searchPalette.open();
    const input = document.getElementById("paletteSearchInput");
    if (input) {
      input.value = q;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    return;
  }
  switchView("memory");
  setElementValue("#search-query", q);
  await runSearch(q);
}
async function queueAskFollowUp(prompt, { send = false } = {}) {
  const text = String(prompt || "").trim();
  if (!text) return;
  switchView("chat");
  const input = document.querySelector("#chat-message");
  if (input) {
    input.value = text;
    autoGrowChatInput();
    input.focus();
  }
  if (send) {
    await sendChatMessage(new Event("submit"));
  } else {
    showSnackbar("Follow-up ready in Ask composer.", "info");
  }
}
async function handleRichAction(action) {
  const type = String(action.type || "").trim();
  const id = String(action.id || "").trim();
  if (type === "ask") {
    await queueAskFollowUp(action.prompt || action.label || "", { send: true });
    return;
  }
  if (type === "open_work_item" || type === "boards_get_item") {
    await openMemorySearch(id ? `AB#${id}` : "work item");
    showSnackbar(id ? `Searching memory for AB#${id}` : "Searching work items", "info");
    return;
  }
  if (type === "boards_search") {
    const query = String(action.query || id || "").trim();
    await queueAskFollowUp(query ? `Search Azure Boards for: ${query}` : "Search my Azure Boards work items", { send: true });
    return;
  }
  if (type === "memory_get") {
    await openMemorySearch(id || "memory");
    showSnackbar(id ? `Searching memory for ${id}` : "Searching memory", "info");
    return;
  }
  showSnackbar(`Unsupported action: ${type || "unknown"}`, "info");
}
function bindEvents() {
  bindChatComposer();
  initVoiceMemory();
  onAll(".nav-item", "click", item => switchView(item.currentTarget.dataset.view));
  on("#project-select", "change", event => { state.projectId = event.target.value; saveWorkspaceSettings({ current_project_id: state.projectId }).catch(showError); state.selectedFile = ""; state.memoryFiles = []; resetGraphFilters(); renderMemoryFiles(); syncProjectSwitcherLabel(); syncProjectTerminology(); syncProjectFields(); refreshWorkspace(); loadProjectFiles(); scheduleGraphLoad(); });
  on("#settings-language-select", "change", event => { applyLanguage(event.target.value); saveUiSettings({ language: state.language }).catch(showError); });
  on("#theme-select", "change", event => applyTheme(event.target.value));
  on("#density-select", "change", event => applyDensity(event.target.value));
  on("#open-project-folder-modal", "click", openProjectFolderModal);
  on("#close-project-folder-modal", "click", closeProjectFolderModal);
  onAll("[data-close-project-folder]", "click", closeProjectFolderModal);
  on("#browse-project-folder", "click", () => browseProjectFolder().catch(error => { setProjectFolderStatus(error.message, "error"); }));
  on("#save-project", "click", () => saveProjectFromFolder().then(project => { setProjectFolderStatus(project.message || "Project folder applied.", "ok"); closeProjectFolderModal(); }).catch(showError));
  on("#init-project", "click", () => initProjectFromFolder().then(() => { setProjectFolderStatus("Project indexed.", "ok"); showSnackbar("Project indexed successfully.", "success"); closeProjectFolderModal(); }).catch(showError));
  on("#context-form", "submit", event => { event.preventDefault(); buildContext(document.querySelector("#context-query")?.value || "memory context").catch(showError); });
  on("#memory-form", "submit", async event => { event.preventDefault(); await api("/api/memory", { method: "POST", body: JSON.stringify({ project_id: state.projectId, label: document.querySelector("#memory-label")?.value || "", type: document.querySelector("#memory-type")?.value || "Note", scope: document.querySelector("#memory-scope")?.value || "project", text: document.querySelector("#memory-text")?.value || "" }) }); event.target.reset(); await refreshWorkspace(); scheduleGraphLoad(); });
  on("#memory-file-picker", "click", () => document.querySelector("#memory-file-input")?.click());
  on("#memory-file-input", "change", event => { handleMemoryFileSelect(event.target.files).catch(showError); event.target.value = ""; });
  on("#memory-file-import", "click", () => importMemoryFiles().catch(showError));
  on("#ingest-memory", "click", () => ingestMemorySources().catch(showError));
  on("#rescan-memory-all", "click", () => rescanAllMemorySources().catch(showError));
  on("#ingest-select-all", "click", () => setIngestSourcesSelected(true));
  on("#ingest-unselect-all", "click", () => setIngestSourcesSelected(false));
  document.querySelectorAll(".ingest-source").forEach(input => {
    input.addEventListener("change", syncBoardsOptionsVisibility);
  });
  document.querySelectorAll(".boards-type-option").forEach(input => {
    input.addEventListener("change", syncBoardsTypeSummary);
  });
  on("#boards-types-select-all", "click", () => {
    document.querySelectorAll(".boards-type-option").forEach(input => { input.checked = true; });
    syncBoardsTypeSummary();
  });
  on("#boards-types-unselect-all", "click", () => {
    document.querySelectorAll(".boards-type-option").forEach(input => { input.checked = false; });
    syncBoardsTypeSummary();
  });
  on("#refresh-candidates", "click", () => loadMemoryCandidates().catch(showError));
  on("#candidate-status-filter", "change", () => {
    syncCandidateBatchActions();
    loadMemoryCandidates().catch(showError);
  });
  on("#candidates-accept-pending", "click", () => batchUpdateCandidates({ action: "promote", status: "candidate" }).catch(showError));
  on("#candidates-accept-non-duplicates", "click", () => batchUpdateCandidates({ action: "promote", status: "candidate", exclude_duplicates: true }).catch(showError));
  on("#candidates-reject-duplicates", "click", () => batchUpdateCandidates({ action: "reject", status: "duplicate", reason: "Batch rejected duplicate" }).catch(showError));
  on("#candidates-reject-pending", "click", () => batchUpdateCandidates({ action: "reject", status: "candidate", reason: "Batch rejected pending" }).catch(showError));
  on("#candidates-accept-filtered", "click", () => batchUpdateCandidates({ action: "promote", ...candidateBatchFilterFromUi() }).catch(showError));
  on("#candidates-reject-filtered", "click", () => batchUpdateCandidates({ action: "reject", reason: "Batch rejected filtered", ...candidateBatchFilterFromUi() }).catch(showError));
  document.addEventListener("click", event => {
    const more = document.querySelector(".candidates-more");
    if (!more || !more.open) return;
    if (more.contains(event.target)) return;
    more.open = false;
  });
  syncCandidateBatchActions();
  on("#run-memory-decay", "click", async () => { const summary = document.querySelector("#memory-decay-summary"); if (summary) summary.textContent = "checking..."; const payload = await api("/api/memory/decay/run", { method: "POST", body: JSON.stringify({ project_id: state.projectId }) }); if (summary) summary.textContent = `${payload.changed} memory item(s) updated`; await runSearch(document.querySelector("#search-query")?.value || "memory"); await loadAnalytics(); await loadMemoryLifecycle(); });
  on("#memory-lifecycle-state-filter", "change", () => loadMemoryLifecycleItems().catch(showError));
  on("#memory-lifecycle-tier-filter", "change", () => loadMemoryLifecycleItems().catch(showError));
  // Tasks are created via the inline TODO composer in loadTasks().
  on("#chat-form", "submit", event => sendChatMessage(event).catch(showError));
  on("#chat-stop", "click", () => cancelActiveRun().catch(showError));
  on("#refresh-files", "click", () => loadProjectFiles().catch(showError));
  on("#build-file-context", "click", () => buildSelectedFileContext().catch(showError));
  on("#load-git-diff", "click", () => loadGitDiff().catch(showError));
  on("#refresh-graph", "click", () => loadGraph().catch(showError));
  on("#export-bundle", "click", async () => { const bundle = await api(`/api/bundle/export?project_id=${projectParam()}`); const box = document.querySelector("#bundle-box"); if (box) box.value = JSON.stringify(bundle, null, 2); });
  on("#import-bundle", "click", async () => { const box = document.querySelector("#bundle-box"); if (!box) throw new Error("Bundle input is not available."); const payload = JSON.parse(box.value); await api("/api/bundle/import", { method: "POST", body: JSON.stringify(payload) }); await loadProjects(); await refreshWorkspace(); });
  on("#settings-form", "submit", async event => { event.preventDefault(); await api("/api/settings", { method: "PATCH", body: JSON.stringify({ ui: { theme: document.querySelector("#theme-select")?.value || state.theme, density: document.querySelector("#density-select")?.value || "comfortable", memory_enabled: document.querySelector("#memory-enabled")?.checked ?? true, language: document.querySelector("#settings-language-select")?.value || state.language, onboarding_complete: state.onboardingComplete }, memory_lifecycle: { enabled: document.querySelector("#lifecycle-enabled")?.checked ?? true, refresh_on_access: document.querySelector("#refresh-on-access")?.checked ?? true, auto_rescan_on_startup: document.querySelector("#auto-rescan-on-startup")?.checked ?? true, chat_memory_mode: document.querySelector("#chat-memory-mode")?.value || "strict", chat_candidate_ttl_days: Number(document.querySelector("#chat-candidate-ttl")?.value || 7), chat_store_facts_only: document.querySelector("#chat-store-facts-only")?.checked ?? true, short_term_ttl_days: Number(document.querySelector("#short-term-ttl")?.value || 14), archive_after_days: Number(document.querySelector("#archive-after")?.value || 30), delete_after_days: Number(document.querySelector("#delete-after")?.value || 0), promote_after_hits: Number(document.querySelector("#promote-after-hits")?.value || 5) } }) }); applyTheme(document.querySelector("#theme-select")?.value || state.theme); applyDensity(document.querySelector("#density-select")?.value || "comfortable"); applyLanguage(document.querySelector("#settings-language-select")?.value || state.language); });
  on("#embeddings-form", "submit", async event => { event.preventDefault(); await saveEmbeddingsSettings().catch(showError); });
  on("#embeddings-rebuild", "click", () => rebuildEmbeddingsIndex().catch(showError));
  on("#embedding-provider", "change", () => {
    refreshEmbeddingModelOptions();
    const modelSelect = document.querySelector("#embedding-model");
    const dims = document.querySelector("#embedding-dimensions");
    const catalog = state.embeddingCatalog || [];
    const providerId = document.querySelector("#embedding-provider")?.value;
    const provider = catalog.find(item => item.id === providerId);
    const model = (provider?.models || []).find(item => item.id === modelSelect?.value) || (provider?.models || [])[0];
    if (dims && model?.default_dims) dims.value = model.default_dims;
  });
  on("#embedding-model", "change", () => {
    const providerId = document.querySelector("#embedding-provider")?.value;
    const modelId = document.querySelector("#embedding-model")?.value;
    const catalog = state.embeddingCatalog || [];
    const provider = catalog.find(item => item.id === providerId) || catalog.find(item => (item.models || []).some(m => m.id === modelId));
    const model = (provider?.models || []).find(item => item.id === modelId);
    const dims = document.querySelector("#embedding-dimensions");
    if (dims && model?.default_dims) dims.value = model.default_dims;
  });
  on("#security-preview-run", "click", () => runSecurityPreview().catch(showError));
  on("#router-form", "submit", event => { event.preventDefault(); saveRouterSettings().catch(showError); });
  on("#connect-env-providers", "click", () => connectEnvProviders().catch(showError));
  on("#test-all-providers", "click", () => testAllProviders().catch(showError));
  document.querySelectorAll("[data-router-preset]").forEach(btn => {
    btn.addEventListener("click", () => {
      const preset = ROUTER_PRESETS[btn.dataset.routerPreset];
      if (!preset) return;
      const strategyMap = { cheap: "cost", fast: "speed", quality: "quality", balanced: "balanced" };
      setRouterWeights(preset, strategyMap[btn.dataset.routerPreset] || "balanced");
    });
  });
  ["#router-w-quality", "#router-w-cost", "#router-w-speed", "#router-w-availability"].forEach(sel => {
    const el = document.querySelector(sel);
    if (el) el.addEventListener("input", syncRouterWeightLabels);
  });
  syncRouterWeightLabels();
  // legacy task-form removed; creation is inline on the board
  const legacyTaskForm = document.querySelector("#task-form");
  if (legacyTaskForm) legacyTaskForm.remove();
  on("#router-preview-form", "submit", event => { event.preventDefault(); previewRouting(document.querySelector("#router-preview-query")?.value || "").catch(showError); });
  on("#code-run-insight", "click", () => runCodeInsight().catch(showError));
  on("#code-connect-folder", "click", openProjectFolderModal);
  on("#code-analyze-project", "click", () => analyzeCodeProject().catch(showError));
  document.querySelectorAll("[data-code-tab]").forEach(button => {
    button.addEventListener("click", () => {
      setCodeInsightTab(button.dataset.codeTab || "symbols");
    });
  });
  on("#code-use-selected-file", "click", () => {
    if (!fillCodeFileFromSelection(true)) showError(new Error(t("code.noFileSelected")));
  });
  on("#terminal-form", "submit", event => runTerminalCommand(event).catch(showError));
  on("#terminal-open", "click", () => openExternalTerminal().catch(showError));
  on("#terminal-clear", "click", () => { state.terminalHistory = []; renderTerminalHistory(); });
  document.addEventListener("click", event => {
    const codeInstall = event.target.closest("[data-code-install]");
    if (codeInstall) {
      event.preventDefault();
      installCodeLanguageServer(codeInstall.dataset.codeInstall, codeInstall).catch(showError);
      return;
    }
    const runButton = event.target.closest("[data-terminal-install-run]");
    if (runButton) {
      event.preventDefault();
      runInstallCommandInTerminal(runButton.dataset.terminalInstallRun).catch(showError);
      return;
    }
    const copyButton = event.target.closest("[data-install-copy], [data-terminal-install-copy]");
    if (copyButton) {
      event.preventDefault();
      const command = copyButton.dataset.command || copyButton.dataset.terminalInstallCopy || "";
      copyInstallCommand(command).catch(showError);
      return;
    }
    const agentButton = event.target.closest("[data-agent-install-command]");
    if (agentButton) {
      event.preventDefault();
      askAgentInstallCommand(agentButton.dataset.agentInstallCommand).catch(showError);
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && projectFolderModal()?.classList.contains("active")) closeProjectFolderModal();
  });
}

// Search Palette functionality
const searchPalette = {
  isOpen: false,
  highlightedIndex: -1,
  currentResults: [],

  getRecentSearches() {
    try {
      return JSON.parse(localStorage.getItem('architectos_recent_searches') || '[]').slice(0, 5);
    } catch {
      return [];
    }
  },

  saveRecentSearch(query) {
    if (!query.trim()) return;
    try {
      let recent = this.getRecentSearches();
      recent = [query, ...recent.filter(q => q !== query)].slice(0, 5);
      localStorage.setItem('architectos_recent_searches', JSON.stringify(recent));
    } catch (error) {
      console.warn('Could not save recent search:', error);
    }
  },

  open() {
    this.isOpen = true;
    const palette = document.getElementById('searchPalette');
    palette.classList.add('active');
    palette.classList.remove('closing');

    const input = document.getElementById('paletteSearchInput');
    setTimeout(() => {
      input.focus();
      input.select();
    }, 100);

    document.body.style.overflow = 'hidden';
    this.renderRecentSearches();
  },

  close() {
    this.isOpen = false;
    const palette = document.getElementById('searchPalette');
    palette.classList.add('closing');

    setTimeout(() => {
      palette.classList.remove('active', 'closing');
      document.body.style.overflow = '';
      const input = document.getElementById('paletteSearchInput');
      input.value = '';
      this.showRecentSection();
    }, 200);
  },

  toggle() {
    if (this.isOpen) {
      this.close();
    } else {
      this.open();
    }
  },

  showRecentSection() {
    document.getElementById('paletteRecentSearches').style.display = 'block';
    document.getElementById('paletteResults').style.display = 'none';
  },

  showResultsSection() {
    document.getElementById('paletteRecentSearches').style.display = 'none';
    document.getElementById('paletteResults').style.display = 'block';
  },

  renderRecentSearches() {
    const recent = this.getRecentSearches();
    const container = document.getElementById('recentSearchesList');

    if (!recent.length) {
      container.innerHTML = '<div class="recent-item" style="cursor: default; opacity: 0.6;">No recent searches</div>';
      return;
    }

    container.innerHTML = recent.map(query => `
      <div class="recent-item" data-recent-query="${escapeHtml(query)}">
        <span class="recent-icon">🕐</span>
        <span>${escapeHtml(query)}</span>
      </div>
    `).join('');

    container.querySelectorAll('.recent-item[data-recent-query]').forEach(item => {
      item.addEventListener('click', () => {
        const query = item.dataset.recentQuery;
        const input = document.getElementById('paletteSearchInput');
        input.value = query;
        this.performSearch(query);
      });
    });
  },

  async performSearch(query) {
    if (!query.trim()) {
      this.showRecentSection();
      return;
    }

    this.showResultsSection();
    this.renderLoading(query);

    const activeFilters = Array.from(document.querySelectorAll('.filter-pill.active'))
      .map(pill => pill.dataset.filterType);

    const requestId = (this._searchRequestId = (this._searchRequestId || 0) + 1);
    try {
      const payload = await api(
        `/api/memory/search?query=${encodeURIComponent(query)}&project_id=${projectParam()}&limit=12&refresh=0`
      );
      if (requestId !== this._searchRequestId) return;

      let results = payload.hits || [];

      // Apply filters
      if (activeFilters.length > 0) {
        results = results.filter(hit => {
          if (activeFilters.includes('favorite')) {
            return hit.node.metadata && hit.node.metadata.favorite;
          }
          return activeFilters.includes(hit.node.type);
        });
      }

      this.currentResults = results;
      this.renderResults(results);
      this.saveRecentSearch(query);
    } catch (error) {
      if (requestId !== this._searchRequestId) return;
      console.error('Search failed:', error);
      this.renderError(error.message);
    }
  },

  renderLoading(query) {
    const container = document.getElementById('paletteResultList');
    const countSpan = document.getElementById('paletteResultCount');
    if (countSpan) countSpan.textContent = '';
    if (!container) return;
    container.innerHTML = `
      <div class="no-palette-results">
        <div class="no-palette-results-icon">⏳</div>
        <div>Searching memory for “${escapeHtml(query.trim())}”…</div>
      </div>
    `;
    this.highlightedIndex = -1;
  },

  getTypeIcon(type) {
    const icons = {
      'Decision': '🎯',
      'Lesson': '💡',
      'Constraint': '⚠️',
      'Feature': '✨',
      'Doc': '📚',
      'Artifact': '📦',
      'Task': '✅',
      'Concept': '💭',
      'Project': '🏗️',
      'Provider': '🔌',
      'Requirement': '📋',
      'Meeting': '🤝'
    };
    return icons[type] || '📝';
  },

  renderResults(results) {
    const container = document.getElementById('paletteResultList');
    const countSpan = document.getElementById('paletteResultCount');
    if (!container || !countSpan) return;

    countSpan.textContent = `(${results.length})`;

    if (!results.length) {
      container.innerHTML = `
        <div class="no-palette-results">
          <div class="no-palette-results-icon">🔍</div>
          <div>No results found</div>
        </div>
      `;
      this.highlightedIndex = -1;
      return;
    }

    container.innerHTML = results.map((hit, index) => {
      const node = hit.node;
      const icon = this.getTypeIcon(node.type);
      const snippet = (node.text || '').substring(0, 140);

      return `
        <div class="palette-result-item ${index === 0 ? 'highlighted' : ''}" data-index="${index}" data-node-id="${escapeHtml(node.id)}">
          <div class="palette-result-icon">${icon}</div>
          <div class="palette-result-content">
            <div class="palette-result-title">${escapeHtml(node.label)}</div>
            <div class="palette-result-meta">
              <span class="badge">${escapeHtml(node.type)}</span>
              <span class="badge">${escapeHtml(node.scope)}</span>
              ${hit.score ? `<span class="badge">score ${hit.score.toFixed(2)}</span>` : ''}
            </div>
            <div class="palette-result-snippet">${escapeHtml(snippet)}${snippet.length >= 140 ? '...' : ''}</div>
          </div>
          <div class="palette-result-actions">
            <kbd>↵</kbd>
          </div>
        </div>
      `;
    }).join('');

    this.highlightedIndex = 0;

    // Bind click events
    container.querySelectorAll('.palette-result-item').forEach(item => {
      item.addEventListener('click', () => {
        this.selectResult(item.dataset.nodeId);
      });
    });
  },

  renderError(message) {
    const container = document.getElementById('paletteResultList');
    container.innerHTML = `
      <div class="no-palette-results">
        <div class="no-palette-results-icon">⚠️</div>
        <div>Search failed: ${escapeHtml(message)}</div>
      </div>
    `;
  },

  selectResult(nodeId) {
    this.close();

    // Switch to memory view
    switchView('memory');

    // Try to focus the node in graph if it exists
    setTimeout(() => {
      if (graphState.nodes.find(n => n.id === nodeId)) {
        const node = graphState.nodes.find(n => n.id === nodeId);
        if (node) {
          openGraphNodeModal(node);
          // Center the graph on this node if possible
          const particle = graphState.particles.find(p => p.id === nodeId);
          if (particle) {
            const canvas = document.querySelector('#graph-canvas');
            if (canvas) {
              graphState.offsetX = canvas.width / 2 - particle.x;
              graphState.offsetY = canvas.height / 2 - particle.y;
            }
          }
        }
      }
    }, 300);
  },

  navigateResults(direction) {
    const items = document.querySelectorAll('.palette-result-item');
    if (!items.length) return;

    const current = document.querySelector('.palette-result-item.highlighted');

    if (current) {
      current.classList.remove('highlighted');
      this.highlightedIndex = parseInt(current.dataset.index);
    }

    if (direction === 'down') {
      this.highlightedIndex = Math.min(this.highlightedIndex + 1, items.length - 1);
    } else {
      this.highlightedIndex = Math.max(this.highlightedIndex - 1, 0);
    }

    items[this.highlightedIndex]?.classList.add('highlighted');
    items[this.highlightedIndex]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  },

  selectHighlighted() {
    const highlighted = document.querySelector('.palette-result-item.highlighted');
    if (highlighted) {
      this.selectResult(highlighted.dataset.nodeId);
    }
  },

  bindEvents() {
    // Open button
    document.getElementById('open-search-palette')?.addEventListener('click', () => this.open());

    // Close on backdrop click
    document.querySelectorAll('[data-close-palette]').forEach(el => {
      el.addEventListener('click', () => this.close());
    });

    // Search input
    const input = document.getElementById('paletteSearchInput');
    let searchTimeout;
    input.addEventListener('input', (e) => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => {
        this.performSearch(e.target.value);
      }, 300);
    });

    // Filter pills
    document.querySelectorAll('.filter-pill').forEach(pill => {
      pill.addEventListener('click', () => {
        pill.classList.toggle('active');
        const input = document.getElementById('paletteSearchInput');
        this.performSearch(input.value);
      });
    });

    // Global keyboard shortcuts
    document.addEventListener('keydown', (e) => {
      // Ctrl/Cmd + K to toggle
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        this.toggle();
        return;
      }

      // Only handle these when palette is open
      if (!this.isOpen) return;

      // Escape to close
      if (e.key === 'Escape') {
        e.preventDefault();
        this.close();
        return;
      }

      // Arrow navigation
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        this.navigateResults(e.key === 'ArrowDown' ? 'down' : 'up');
        return;
      }

      // Enter to select
      if (e.key === 'Enter') {
        e.preventDefault();
        this.selectHighlighted();
        return;
      }
    });
  }
};

// Initialize search palette
searchPalette.bindEvents();

// Tab switching for Add Memory
function initAddMemoryTabs() {
  const tabButtons = document.querySelectorAll('.add-memory-tabs .tab-button');
  const tabContents = document.querySelectorAll('.add-memory-tabs .tab-content');

  tabButtons.forEach(button => {
    button.addEventListener('click', () => {
      const targetTab = button.dataset.tab;

      // Remove active class from all buttons and contents
      tabButtons.forEach(btn => btn.classList.remove('active'));
      tabContents.forEach(content => content.classList.remove('active'));

      // Add active class to clicked button and corresponding content
      button.classList.add('active');
      const targetContent = document.querySelector(`[data-tab-content="${targetTab}"]`);
      if (targetContent) {
        targetContent.classList.add('active');
      }
    });
  });
}

// Initialize tabs on load
initAddMemoryTabs();

async function bootstrap() {
  bindEvents();
  await loadSettings();
  await loadProjects();
  await loadProjectFiles();
  await loadProviders().catch(showError);
  renderMemoryFiles();
  buildContext("memory context builder").catch(showError);
  refreshWorkspace().catch(showError);
  scheduleGraphLoad();
  await maybeShowOnboarding();
  syncStartupMemoryRescan().catch(() => {});
}
bootstrap().catch(showError);

// Language dropdown toggle
function initLanguageDropdown() {
  const languageBtn = document.getElementById('language-btn');
  const languageDropdown = document.querySelector('.language-dropdown');
  const languageMenu = document.getElementById('language-menu');

  if (!languageBtn || !languageDropdown || !languageMenu) return;

  const close = () => {
    languageDropdown.classList.remove('open');
    languageBtn.setAttribute('aria-expanded', 'false');
  };
  const open = () => {
    languageDropdown.classList.add('open');
    languageBtn.setAttribute('aria-expanded', 'true');
    const active = languageMenu.querySelector('[data-language-option].active') || languageMenu.querySelector('[data-language-option]');
    if (active) active.focus();
  };
  const toggle = () => {
    if (languageDropdown.classList.contains('open')) close();
    else open();
  };

  languageBtn.addEventListener('click', event => {
    event.stopPropagation();
    toggle();
  });

  languageMenu.addEventListener('click', event => {
    const option = event.target.closest('[data-language-option]');
    if (!option) return;
    applyLanguage(option.dataset.languageOption);
    saveUiSettings({ language: state.language }).catch(showError);
    close();
    languageBtn.focus();
  });

  languageMenu.addEventListener('keydown', event => {
    const options = Array.from(languageMenu.querySelectorAll('[data-language-option]'));
    const index = options.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      languageBtn.focus();
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const direction = event.key === 'ArrowDown' ? 1 : -1;
      const next = options[(Math.max(index, 0) + direction + options.length) % options.length];
      next.focus();
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      document.activeElement.click();
    }
  });

  document.addEventListener('click', event => {
    if (!event.target.closest('.language-dropdown')) close();
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') close();
  });

  syncLanguageMenu();
}

// Initialize language dropdown
initLanguageDropdown();

// ============================================
// File Tree Rendering
// ============================================

function buildFileTree(files) {
  const tree = { name: 'root', type: 'folder', path: '', children: [], expanded: true };

  files.forEach(file => {
    const parts = file.path.split(/[\\/]/);
    let current = tree;

    parts.forEach((part, idx) => {
      const isLast = idx === parts.length - 1;
      const existingChild = current.children.find(child => child.name === part);

      if (existingChild) {
        current = existingChild;
      } else {
        const explicitType = file.type === 'folder' || file.type === 'directory' ? 'folder' : 'file';
        const newNode = {
          name: part,
          type: isLast ? explicitType : 'folder',
          path: parts.slice(0, idx + 1).join('/'),
          children: isLast && explicitType === 'file' ? undefined : [],
          expanded: false
        };
        current.children.push(newNode);
        if (!isLast || explicitType === 'folder') current = newNode;
      }
    });
  });

  return tree;
}

function getFileExtension(filename) {
  const match = filename.match(/\.[^.]+$/);
  return match ? match[0] : '';
}

function renderFileTreeNode(node, container, level = 0, onSelect) {
  if (node.name === 'root') {
    node.children.forEach(child => renderFileTreeNode(child, container, 0, onSelect));
    return;
  }

  const item = document.createElement('div');
  item.className = 'file-tree-item';
  item.style.paddingLeft = `${level * 20 + 8}px`;
  item.dataset.path = node.path;

  let childrenContainer = null;

  if (node.type === 'folder') {
    item.classList.add('folder');

    const toggle = document.createElement('span');
    toggle.className = 'file-tree-toggle';
    toggle.textContent = '▶';
    if (node.expanded) toggle.classList.add('expanded');

    toggle.addEventListener('click', e => {
      e.stopPropagation();
      node.expanded = !node.expanded;
      toggle.classList.toggle('expanded');
      if (childrenContainer) {
        childrenContainer.classList.toggle('collapsed');
      }
    });
    item.appendChild(toggle);

    const icon = document.createElement('span');
    icon.className = 'file-tree-icon';
    icon.textContent = '📂';
    item.appendChild(icon);
  } else {
    item.classList.add('file');

    const spacer = document.createElement('span');
    spacer.style.width = '16px';
    item.appendChild(spacer);

    const ext = getFileExtension(node.name);
    item.dataset.ext = ext;

    const icon = document.createElement('span');
    icon.className = 'file-tree-icon';
    icon.textContent = '📄';
    item.appendChild(icon);
  }

  const name = document.createElement('span');
  name.className = 'file-tree-name';
  name.textContent = node.name;
  name.title = node.path;
  item.appendChild(name);

  item.addEventListener('click', () => {
    if (node.type === 'file') {
      document.querySelectorAll('.file-tree-item').forEach(el => el.classList.remove('selected'));
      item.classList.add('selected');
      if (onSelect) onSelect(node);
    }
  });

  container.appendChild(item);

  if (node.type === 'folder' && node.children && node.children.length > 0) {
    childrenContainer = document.createElement('div');
    childrenContainer.className = 'file-tree-children';
    if (!node.expanded) childrenContainer.classList.add('collapsed');

    node.children
      .sort((a, b) => {
        if (a.type !== b.type) return a.type === 'folder' ? -1 : 1;
        return a.name.localeCompare(b.name);
      })
      .forEach(child => renderFileTreeNode(child, childrenContainer, level + 1, onSelect));

    container.appendChild(childrenContainer);
  }
}

function renderFileTree(files, container, onSelect) {
  container.innerHTML = '';
  
  if (!files || files.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'file-tree-empty';
    empty.textContent = 'No files found';
    container.appendChild(empty);
    return;
  }

  const tree = buildFileTree(files);
  const treeContainer = document.createElement('div');
  treeContainer.className = 'file-tree';
  renderFileTreeNode(tree, treeContainer, 0, onSelect);
  container.appendChild(treeContainer);
}
// ============================================
// File Editor Logic
// ============================================

const fileEditor = {
  currentFile: null,
  originalContent: '',
  isModified: false,
  openFiles: new Map(), // path -> { content, modified, element }

  init() {
    this.bindEvents();
    this.setupContextMenu();
    this.setupKeyboardShortcuts();
  },

  bindEvents() {
    // File open is handled on single click in renderFileTree onSelect

    // Editor textarea change
    const editor = document.getElementById('file-editor');
    if (editor) {
      editor.addEventListener('input', () => {
        this.markAsModified();
      });
    }

    // Save button
    const saveBtn = document.getElementById('editor-save');
    if (saveBtn) {
      saveBtn.addEventListener('click', () => this.saveFile());
    }

    // Close button
    const closeBtn = document.getElementById('editor-close');
    if (closeBtn) {
      closeBtn.addEventListener('click', () => this.closeFile());
    }

    // New file button
    const newFileBtn = document.getElementById('new-file-btn');
    if (newFileBtn) {
      newFileBtn.addEventListener('click', () => this.createNewFile());
    }

    // New folder button
    const newFolderBtn = document.getElementById('new-folder-btn');
    if (newFolderBtn) {
      newFolderBtn.addEventListener('click', () => this.createNewFolder());
    }

    // Toggle context builder
    const toggleBtn = document.getElementById('toggle-context-builder');
    const contextSection = document.querySelector('.context-builder-section');
    if (toggleBtn && contextSection) {
      toggleBtn.addEventListener('click', () => {
        contextSection.classList.toggle('collapsed');
      });
    }
  },

  setupKeyboardShortcuts() {
    document.addEventListener('keydown', (e) => {
      // Ctrl+S / Cmd+S - Save
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        if (this.currentFile) {
          this.saveFile();
        }
      }

      // Ctrl+W / Cmd+W - Close file
      if ((e.ctrlKey || e.metaKey) && e.key === 'w') {
        e.preventDefault();
        if (this.currentFile) {
          this.closeFile();
        }
      }
    });
  },

  async openFile(path) {
    try {
      const editor = document.getElementById('file-editor');
      const pathEl = document.getElementById('editor-file-path');
      const statusEl = document.getElementById('editor-file-status');
      const saveBtn = document.getElementById('editor-save');
      const closeBtn = document.getElementById('editor-close');

      if (!editor) return;

      // Load file content
      editor.dataset.loading = 'true';
      const payload = await api(`/api/project/file?project_id=${projectParam()}&path=${encodeURIComponent(path)}`);

      this.currentFile = path;
      this.originalContent = payload.text || '';
      this.isModified = false;

      editor.value = this.originalContent;
      editor.dataset.loading = 'false';
      editor.disabled = false;

      if (pathEl) pathEl.textContent = path;
      if (statusEl) {
        statusEl.textContent = '';
        statusEl.className = 'editor-status';
      }

      if (saveBtn) saveBtn.disabled = false;
      if (closeBtn) closeBtn.disabled = false;

      // Set language for potential syntax highlighting
      const ext = this.getFileExtension(path);
      editor.dataset.language = this.getLanguageFromExtension(ext);

      // Add to open files
      this.addTab(path);

      // Update UI
      this.updateEditorUI();
      state.selectedFile = path;
      if (typeof workspaceChat !== "undefined") {
        workspaceChat.syncFileChip?.();
        if (workspaceChat.surface === "ask") workspaceChat.applySurface("split");
      }

    } catch (error) {
      showError(error);
    }
  },

  async saveFile() {
    if (!this.currentFile) return;

    try {
      const editor = document.getElementById('file-editor');
      const statusEl = document.getElementById('editor-file-status');

      if (!editor) return;

      const content = editor.value;

      // Call API to save file
      await api('/api/project/file/save', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: this.currentFile,
          text: content
        })
      });

      this.originalContent = content;
      this.isModified = false;

      if (statusEl) {
        statusEl.textContent = 'Saved';
        statusEl.className = 'editor-status saved';
        setTimeout(() => {
          statusEl.textContent = '';
          statusEl.className = 'editor-status';
        }, 2000);
      }

      this.updateEditorUI();

      // Refresh file tree
      await loadProjectFiles();

    } catch (error) {
      showError(error);
    }
  },

  closeFile() {
    if (this.isModified) {
      if (!confirm('File has unsaved changes. Close anyway?')) {
        return;
      }
    }

    const editor = document.getElementById('file-editor');
    const pathEl = document.getElementById('editor-file-path');
    const statusEl = document.getElementById('editor-file-status');
    const saveBtn = document.getElementById('editor-save');
    const closeBtn = document.getElementById('editor-close');

    this.currentFile = null;
    this.originalContent = '';
    this.isModified = false;

    if (editor) {
      editor.value = '';
      editor.disabled = true;
      delete editor.dataset.loading;
      delete editor.dataset.language;
    }

    if (pathEl) pathEl.textContent = 'Select or create a file';
    if (statusEl) {
      statusEl.textContent = '';
      statusEl.className = 'editor-status';
    }

    if (saveBtn) saveBtn.disabled = true;
    if (closeBtn) closeBtn.disabled = true;

    this.removeTab(this.currentFile);
    this.updateEditorUI();
  },

  markAsModified() {
    const editor = document.getElementById('file-editor');
    const statusEl = document.getElementById('editor-file-status');

    if (!editor || !this.currentFile) return;

    const currentContent = editor.value;
    this.isModified = currentContent !== this.originalContent;

    if (statusEl) {
      if (this.isModified) {
        statusEl.textContent = 'Modified';
        statusEl.className = 'editor-status modified';
      } else {
        statusEl.textContent = '';
        statusEl.className = 'editor-status';
      }
    }

    this.updateEditorUI();
  },

  async createNewFile() {
    const fileName = prompt('Enter file name (with extension):');
    if (!fileName) return;

    try {
      // Create empty file
      await api('/api/project/file/save', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: fileName,
          text: ''
        })
      });

      // Refresh file tree and open the new file
      await loadProjectFiles();
      await this.openFile(fileName);

    } catch (error) {
      showError(error);
    }
  },

  async createNewFolder() {
    const folderName = prompt('Enter folder name:');
    if (!folderName) return;

    try {
      // Create folder by creating a .gitkeep file inside
      await api('/api/project/file/save', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: `${folderName}/.gitkeep`,
          text: ''
        })
      });

      // Refresh file tree
      await loadProjectFiles();

    } catch (error) {
      showError(error);
    }
  },

  async deleteFile(path) {
    if (!confirm(`Delete ${path}?`)) return;

    try {
      await api('/api/project/file/delete', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: path
        })
      });

      if (this.currentFile === path) {
        this.closeFile();
      }

      await loadProjectFiles();

    } catch (error) {
      showError(error);
    }
  },

  async renameFile(oldPath) {
    const newPath = prompt('Enter new name:', oldPath);
    if (!newPath || newPath === oldPath) return;

    try {
      // Read current content
      const payload = await api(`/api/project/file?project_id=${projectParam()}&path=${encodeURIComponent(oldPath)}`);

      // Create new file with same content
      await api('/api/project/file/save', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: newPath,
          text: payload.text
        })
      });

      // Delete old file
      await api('/api/project/file/delete', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: oldPath
        })
      });

      if (this.currentFile === oldPath) {
        this.closeFile();
        await this.openFile(newPath);
      }

      await loadProjectFiles();

    } catch (error) {
      showError(error);
    }
  },

  setupContextMenu() {
    const contextMenu = document.getElementById('file-context-menu');
    if (!contextMenu) return;

    let targetPath = null;

    // Right-click on file tree item
    document.addEventListener('contextmenu', (e) => {
      const fileItem = e.target.closest('.file-tree-item');
      if (fileItem && fileItem.dataset.path) {
        e.preventDefault();
        targetPath = fileItem.dataset.path;

        // Position context menu
        contextMenu.style.left = `${e.pageX}px`;
        contextMenu.style.top = `${e.pageY}px`;
        contextMenu.dataset.visible = 'true';

        // Highlight target
        document.querySelectorAll('.file-tree-item').forEach(el => el.classList.remove('context-target'));
        fileItem.classList.add('context-target');
      }
    });

    // Click outside to close
    document.addEventListener('click', () => {
      contextMenu.dataset.visible = 'false';
      document.querySelectorAll('.file-tree-item').forEach(el => el.classList.remove('context-target'));
    });

    // Context menu actions
    contextMenu.addEventListener('click', async (e) => {
      const action = e.target.closest('[data-action]')?.dataset.action;
      if (!action || !targetPath) return;

      contextMenu.dataset.visible = 'false';

      switch (action) {
        case 'open':
          await this.openFile(targetPath);
          break;
        case 'rename':
          await this.renameFile(targetPath);
          break;
        case 'delete':
          await this.deleteFile(targetPath);
          break;
        case 'copy-path':
          await navigator.clipboard.writeText(targetPath);
          break;
      }
    });
  },

  addTab(path) {
    const tabsContainer = document.getElementById('editor-tabs');
    if (!tabsContainer) return;

    // Remove placeholder
    const placeholder = tabsContainer.querySelector('.editor-tab-placeholder');
    if (placeholder) placeholder.remove();

    // Check if tab already exists
    if (tabsContainer.querySelector(`[data-tab-path="${path}"]`)) return;

    const tab = document.createElement('div');
    tab.className = 'editor-tab active';
    tab.dataset.tabPath = path;

    const ext = this.getFileExtension(path);
    const icon = this.getFileIcon(ext);

    tab.innerHTML = `
      <span class="editor-tab-icon">${icon}</span>
      <span class="editor-tab-name">${path.split('/').pop()}</span>
      <button class="editor-tab-close" aria-label="Close">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    `;

    // Tab click to switch
    tab.addEventListener('click', (e) => {
      if (!e.target.closest('.editor-tab-close')) {
        this.openFile(path);
      }
    });

    // Close button
    on(tab.querySelector('.editor-tab-close'), 'click', (e) => {
      e.stopPropagation();
      if (this.currentFile === path) {
        this.closeFile();
      } else {
        tab.remove();
      }
    });

    tabsContainer.appendChild(tab);
  },

  removeTab(path) {
    const tab = document.querySelector(`[data-tab-path="${path}"]`);
    if (tab) tab.remove();

    const tabsContainer = document.getElementById('editor-tabs');
    if (tabsContainer && tabsContainer.children.length === 0) {
      tabsContainer.innerHTML = '<div class="editor-tab-placeholder"><span>No files open</span></div>';
    }
  },

  updateEditorUI() {
    // Update tab modified state
    const tabs = document.querySelectorAll('.editor-tab');
    tabs.forEach(tab => {
      if (tab.dataset.tabPath === this.currentFile) {
        tab.classList.add('active');
        if (this.isModified) {
          tab.classList.add('modified');
        } else {
          tab.classList.remove('modified');
        }
      } else {
        tab.classList.remove('active');
      }
    });
  },

  getFileExtension(path) {
    const match = path.match(/\.([^.]+)$/);
    return match ? match[1].toLowerCase() : '';
  },

  getLanguageFromExtension(ext) {
    const langMap = {
      'py': 'python',
      'js': 'javascript',
      'ts': 'typescript',
      'jsx': 'javascript',
      'tsx': 'typescript',
      'json': 'json',
      'html': 'html',
      'css': 'css',
      'scss': 'scss',
      'md': 'markdown',
      'sql': 'sql',
      'sh': 'shell',
      'yml': 'yaml',
      'yaml': 'yaml'
    };
    return langMap[ext] || 'text';
  },

  getFileIcon(ext) {
    const iconMap = {
      'py': '🐍',
      'js': '📜',
      'ts': '📜',
      'jsx': '⚛️',
      'tsx': '⚛️',
      'json': '📦',
      'md': '📝',
      'html': '🌐',
      'css': '🎨',
      'scss': '🎨',
      'png': '🖼️',
      'jpg': '🖼️',
      'svg': '🖼️',
      'pdf': '📄',
      'txt': '📄'
    };
    return iconMap[ext] || '📄';
  }
};

// Initialize file editor when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => fileEditor.init());
} else {
  fileEditor.init();
}

// Export for use in other modules
window.fileEditor = fileEditor;
// ============================================
// New Project Wizard Logic
// ============================================

const projectWizard = {
  firstRun: false,
  projectData: {},

  init() {
    this.bindEvents();
    this.loadCurrentProject();
  },

  bindEvents() {
    // New Project button
    const newProjectBtn = document.getElementById('new-project-btn');
    if (newProjectBtn) {
      newProjectBtn.addEventListener('click', () => this.openWizard());
    }

    // Project Settings button
    const settingsBtn = document.getElementById('project-settings-btn');
    if (settingsBtn) {
      settingsBtn.addEventListener('click', () => this.openSettings());
    }

    const wizardSkip = document.getElementById('wizard-skip');
    const wizardCancel = document.getElementById('wizard-cancel');
    const wizardFinish = document.getElementById('wizard-finish');

    if (wizardSkip) wizardSkip.addEventListener('click', () => {
      if (this.firstRun) {
        markOnboardingComplete().catch(showError);
      }
      this.closeModal('new-project-wizard');
    });
    if (wizardCancel) wizardCancel.addEventListener('click', () => this.closeModal('new-project-wizard'));
    if (wizardFinish) wizardFinish.addEventListener('click', () => this.finishWizard());

    // Browse folder
    const browseFolderBtn = document.getElementById('wizard-browse-folder');
    if (browseFolderBtn) {
      browseFolderBtn.addEventListener('click', () => this.browseFolder());
    }

    // Auto-fill project name from folder path
    const folderPathInput = document.getElementById('wizard-folder-path');
    const projectNameInput = document.getElementById('wizard-project-name');
    const syncProjectName = () => {
      const path = folderPathInput?.value?.trim();
      if (!path || !projectNameInput) return;
      const folderName = path.split(/[/\\]/).filter(Boolean).pop();
      if (folderName) projectNameInput.value = folderName;
    };
    if (folderPathInput) {
      folderPathInput.addEventListener('change', syncProjectName);
      folderPathInput.addEventListener('blur', syncProjectName);
    }

    // Modal close handlers
    document.querySelectorAll('[data-close-modal]').forEach(el => {
      el.addEventListener('click', (e) => {
        const modal = e.target.closest('.modal');
        if (modal) this.closeModal(modal.id);
      });
    });

    // Settings actions
    this.bindSettingsActions();
  },

  bindSettingsActions() {
    const initBtn = document.getElementById('settings-init-project');
    const scanBtn = document.getElementById('settings-scan-project');
    const reindexBtn = document.getElementById('settings-reindex-project');
    const buildContextBtn = document.getElementById('settings-build-context');
    const saveBtn = document.getElementById('settings-save');
    const browsePathBtn = document.getElementById('settings-browse-path');

    if (initBtn) initBtn.addEventListener('click', () => this.initProject());
    if (scanBtn) scanBtn.addEventListener('click', () => this.scanProject());
    if (reindexBtn) reindexBtn.addEventListener('click', () => this.reindexProject());
    if (buildContextBtn) buildContextBtn.addEventListener('click', () => this.buildContext());
    if (saveBtn) saveBtn.addEventListener('click', () => this.saveSettings());
    if (browsePathBtn) browsePathBtn.addEventListener('click', () => this.browseSettingsPath());
  },

  async loadCurrentProject() {
    try {
      // Load from state or API
      if (state.projectId && state.projectId !== 'architectos') {
        const projectNameEl = document.getElementById('current-project-name');
        if (projectNameEl) {
          projectNameEl.textContent = state.projectId;
        }
      }
    } catch (error) {
      console.error('Failed to load current project:', error);
    }
  },

  openWizard(options = {}) {
    this.firstRun = Boolean(options.firstRun);
    this.projectData = {};
    const title = document.getElementById('wizard-title');
    const lead = document.getElementById('wizard-lead');
    const skipBtn = document.getElementById('wizard-skip');
    const cancelBtn = document.getElementById('wizard-cancel');
    const finishBtn = document.getElementById('wizard-finish');
    const advanced = document.querySelector('#new-project-wizard .wizard-advanced');

    if (title) title.textContent = this.firstRun ? t('wizard.welcomeTitle') : t('wizard.title');
    if (lead) lead.textContent = this.firstRun ? t('wizard.welcomeLead') : t('wizard.lead');
    if (skipBtn) skipBtn.hidden = !this.firstRun;
    if (cancelBtn) cancelBtn.hidden = this.firstRun;
    if (finishBtn) finishBtn.textContent = this.firstRun ? t('wizard.getStarted') : t('wizard.create');
    if (advanced) advanced.open = false;

    const folderPathInput = document.getElementById('wizard-folder-path');
    const projectNameInput = document.getElementById('wizard-project-name');
    if (folderPathInput) folderPathInput.value = '';
    if (projectNameInput) projectNameInput.value = '';
    setWizardFolderStatus('');

    this.openModal('new-project-wizard');
  },

  openSettings() {
    // Load current project settings
    const project = currentProject();
    const projectName = document.getElementById('settings-project-name');
    const projectPath = document.getElementById('settings-project-path');

    if (projectName) {
      projectName.value = displayProjectName(project);
    }
    if (projectPath) {
      projectPath.value = project?.root_path || "";
    }

    this.openModal('project-settings-modal');
  },

  openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
      modal.removeAttribute('hidden');
      document.body.style.overflow = 'hidden';
    }
  },

  closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
      modal.setAttribute('hidden', '');
      document.body.style.overflow = '';
    }
  },

  async browseFolder() {
    const browseBtn = document.getElementById('wizard-browse-folder');
    const folderPathInput = document.getElementById('wizard-folder-path');
    if (browseBtn) browseBtn.disabled = true;
    try {
      setWizardFolderStatus(t("wizard.pickerWaiting"));
      const path = await pickProjectFolder({
        initialPath: folderPathInput?.value || '',
        onStatus: setWizardFolderStatus,
      });
      if (!path || !folderPathInput) return;
      folderPathInput.value = path;
      folderPathInput.dispatchEvent(new Event('change'));
    } catch (error) {
      setWizardFolderStatus(error.message || t('wizard.pickerUnavailable'), 'error');
    } finally {
      if (browseBtn) browseBtn.disabled = false;
    }
  },

  async browseSettingsPath() {
    try {
      const pathInput = document.getElementById('settings-project-path');
      const path = await pickProjectFolder({ initialPath: pathInput?.value || '' });
      if (path && pathInput) pathInput.value = path;
    } catch (error) {
      showError(error);
    }
  },

  async finishWizard() {
    try {
      // Gather all wizard data
      const folderPath = document.getElementById('wizard-folder-path')?.value;
      const projectName = document.getElementById('wizard-project-name')?.value;
      const codeStyle = document.getElementById('wizard-code-style')?.value;
      const ignorePatterns = document.getElementById('wizard-ignore-patterns')?.value;
      const autoIndex = this.firstRun ? true : (document.getElementById('wizard-auto-index')?.checked ?? true);
      const autoMemory = document.getElementById('wizard-auto-memory')?.checked;
      const memoryScope = document.getElementById('wizard-memory-scope')?.value;

      if (!folderPath || !projectName) {
        showSnackbar(t('wizard.validation'), 'error');
        return;
      }

      // Create project via API
      const response = await api('/api/projects', {
        method: 'POST',
        body: JSON.stringify({
          name: projectName,
          root_path: folderPath,
          config: {
            code_style: codeStyle,
            ignore_patterns: ignorePatterns?.split('\n').filter(p => p.trim()),
            auto_index: autoIndex,
            auto_memory: autoMemory,
            memory_scope: memoryScope
          }
        })
      });

      // Update UI
      const actualProjectId = response.project_id || response.id || (response.project && response.project.id) || projectName;
      state.projectId = actualProjectId;
      await saveWorkspaceSettings({ current_project_id: state.projectId });
      state.selectedFile = "";

      const projectNameEl = document.getElementById('current-project-name');
      if (projectNameEl) {
        projectNameEl.textContent = projectName;
      }

      await markOnboardingComplete();

      if (autoIndex) {
        try {
          await api('/api/project/scan', {
            method: 'POST',
            body: JSON.stringify({ project_id: state.projectId, root_path: folderPath, name: projectName, limit: 80, reindex: true, rebuild_links: true })
          });
        } catch (scanError) {
          console.warn('Auto-index after wizard failed:', scanError);
        }
      }

      // Close wizard
      this.closeModal('new-project-wizard');

      // Refresh workspace
      await loadProjects();
      await loadProjectFiles();

      // Show success message
      console.log(`Project created: ${projectName} (ID: ${actualProjectId})`);
      showSnackbar(response.message || `Project ${projectName} was created successfully.`, response.created === false ? 'info' : 'success');

    } catch (error) {
      showError(error);
    }
  },

  async initProject() {
    try {
      const projectName = document.getElementById('settings-project-name')?.value;
      if (!projectName) {
        showSnackbar('Please enter a project name', 'error');
        return;
      }

      await api('/api/project/scan', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          init: true
        })
      });

      showSnackbar('Project initialized successfully.', 'success');
      await loadProjectFiles();
    } catch (error) {
      showError(error);
    }
  },

  async scanProject() {
    try {
      await api('/api/project/scan', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId
        })
      });

      showSnackbar('Project scanned successfully.', 'success');
      await loadProjectFiles();
    } catch (error) {
      showError(error);
    }
  },

  async reindexProject() {
    try {
      await api('/api/project/scan', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          reindex: true
        })
      });

      showSnackbar('Project reindexed successfully.', 'success');
      await loadProjectFiles();
    } catch (error) {
      showError(error);
    }
  },

  async buildContext() {
    try {
      const query = document.getElementById('settings-context-query')?.value;
      if (!query) {
        showSnackbar('Please enter a context query', 'error');
        return;
      }

      const response = await api('/api/project/context', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          query: query
        })
      });

      console.log('Context built:', response.context);
      showSnackbar('Context built successfully.', 'success');
    } catch (error) {
      showError(error);
    }
  },

  async saveSettings() {
    try {
      const projectName = document.getElementById('settings-project-name')?.value;
      const projectPath = document.getElementById('settings-project-path')?.value;

      if (!projectName || !projectPath) {
        showSnackbar('Please select a project folder and enter a project name', 'error');
        return;
      }

      const project = await api('/api/projects', {
        method: 'POST',
        body: JSON.stringify({ name: projectName, root_path: projectPath })
      });
      state.projectId = project.project_id || project.id || (project.project && project.project.id);
      if (!state.projectId) throw new Error("Project was created but no project id was returned.");
      await saveWorkspaceSettings({ current_project_id: state.projectId });
      state.selectedFile = "";
      await loadProjects();
      await loadProjectFiles();

      this.closeModal('project-settings-modal');
      showSnackbar(project.message || 'Project settings saved successfully.', project.created === false ? 'info' : 'success');
    } catch (error) {
      showError(error);
    }
  }
};

// Initialize wizard when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => projectWizard.init());
} else {
  projectWizard.init();
}

// Export for use in other modules
window.projectWizard = projectWizard;
// ============================================
// Workspace Chat Panel Logic
// ============================================

const workspaceChat = {
  isThinking: false,
  isSending: false,
  surface: "split",
  askWidth: 0,
  _streamEl: null,

  init() {
    this.surface = this.loadSurface();
    this.bindEvents();
    this.applySurface(this.surface, { persist: false });
    this.syncProviders();
    this.syncControlsFromAsk();
    this.refreshThread();
    this.syncFileChip();
  },

  loadSurface() {
    const saved = String(localStorage.getItem("workspace-surface-mode") || "").trim();
    if (["editor", "ask", "split"].includes(saved)) return saved;
    return window.matchMedia("(max-width: 768px)").matches ? "ask" : "split";
  },

  applySurface(mode, { persist = true } = {}) {
    const next = ["editor", "ask", "split"].includes(mode) ? mode : "split";
    this.surface = next;
    const layout = document.getElementById("workspace-layout");
    if (layout) layout.dataset.surface = next;
    document.querySelectorAll("[data-workspace-surface]").forEach((btn) => {
      const active = btn.dataset.workspaceSurface === next;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    if (persist) localStorage.setItem("workspace-surface-mode", next);
    if (next === "ask" || next === "split") {
      window.setTimeout(() => document.getElementById("workspace-chat-input")?.focus(), 0);
    }
  },

  bindEvents() {
    document.querySelectorAll("[data-workspace-surface]").forEach((btn) => {
      btn.addEventListener("click", () => this.applySurface(btn.dataset.workspaceSurface));
    });

    const chatForm = document.getElementById("workspace-chat-form");
    if (chatForm) {
      chatForm.addEventListener("submit", (e) => {
        e.preventDefault();
        this.sendMessage().catch(showError);
      });
    }

    const chatInput = document.getElementById("workspace-chat-input");
    if (chatInput) {
      chatInput.addEventListener("input", () => this.autoResizeInput(chatInput));
      chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          this.sendMessage().catch(showError);
        }
      });
    }

    const plusBtn = document.getElementById("workspace-chat-plus");
    const menu = document.getElementById("workspace-chat-menu");
    if (plusBtn && menu) {
      plusBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        menu.hidden = !menu.hidden;
      });
      document.addEventListener("click", (e) => {
        if (!e.target.closest(".chat-menu-wrap")) menu.hidden = true;
      });
      menu.addEventListener("click", (e) => {
        const action = e.target.closest("[data-action]")?.dataset.action;
        if (action === "attach") {
          this.openFilePicker();
          menu.hidden = true;
        } else if (action === "council") {
          this.openInAsk("council");
          menu.hidden = true;
        } else if (action === "open-ask") {
          this.openInAsk();
          menu.hidden = true;
        }
      });
    }

    const attachBtn = document.getElementById("workspace-chat-attach");
    if (attachBtn) attachBtn.addEventListener("click", () => this.openFilePicker());

    const fileInput = document.getElementById("workspace-chat-file-input");
    if (fileInput) fileInput.addEventListener("change", (e) => this.handleFileSelect(e).catch(showError));

    const clearBtn = document.getElementById("chat-clear-btn");
    if (clearBtn) clearBtn.addEventListener("click", () => this.clearChat());

    const expandBtn = document.getElementById("workspace-open-ask");
    if (expandBtn) expandBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      this.openInAsk();
    });

    const fileChip = document.getElementById("workspace-ask-focus-file");
    if (fileChip) {
      fileChip.addEventListener("click", () => {
        this.applySurface("editor");
        document.getElementById("file-editor")?.focus();
      });
    }

    const provider = document.getElementById("workspace-chat-provider");
    if (provider) provider.addEventListener("change", () => this.syncControlsToAsk());

    const remember = document.getElementById("workspace-chat-remember");
    if (remember) remember.addEventListener("change", () => this.syncControlsToAsk());

    const approve = document.getElementById("workspace-chat-approve-cli");
    if (approve) approve.addEventListener("change", () => this.syncControlsToAsk());

    this.bindAskResizer();

    document.addEventListener("keydown", (e) => {
      const meta = e.metaKey || e.ctrlKey;
      if (!meta || !e.shiftKey || String(e.key || "").toLowerCase() !== "a") return;
      if (e.target && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
      e.preventDefault();
      const order = ["editor", "split", "ask"];
      const idx = order.indexOf(this.surface);
      this.applySurface(order[(idx + 1) % order.length]);
    });
  },

  bindAskResizer() {
    const resizer = document.getElementById("workspace-ask-resizer");
    const panel = document.getElementById("workspace-chat-panel");
    const stage = document.querySelector(".workspace-stage-body");
    if (!resizer || !panel || !stage) return;
    const saved = Number(localStorage.getItem("workspace-ask-width") || 0);
    if (saved >= 280) {
      panel.style.width = `${saved}px`;
      this.askWidth = saved;
    }
    let dragging = false;
    const onMove = (event) => {
      if (!dragging) return;
      const rect = stage.getBoundingClientRect();
      const width = Math.round(rect.right - event.clientX);
      const clamped = Math.max(280, Math.min(Math.floor(rect.width * 0.7), width));
      panel.style.width = `${clamped}px`;
      this.askWidth = clamped;
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove("is-dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      if (this.askWidth) localStorage.setItem("workspace-ask-width", String(this.askWidth));
    };
    resizer.addEventListener("mousedown", (event) => {
      if (this.surface !== "split") return;
      event.preventDefault();
      dragging = true;
      resizer.classList.add("is-dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    });
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  },

  syncFileChip() {
    const chip = document.getElementById("workspace-ask-focus-file");
    if (!chip) return;
    const path = String(state.selectedFile || "").trim();
    if (!path) {
      chip.hidden = true;
      chip.textContent = "";
      return;
    }
    const name = path.split(/[\\/]/).pop() || path;
    chip.hidden = false;
    chip.textContent = name;
    chip.title = path;
  },

  autoResizeInput(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 160) + "px";
  },

  syncProviders() {
    const providerSelect = document.getElementById("workspace-chat-provider");
    const mainProviderSelect = document.getElementById("chat-provider");
    if (providerSelect && mainProviderSelect && mainProviderSelect.options.length) {
      const current = providerSelect.value || mainProviderSelect.value || "auto";
      providerSelect.innerHTML = mainProviderSelect.innerHTML;
      providerSelect.value = current;
      if (!providerSelect.value) providerSelect.value = "auto";
    }
  },

  syncControlsFromAsk() {
    const pairs = [
      ["#chat-provider", "#workspace-chat-provider"],
      ["#chat-remember", "#workspace-chat-remember"],
      ["#chat-approve-cli", "#workspace-chat-approve-cli"],
    ];
    for (const [fromSel, toSel] of pairs) {
      const from = document.querySelector(fromSel);
      const to = document.querySelector(toSel);
      if (!from || !to) continue;
      if (to.tagName === "SELECT") to.value = from.value;
      else to.checked = from.checked;
    }
  },

  syncControlsToAsk() {
    const pairs = [
      ["#workspace-chat-provider", "#chat-provider"],
      ["#workspace-chat-remember", "#chat-remember"],
      ["#workspace-chat-approve-cli", "#chat-approve-cli"],
    ];
    for (const [fromSel, toSel] of pairs) {
      const from = document.querySelector(fromSel);
      const to = document.querySelector(toSel);
      if (!from || !to) continue;
      if (from.tagName === "SELECT") to.value = from.value;
      else to.checked = from.checked;
    }
    if (typeof syncAskMode === "function") syncAskMode();
  },

  setStatus(text, tone = "") {
    const statusEl = document.getElementById("chat-status");
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = `chat-status-indicator ${tone}`.trim();
  },

  showEmptyState() {
    if (this.isSending) return;
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    chatThread.innerHTML = `
      <div class="chat-empty-state">
        <div class="chat-empty-icon">💬</div>
        <div class="chat-empty-text">${escapeHtml(t("workspace.ask.empty"))}</div>
        <div class="chat-empty-hint">${escapeHtml(t("workspace.ask.emptyHint"))}</div>
      </div>`;
  },

  renderFromChat(chat) {
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    const messages = (chat && chat.messages) || [];
    if (!messages.length) {
      this.showEmptyState();
      return;
    }
    const frag = document.createDocumentFragment();
    messages.forEach((message, index) => {
      frag.appendChild(this.buildThreadMessage(message, chat.id, index));
    });
    chatThread.replaceChildren(frag);
    this._streamEl = null;
    this._streamWrap = null;
    chatThread.scrollTop = chatThread.scrollHeight;
  },

  buildThreadMessage(message, chatId, index) {
    const role = message.role || "assistant";
    if (role === "user") {
      return this.buildMessageElement("user", message.text || "", false, null);
    }

    const wrap = document.createElement("div");
    wrap.className = "workspace-stream-block";

    const persona = document.createElement("div");
    persona.className = "msg-persona";
    wrap.appendChild(persona);

    const bubble = document.createElement("div");
    bubble.className = "message assistant workspace-ask-message";
    const textNode = document.createElement("div");
    textNode.className = "message-text";
    bubble.appendChild(textNode);
    bubble._persona = persona;
    wrap.appendChild(bubble);

    if (isProviderError(message.provider)) {
      setBubbleProvider(bubble, message.provider);
      renderProviderErrorBubble(bubble, message.text || "", message.provider);
    } else {
      setBubbleProvider(bubble, message.provider);
      renderAssistantRichContent(bubble, message.raw_text || message.text || "", message.structured || null);
      if (Array.isArray(message.tool_trace) && message.tool_trace.length) {
        finalizeAgentActivity(bubble, message.tool_trace);
      }
      setBubbleUsage(bubble, message.usage);
    }
    return wrap;
  },

  async refreshThread(options = {}) {
    const allowEmpty = options.allowEmpty !== false;
    try {
      const payload = await api(`/api/chats?project_id=${projectParam()}&limit=80`);
      const chats = payload.chats || [];
      const summary = (state.chatId && chats.find(item => item.id === state.chatId)) || null;
      if (summary) {
        const chat = await fetchChat(summary.id);
        state.chatId = chat.id;
        this.renderFromChat(chat);
        return;
      }
      if (allowEmpty && !this.isSending) this.showEmptyState();
    } catch (error) {
      if (allowEmpty && !this.isSending) this.showEmptyState();
    }
  },

  beginStream(userMessage) {
    this.isSending = true;
    this.setStatus("Working…", "thinking");
    this.addMessage("user", userMessage);
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    chatThread.querySelector(".chat-empty-state")?.remove();

    const wrap = document.createElement("div");
    wrap.className = "workspace-stream-block";

    const persona = document.createElement("div");
    persona.className = "msg-persona";
    wrap.appendChild(persona);

    const bubble = document.createElement("div");
    bubble.className = "message assistant streaming workspace-ask-message";
    const textNode = document.createElement("div");
    textNode.className = "message-text";
    bubble.appendChild(textNode);
    bubble._persona = persona;
    wrap.appendChild(bubble);

    chatThread.appendChild(wrap);
    this._streamEl = bubble;
    this._streamWrap = wrap;
    beginAgentActivity(bubble);
    chatThread.scrollTop = chatThread.scrollHeight;
  },

  onStreamStart(event) {
    if (!this._streamEl) return;
    if (event?.provider) {
      setBubbleProvider(this._streamEl, event.provider);
      setAgentActivityModel(this._streamEl, event.provider);
      this.setStatus(providerLabel(event.provider) || "Working…", "thinking");
    }
    beginAgentActivity(this._streamEl);
  },

  onStreamDelta(text) {
    if (!this._streamEl) return;
    const node = getMessageTextElement(this._streamEl);
    if (!node) return;
    node.textContent += text || "";
    const chatThread = document.getElementById("workspace-chat-thread");
    if (chatThread) chatThread.scrollTop = chatThread.scrollHeight;
  },

  onStreamProgress(event) {
    if (!this._streamEl || !event) return;
    updateAgentActivity(this._streamEl, event);
    if (event.provider) {
      this.setStatus(providerLabel(event.provider) || event.status || "Working…", "thinking");
    } else if (event.status) {
      this.setStatus(String(event.status).slice(0, 48), "thinking");
    }
    const chatThread = document.getElementById("workspace-chat-thread");
    if (chatThread) chatThread.scrollTop = chatThread.scrollHeight;
  },

  onStreamDone(event) {
    this.isSending = false;
    this.setStatus("Ready");
    if (this._streamEl) {
      this._streamEl.classList.remove("streaming");
      if (event?.provider) setBubbleProvider(this._streamEl, event.provider);
      if (isProviderError(event?.provider)) {
        renderProviderErrorBubble(
          this._streamEl,
          event.response || getMessageTextElement(this._streamEl)?.textContent || "",
          event.provider,
        );
      } else {
        const finalText = event?.response || getMessageTextElement(this._streamEl)?.textContent || "";
        const withStderr = event?.raw?.stderr ? `${finalText}\n\nstderr: ${event.raw.stderr}` : finalText;
        renderAssistantRichContent(this._streamEl, withStderr, event?.structured || null);
        finalizeAgentActivity(this._streamEl, event?.tool_trace);
        if (event?.usage) setBubbleUsage(this._streamEl, event.usage);
      }
    }
    // Keep the live bubble with activity; only replace if we need full history sync.
    if (event?.chat?.messages?.length) {
      this.renderFromChat(event.chat);
    }
    this._streamEl = null;
    this._streamWrap = null;
  },

  onStreamError(error) {
    this.isSending = false;
    if (this._streamEl) {
      this._streamEl.classList.remove("streaming");
      const node = getMessageTextElement(this._streamEl);
      if (node) node.textContent += `\n${error || "stream error"}`;
      stopAgentActivityTimer(this._streamEl.querySelector(".agent-activity"));
      finalizeAgentActivity(this._streamEl, []);
    }
    this.setStatus("Error", "error");
    window.setTimeout(() => this.setStatus("Ready"), 3000);
  },

  openInAsk(mode) {
    this.syncControlsToAsk();
    const input = document.getElementById("workspace-chat-input");
    const askInput = document.getElementById("chat-message");
    if (input && askInput && input.value.trim()) {
      askInput.value = input.value;
      autoGrowChatInput();
    }
    if (mode) syncAskMode(mode);
    switchView("chat");
  },

  async sendMessage() {
    const input = document.getElementById("workspace-chat-input");
    if (!input) return;
    const message = input.value.trim();
    if (!message || this.isSending) return;

    const previousSurface = this.surface;
    if (this.surface === "editor") this.applySurface("split");

    this.syncControlsToAsk();
    const askInput = document.getElementById("chat-message");
    if (askInput) {
      askInput.value = message;
      autoGrowChatInput();
    }
    input.value = "";
    this.autoResizeInput(input);

    try {
      await sendChatMessage(new Event("submit"), { workspaceMirror: true });
      if (previousSurface === "ask") this.applySurface("ask", { persist: true });
      else if (this.surface === "editor") this.applySurface("split", { persist: true });
    } catch (error) {
      this.onStreamError(error.message || String(error));
      throw error;
    }
  },

  buildMessageElement(role, content, isError = false, provider = null) {
    const messageEl = document.createElement("div");
    messageEl.className = `chat-message-compact ${role}`;
    const avatar = role === "user" ? "👤" : ((providerLabel(provider) || "AI").trim().charAt(0) || "A").toUpperCase();
    const time = new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
    messageEl.innerHTML = `
      <div class="chat-avatar-compact">${this.escapeHtml(avatar)}</div>
      <div class="chat-message-content-compact">
        <div class="chat-bubble-compact ${isError ? "error" : ""}">${this.formatMessage(content)}</div>
        <div class="chat-message-time">${time}</div>
      </div>`;
    return messageEl;
  },

  addMessage(role, content, isError = false, provider = null, store = true) {
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    chatThread.querySelector(".chat-empty-state")?.remove();
    chatThread.appendChild(this.buildMessageElement(role, content, isError, provider));
    if (isError && role === "assistant") this.addProviderErrorActions(content);
    chatThread.scrollTop = chatThread.scrollHeight;
  },

  addProviderErrorActions(text) {
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    const cta = providerErrorAction(text);
    const wrap = document.createElement("div");
    wrap.className = "provider-error-actions provider-error-actions-compact";
    if (usedExplicitProvider()) {
      const retryButton = document.createElement("button");
      retryButton.type = "button";
      retryButton.className = "btn btn-primary btn-sm";
      retryButton.textContent = t("provider.retryAuto");
      retryButton.addEventListener("click", () => handleProviderErrorAction("retry-auto"));
      wrap.appendChild(retryButton);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-secondary btn-sm";
    button.textContent = cta.label;
    button.addEventListener("click", () => {
      if (cta.action === "cli") {
        const checkbox = document.getElementById("workspace-chat-approve-cli");
        if (checkbox) checkbox.checked = true;
        this.syncControlsToAsk();
        showSnackbar("CLI runs enabled. Send your message again.", "info");
        return;
      }
      if (cta.action === "retry-auto") {
        handleProviderErrorAction("retry-auto");
        return;
      }
      switchView("providers");
    });
    wrap.appendChild(button);
    chatThread.appendChild(wrap);
  },

  formatMessage(content) {
    return this.escapeHtml(content || "").replace(/\n/g, "<br>");
  },

  escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  },

  setThinking(thinking) {
    this.isThinking = thinking;
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    chatThread.querySelector(".chat-thinking")?.closest(".chat-message-compact")?.remove();
    if (!thinking) {
      this.setStatus("Ready");
      return;
    }
    const thinkingEl = document.createElement("div");
    thinkingEl.className = "chat-message-compact assistant";
    thinkingEl.innerHTML = `
      <div class="chat-avatar-compact">AI</div>
      <div class="chat-message-content-compact">
        <div class="chat-thinking">
          <div class="chat-thinking-dot"></div>
          <div class="chat-thinking-dot"></div>
          <div class="chat-thinking-dot"></div>
        </div>
      </div>`;
    chatThread.appendChild(thinkingEl);
    chatThread.scrollTop = chatThread.scrollHeight;
  },

  openFilePicker() {
    document.getElementById("workspace-chat-file-input")?.click();
  },

  async handleFileSelect(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) return;
    try {
      await handleFileSelect(files);
      showSnackbar(`${files.length} file(s) attached in Ask composer.`, "info");
      this.applySurface(this.surface === "editor" ? "split" : this.surface);
      this.openInAsk();
    } catch (error) {
      showError(error);
    }
  },

  clearChat() {
    if (!confirm("Start a new Ask dialog? Current thread stays in Dialogs history.")) return;
    state.chatId = "";
    this.showEmptyState();
    const askThread = document.querySelector("#chat-thread");
    if (askThread) {
      askThread.innerHTML = "";
      updateChatEmpty();
    }
    showSnackbar("Started a new Ask thread.", "info");
  },
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => workspaceChat.init());
} else {
  workspaceChat.init();
}

window.workspaceChat = workspaceChat;

