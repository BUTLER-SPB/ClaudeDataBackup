# ClaudeDataBackup — форк BUTLER-SPB (Windows)

Форк [morningcraft/ClaudeDataBackup](https://github.com/morningcraft/ClaudeDataBackup): локальный бэкап чатов claude.ai, кеша Claude Desktop и сессий Claude Code.

## Установка для сотрудника (одна строка)

Открыть PowerShell (Пуск → «PowerShell») и вставить:

```powershell
irm https://raw.githubusercontent.com/BUTLER-SPB/ClaudeDataBackup/main/scripts/install.ps1 | iex
```

Права администратора не нужны. Скрипт ставит uv и Python 3.12 в профиль пользователя, приложение — в `%LOCALAPPDATA%\ClaudeDataBackup`, создаёт ярлык **Claude Backup** в меню Пуск и на рабочем столе и открывает окно. Повторный запуск той же строки обновляет приложение; бэкапы и настройки сохраняются.

Удаление: `& ([scriptblock]::Create((irm https://raw.githubusercontent.com/BUTLER-SPB/ClaudeDataBackup/main/scripts/install.ps1))) -Uninstall` (бэкапы остаются).

### Окно «Бэкап Claude»

| Блок | Что делает |
|---|---|
| 1. Подготовка | Сама находит профиль Claude Desktop (любой вариант установки), ключ claude.ai, кэш, сессии Claude Code; проверяет вход онлайн |
| 2. Папка для бэкапов | Показать / изменить / открыть папку |
| 3. Расписание | Ежедневный бэкап в заданное время (Планировщик заданий Windows, без админа; пропущенный запуск — при включении ПК) |
| 4. Бэкапы | «Сделать бэкап сейчас», история всех прогонов (вручную / по расписанию, результат, сколько нового), «Открыть просмотр чатов» |

При **первом запуске** всё делается само: подготовка → ежедневное расписание на 03:30 → первый бэкап. Сотруднику нужно только один раз согласиться закрыть Claude Desktop на несколько секунд (см. ниже).

### Почему просит закрыть Claude Desktop

Запущенный Claude Desktop держит файл с ключом входа (Cookies) с монопольной блокировкой — прочитать его можно только пока Claude закрыт. Кнопка «Закрыть Claude и прочитать ключ» закрывает Claude Desktop (только его — Claude Code не трогается), читает ключ и сразу открывает Claude снова.

Прочитанный ключ хранится в `%USERPROFILE%\.claude-data-backup\session.dpapi`, зашифрованным Windows DPAPI: расшифровать его может только этот пользователь на этом ПК (так же Windows защищает сами куки Claude). Ночной бэкап пользуется этим ключом, даже если Claude открыт; когда Claude закрыт, ключ каждый раз обновляется из куки. Если ключ устарел (выход из аккаунта), «Подготовка» покажет ошибку в строке «Аккаунт» и снова предложит прочитать ключ.

### Где Claude Desktop хранит данные (автопоиск)

| Вариант установки | Профиль |
|---|---|
| Классический установщик | `%APPDATA%\Claude` |
| Microsoft Store / MSIX, старые сборки | `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude` |
| MSIX, новые сборки | `%LOCALAPPDATA%\Claude-Data\claude_<id>` (передаётся через `--user-data-dir`) |
| Прочие сборки | `%LOCALAPPDATA%\Claude*`, `%APPDATA%\Claude*` |

Порядок выбора: профиль запущенного Claude Desktop → сохранённый «Подготовкой» (`desktop_profile_dir` в `config.json`) → профиль с самым свежим файлом Cookies. Сессии Claude Code: `CLAUDE_CONFIG_DIR\projects`, иначе `%USERPROFILE%\.claude\projects`.

## Что изменено в форке

| Что | Зачем |
|---|---|
| Окно «Бэкап Claude» (`app_ru.py`) + установщик одной строкой (`scripts/install.ps1`) | Сотрудник ставит и пользуется без консоли и настроек |
| Автопоиск профиля Claude Desktop, включая новый MSIX-путь `Claude-Data` | Upstream искал только `%APPDATA%\Claude` и `Packages\…\LocalCache` — на новых сборках Desktop Mode A/B не работали |
| Сохранённый ключ (DPAPI) + перезапуск Claude для чтения куки | Запущенный Desktop блокирует Cookies (WinError 32) |
| История прогонов `history.jsonl` в папке бэкапа | Видно, что бэкапы реально делаются (и ночные тоже) |
| Задача Планировщика запускает `pythonw -m claude_data_backup.scheduled_run` | Без мигающей консоли, лог — `logs\scheduled-last.log` |
| Mode C: неизменённые сессии больше не переписываются каждый раз; `00_index.md` копит строки между прогонами | Upstream искал файл по префиксу id, а файлы называются `дата__заголовок__id.md` |
| Инкрементальный режим перекачивает изменённые чаты | В upstream обновлённый чат брался из старой копии `_raw/mode_a/*.json`, и продолжаемые диалоги в бэкапе не обновлялись |
| Обход **всех** организаций с чатами | Upstream брал только `orgs[0]`, поэтому при Team + личном аккаунте часть чатов терялась |
| Пагинация `chat_conversations` (limit/offset + дедуп + fallback) | На больших аккаунтах полный список может не прийти одним запросом |
| Бэкап знаний проектов: `project-knowledge/<org>/<project>/INSTRUCTIONS.md`, `docs/*`, `project.json` | Upstream сохранял только метаданные проектов |
| `CLAUDE_SESSION_KEY` | Ручной sessionKey, если расшифровка cookie Desktop не работает или нужен другой аккаунт |
| Корректная расшифровка cookie: SHA256(host_key)-префикс по версии БД ≥24 с проверкой хэша, явная ошибка для v20 | Upstream резал 32 байта вслепую |

## Установка для разработки

```powershell
cd $env:USERPROFILE\src
git clone https://github.com/BUTLER-SPB/ClaudeDataBackup.git
cd ClaudeDataBackup
pwsh -File scripts\setup-win.ps1 -BackupDir "$env:USERPROFILE\ClaudeBackup" -ScheduleDaily -At 03:30
```

Проверить установщик на локальной копии без пуша: `powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -SourceDir . -NoLaunch`. Другая ветка с GitHub: `$env:CDB_REF = "имя-ветки"` перед `irm … | iex`.

## Использование из консоли

```powershell
$cli = "$env:LOCALAPPDATA\ClaudeDataBackup\venv\Scripts\claude-data-backup.exe"
& $cli list                      # что найдено (без сети)
& $cli --incremental             # бэкап (первый раз — полный)
& $cli --incremental --mode a    # только claude.ai
& $cli status --json             # состояние
& "$env:LOCALAPPDATA\ClaudeDataBackup\venv\Scripts\python.exe" -m claude_data_backup.prepare   # «Подготовка» в JSON
```

Если cookie не расшифровывается: возьми `sessionKey` в браузере (DevTools → Application → Cookies → claude.ai) и запусти так:

```powershell
$env:CLAUDE_SESSION_KEY = "sk-ant-sid..."
& $cli --incremental --mode a
Remove-Item Env:CLAUDE_SESSION_KEY
```

sessionKey даёт полный доступ к аккаунту: не сохраняй его в файлах и истории.

## Структура бэкапа

```
ClaudeBackup/
├── index.html                    # офлайн-просмотрщик с поиском
├── history.jsonl                 # история прогонов (для окна приложения)
├── manifest.json                 # состояние инкрементального бэкапа
├── desktop-conversations/        # чаты claude.ai (md + json), по проектам
├── project-knowledge/<org>/<project>/  # инструкции и документы проектов
├── claude-code/                  # сессии Claude Code
└── _raw/mode_a/                  # сырые JSON из API
```

Логи и настройки: `%USERPROFILE%\.claude-data-backup\` (`config.json`, `session.dpapi`, `logs\app.log`, `logs\scheduled-last.log`).
