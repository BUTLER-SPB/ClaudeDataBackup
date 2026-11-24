# ClaudeDataBackup — форк BUTLER-SPB (Windows и macOS)

Форк [morningcraft/ClaudeDataBackup](https://github.com/morningcraft/ClaudeDataBackup): локальный бэкап чатов claude.ai, кеша Claude Desktop и сессий Claude Code.

## Установка на macOS (одна строка)

Открыть «Терминал» (Программы → Утилиты) и вставить:

```bash
curl -fsSL https://raw.githubusercontent.com/BUTLER-SPB/ClaudeDataBackup/main/scripts/install.sh | bash
```

Без sudo. Скрипт ставит uv и Python 3.12 в профиль пользователя, приложение — в `~/Library/Application Support/ClaudeDataBackup`, создаёт **Claude Backup** в `~/Applications` (видно в Launchpad и Spotlight) и открывает окно. Повторный запуск обновляет приложение. Удаление: `curl -fsSL …/install.sh | bash -s -- --uninstall` (бэкапы остаются).

Отличия от Windows:

| | macOS |
|---|---|
| Ключ claude.ai | Claude закрывать не нужно: куки читаются копией, а ключ шифрования macOS выдаёт из Связки ключей («Claude Safe Storage»). При первом подключении macOS спросит пароль — нажмите **«Разрешить всегда»**, иначе запрос будет появляться при каждом бэкапе, в том числе ночном. Ключ приложением не сохраняется |
| Расписание | LaunchAgent `~/Library/LaunchAgents/com.butler.claudebackup.plist` (launchd, без sudo). Если Mac спал — запуск после пробуждения; если был выключен — в следующий раз по расписанию |
| Профиль Desktop | `~/Library/Application Support/Claude` (и другие `Claude*` там же), запущенный процесс находится через `ps` |
| Папка по умолчанию | `~/Documents/ClaudeDataBackup`. Если «Документы» синхронизируются с iCloud, лучше выбрать другую папку: просмотрщик `index.html` весит сотни МБ |

## Установка на Windows (одна строка)

Открыть PowerShell (Пуск → «PowerShell») и вставить:

```powershell
irm https://raw.githubusercontent.com/BUTLER-SPB/ClaudeDataBackup/main/scripts/install.ps1 | iex
```

Права администратора не нужны. Скрипт ставит uv и Python 3.12 в профиль пользователя, приложение — в `%LOCALAPPDATA%\ClaudeDataBackup` (окружение создаётся стандартным `python -m venv`, чтобы не было окна консоли), создаёт ярлык **Claude Backup** в меню Пуск и на рабочем столе и открывает окно. Повторный запуск той же строки обновляет приложение; бэкапы и настройки сохраняются.

Удаление: `& ([scriptblock]::Create((irm https://raw.githubusercontent.com/BUTLER-SPB/ClaudeDataBackup/main/scripts/install.ps1))) -Uninstall` (бэкапы остаются).

### Окно «Бэкап Claude»

Окно на HTML (pywebview: Edge WebView2 в Windows, WebKit в macOS), светлая и тёмная тема по системе. Если WebView недоступен, открывается запасное окно на customtkinter (`app_ru.py`).

| Блок | Что делает |
|---|---|
| Статус | «Всё сохранено» / «Сохранено частично» / «Давно не было бэкапа», кнопки «Сделать бэкап» и «Открыть чаты», ход бэкапа с подробностями |
| Цифры | Чатов claude.ai, сессий Claude Code, время следующего бэкапа |
| Что сохраняется | Claude Desktop, чаты claude.ai (аккаунт и организации), Claude Code; проверка идёт сама при открытии (если прошлой больше 6 ч) |
| Настройки | Папка для бэкапов; переключатель ежедневного бэкапа и время (Планировщик Windows, без админа; пропущенный запуск — при включении ПК) |
| История | Последние прогоны: когда, как запущен, результат, сколько нового |

При **первом запуске** всё делается само: проверка → (подключение claude.ai) → ежедневное расписание на 03:30 → первый бэкап. Сотруднику нужно только один раз согласиться закрыть Claude Desktop на несколько секунд (см. ниже).

### Почему просит закрыть Claude Desktop

Запущенный Claude Desktop держит файл с ключом входа (Cookies) с монопольной блокировкой — прочитать его можно только пока Claude закрыт. Кнопка «Подключить» закрывает Claude Desktop (только его процессы; Claude Code в терминале не трогается), читает ключ и сразу открывает Claude снова. Это делает отдельный процесс со сторожевым таймером (75 с), а окно ждёт его не больше 90 с и при необходимости само запускает Claude обратно; каждый шаг пишется в журнал с меткой `[read-key]`.

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
| Mode C: jsonl сессий в бэкап — копией, не hardlink; перед бэкапом сессии-ссылки и облачные плейсхолдеры в `~/.claude/projects` чинятся сами | Hardlink в облачную папку (Яндекс.Диск, OneDrive) делал плейсхолдером и оригинал — Claude Desktop переставал открывать сессию |
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
