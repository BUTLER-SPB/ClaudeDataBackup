# ClaudeDataBackup — форк BUTLER-SPB (Windows)

Форк [morningcraft/ClaudeDataBackup](https://github.com/morningcraft/ClaudeDataBackup): локальный бэкап чатов claude.ai, кеша Claude Desktop и сессий Claude Code.

## Что изменено в форке

| Что | Зачем |
|---|---|
| Инкрементальный режим перекачивает изменённые чаты | В upstream обновлённый чат брался из старой копии `_raw/mode_a/*.json`, и продолжаемые диалоги в бэкапе не обновлялись |
| Обход **всех** организаций с чатами | Upstream брал только `orgs[0]`, поэтому при Team + личном аккаунте часть чатов терялась |
| Пагинация `chat_conversations` (limit/offset + дедуп + fallback) | На больших аккаунтах полный список может не прийти одним запросом |
| Бэкап знаний проектов: `project-knowledge/<org>/<project>/INSTRUCTIONS.md`, `docs/*`, `project.json` | Upstream сохранял только метаданные проектов |
| `CLAUDE_SESSION_KEY` | Ручной sessionKey, если расшифровка cookie Desktop не работает или нужен другой аккаунт |
| Корректная расшифровка cookie: SHA256(host_key)-префикс по версии БД ≥24 с проверкой хэша, явная ошибка для v20 | Upstream резал 32 байта вслепую |
| `scripts/setup-win.ps1` — установка через uv и ежедневная задача в Планировщике | Обходится без прав администратора и без висящего демона |

## Установка (Windows)

```powershell
cd $env:USERPROFILE\src
git clone https://github.com/BUTLER-SPB/ClaudeDataBackup.git
cd ClaudeDataBackup
powershell -ExecutionPolicy Bypass -File scripts\setup-win.ps1 -BackupDir "$env:USERPROFILE\ClaudeBackup" -ScheduleDaily -At 03:30
```

## Использование

```powershell
$cli = "$env:USERPROFILE\src\ClaudeDataBackup\.venv\Scripts\claude-data-backup.exe"
& $cli list                      # что найдено (без сети)
& $cli --incremental             # бэкап (первый раз — полный)
& $cli --incremental --mode a    # только claude.ai
& $cli status --json             # состояние
Start-Process "$env:USERPROFILE\ClaudeBackup\index.html"   # просмотрщик
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
├── manifest.json                 # состояние инкрементального бэкапа
├── desktop-conversations/        # чаты claude.ai (md + json), по проектам
├── project-knowledge/<org>/<project>/  # инструкции и документы проектов
├── claude-code/                  # сессии Claude Code
└── _raw/mode_a/                  # сырые JSON из API
```

Логи: `%USERPROFILE%\.claude-data-backup\logs\` (`app.log`, `scheduled-last.json`).
