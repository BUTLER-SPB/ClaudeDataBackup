#!/bin/bash
# ClaudeDataBackup (BUTLER-SPB fork) — установка на macOS одной строкой, без sudo.
#
#   curl -fsSL https://raw.githubusercontent.com/BUTLER-SPB/ClaudeDataBackup/main/scripts/install.sh | bash
#
# Что делает:
#   1. ставит uv (менеджер Python) в профиль пользователя, если его нет;
#   2. скачивает приложение с GitHub в ~/Library/Application Support/ClaudeDataBackup;
#   3. создаёт отдельное окружение Python 3.12 и ставит приложение;
#   4. создаёт «Claude Backup.app» в ~/Applications и открывает его.
# Повторный запуск той же строки обновляет приложение; бэкапы и настройки сохраняются.
#
# Параметры (через `bash -s --` при запуске из curl):
#   --ref <ветка>       другая ветка (по умолчанию main; также CDB_REF)
#   --source-dir <dir>  поставить из локальной копии (для проверки без GitHub)
#   --no-launch         не открывать приложение после установки
#   --uninstall         удалить приложение и ежедневную задачу (бэкапы остаются)
set -euo pipefail

# Всё тело — в функции, вызываемой в последней строке: при `curl | bash` bash
# сначала прочитает скрипт целиком, а stdin команд отрезан (</dev/null), так что
# ни одна дочерняя программа не «съест» остаток скрипта из канала.
main() {
REF="${CDB_REF:-main}"
REPO="${CDB_REPO:-BUTLER-SPB/ClaudeDataBackup}"
SOURCE_DIR=""
NO_LAUNCH=0
UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --source-dir) SOURCE_DIR="$2"; shift 2 ;;
    --no-launch) NO_LAUNCH=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "Неизвестный параметр: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\033[36m[ClaudeBackup]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[ClaudeBackup] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "Этот установщик — для macOS. Для Windows: scripts/install.ps1"

BASE="$HOME/Library/Application Support/ClaudeDataBackup"
APP_DIR="$BASE/app"
VENV="$BASE/venv"
PY="$VENV/bin/python3"
BUNDLE="$HOME/Applications/Claude Backup.app"
LABEL="com.butler.claudebackup"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$UNINSTALL" = 1 ]; then
  launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
  rm -f "$AGENT"
  rm -rf "$BUNDLE" "$BASE"
  step "Удалено. Бэкапы и настройки (~/.claude-data-backup) сохранены."
  exit 0
fi

# 1. uv
UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
if [ -z "$UV" ]; then
  step "Ставлю uv (в профиль пользователя, astral.sh)…"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  UV="$HOME/.local/bin/uv"
  [ -x "$UV" ] || die "uv не установился. Попробуйте: brew install uv"
fi

# 2. Исходники
mkdir -p "$BASE"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [ -n "$SOURCE_DIR" ]; then
  step "Копирую исходники из $SOURCE_DIR…"
  mkdir -p "$TMP/src-copy"
  cp -R "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/README.md" "$SOURCE_DIR/src" "$TMP/src-copy/"
else
  step "Скачиваю $REPO ($REF)…"
  mkdir -p "$TMP/src-copy"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" \
    | tar -xz -C "$TMP/src-copy" --strip-components=1
fi
[ -f "$TMP/src-copy/pyproject.toml" ] || die "Архив без pyproject.toml"
rm -rf "$APP_DIR"
mv "$TMP/src-copy" "$APP_DIR"

# 3. Окружение Python (стандартный venv, как в Windows-установщике)
if [ ! -x "$PY" ] || [ ! -f "$VENV/.stdlib-venv" ]; then
  step "Создаю окружение Python 3.12…"
  rm -rf "$VENV"
  "$UV" python install 3.12
  BASE_PY="$("$UV" python find 3.12)"
  [ -x "$BASE_PY" ] || die "Python 3.12 не найден после установки"
  "$BASE_PY" -m venv --without-pip "$VENV"
  echo "created by install.sh" > "$VENV/.stdlib-venv"
fi
step "Ставлю приложение…"
"$UV" pip install --python "$PY" --reinstall-package claude-data-backup "$APP_DIR"

# 4. Claude Backup.app (лаунчер запускает окно из окружения)
step "Создаю «Claude Backup» в ~/Applications…"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
ICNS="$("$PY" -c 'import claude_data_backup, pathlib; print(pathlib.Path(claude_data_backup.__file__).with_name("app-icon.icns"))')"
[ -f "$ICNS" ] && cp "$ICNS" "$BUNDLE/Contents/Resources/AppIcon.icns"
VERSION="$("$PY" -c 'import claude_data_backup as c; print(c.__version__)')"
cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Claude Backup</string>
  <key>CFBundleDisplayName</key><string>Бэкап Claude</string>
  <key>CFBundleIdentifier</key><string>$LABEL.app</string>
  <key>CFBundleExecutable</key><string>claude-backup</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
cat > "$BUNDLE/Contents/MacOS/claude-backup" <<'LAUNCHER'
#!/bin/bash
# Окно «Бэкап Claude» из окружения в ~/Library/Application Support/ClaudeDataBackup
export PYTHONUTF8=1
exec "$HOME/Library/Application Support/ClaudeDataBackup/venv/bin/python3" -m claude_data_backup.app_web
LAUNCHER
chmod +x "$BUNDLE/Contents/MacOS/claude-backup"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$BUNDLE" >/dev/null 2>&1 || true

# Ежедневная задача от прежней установки — перенаправить на это окружение
if [ -f "$AGENT" ]; then
  if "$PY" - <<'PYCODE'
import sys
from claude_data_backup import mac_task
info = mac_task.query()
if info.get("exists") and not mac_task.is_ours(info):
    ok, err = mac_task.install(info.get("at") or mac_task.DEFAULT_TIME)
    if not ok:
        print(err, file=sys.stderr)
        sys.exit(1)
PYCODE
  then
    step "Ежедневная задача проверена."
  else
    step "Ежедневную задачу обновить не удалось — включите расписание в окне приложения заново."
  fi
fi

step "Готово. «Claude Backup» — в папке «Программы» вашей учётной записи (~/Applications) и в Launchpad."
[ "$NO_LAUNCH" = 1 ] || open "$BUNDLE"
}

main "$@" </dev/null
