# ArchitectOS Memory — IntelliJ plugin

Тонкая «панель памяти» ArchitectOS внутри IntelliJ IDEA (и других IDE на платформе
IntelliJ). Не конкурирует с Copilot/Junie: только работа с памятью — поиск,
просмотр, добавление, оценка релевантности.

## Возможности

- **Tool Window «ArchitectOS Memory» → вкладка Search**: поиск по памяти, список
  результатов, полный текст узла, кнопки оценки «Useful / Not useful» (улучшают
  ранжирование), счётчик review queue.
- **Вкладка Sources**: статус источников инжеста с сервера (локальные + MCP),
  путь к inbox-папке, последние логи инжеста, кнопка «Rescan selected».
  Сами подключения источников (Azure, Granola/Krisp, корни FS) настраиваются в
  десктопном приложении — плагин их только показывает и запускает.
- **Editor action «Add Selection to ArchitectOS Memory»** (правая кнопка в
  редакторе): сохранить выделенный фрагмент как память (source=`intellij`),
  type/scope подставляются из настроек.
- **Editor action «Search ArchitectOS Memory for Selection»**: открывает панель
  с готовым запросом из выделенного текста.
- **Settings → Tools → ArchitectOS Memory**: URL сервера, токен, путь к корню
  ArchitectOS; кнопка «Detect» читает `data/architectos.runtime.json` и
  подставляет `url`/`auth_token` автоматически; дефолты «куда писать»
  (project id, scope, type) для ручного добавления памяти.

## Inbox-папка (drop folder)

На сервере есть источник `inbox`: всё, что пользователь кладёт в папку
(по умолчанию `<root>/data/inbox`, настраивается ключом настроек
`memory_ingest.inbox_dir` через `POST /api/settings`), подхватывается при
ingest/rescan и попадает в движок памяти как кандидаты. Текстовые файлы
(md/txt/log/код/конфиги — см. `TEXT_LIKE_EXTENSIONS`), бинарные пропускаются.
Во вкладке Sources плагина виден актуальный путь к inbox.

Совместимость: IntelliJ IDEA / PyCharm **2024.3–2026.2** (build `243` and newer). Version `0.2.0` was capped at `243.*` and will not load on 2025/2026 IDEs — install **0.2.1+**.

## Сборка

Требуется JDK 17 (все команды из каталога `ide/intellij`):

```bash
./gradlew buildPlugin   # zip в build/distributions/
./gradlew runIde        # sandbox IDE с установленным плагином
```

Установка: Settings → Plugins → ⚙ → Install Plugin from Disk → выбрать zip.

## Как плагин общается с сервером

HTTP API локального сервера ArchitectOS (`run_architectos.py`), заголовок
`X-ArchitectOS-Token`. Плагин не требует CORS: нативный HTTP-клиент не шлёт
`Origin`. Токен и URL удобно взять из `data/architectos.runtime.json`
(перезаписывается при каждом запуске сервера — кнопка «Detect»).
