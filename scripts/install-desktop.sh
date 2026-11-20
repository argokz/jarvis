#!/usr/bin/env bash
# Окно Jarvis для этого пользователя Linux: сборка, ярлык в меню, подключение к ядру на этой машине,
# автозапуск при входе в систему. Повторный запуск обновляет установленное.
#   scripts/install-desktop.sh              собрать и установить
#   scripts/install-desktop.sh --no-build   установить уже собранное
#   scripts/install-desktop.sh --remove     убрать ярлык, автозапуск и программу (настройки ядра не трогает)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin="$HOME/.local/bin/jarvis-app"
icon="$HOME/.local/share/icons/hicolor/128x128/apps/jarvis.png"
entry="$HOME/.local/share/applications/jarvis.desktop"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/jarvis"
autostart="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/Jarvis.desktop"

if [[ "${1:-}" == "--remove" ]]; then
  pkill -x jarvis-app 2>/dev/null || true
  for f in "$bin" "$icon" "$entry" "$autostart" "$conf/env"; do
    [[ -e "$f" || -L "$f" ]] && unlink "$f" && echo "убрано: $f"
  done
  exit 0
fi

if [[ "${1:-}" != "--no-build" ]]; then
  (cd "$root/app" && npm run tauri build -- --no-bundle)
fi
built="$root/app/src-tauri/target/release/jarvis"
[[ -x "$built" ]] || { echo "нет собранной программы: $built" >&2; exit 1; }

pkill -x jarvis-app 2>/dev/null && sleep 1 || true   # запущенную программу нельзя заменить на месте
install -Dm755 "$built" "$bin"
install -Dm644 "$root/app/src-tauri/icons/128x128.png" "$icon"

# Окно берёт адрес ядра и токен отсюда; ссылка, а не копия — токен остаётся в одном месте.
mkdir -p "$conf"
if [[ -f "$root/.env" ]]; then
  ln -sfn "$root/.env" "$conf/env"
else
  echo "нет $root/.env — адрес и токен придётся ввести в настройках окна" >&2
fi

mkdir -p "$(dirname "$entry")"
cat > "$entry" <<EOF
[Desktop Entry]
Type=Application
Name=Jarvis
Comment=Личный ассистент
Exec=$bin
Icon=jarvis
Terminal=false
Categories=Utility;
StartupWMClass=jarvis
EOF
update-desktop-database "$(dirname "$entry")" 2>/dev/null || true

# Первый запуск: включает автозапуск и остаётся в трее. Дальше окно открывается ярлыком или из значка.
setsid -f "$bin" --autostart --hidden >/dev/null 2>&1
echo "Установлено: $bin. Значок Jarvis — в трее; автозапуск включён (выключается в окне: Функции → Этот компьютер)."
