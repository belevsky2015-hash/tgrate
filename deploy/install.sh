#!/usr/bin/env bash
# Установка в /opt/tgrate под systemd. Запускать из корня проекта.
set -euo pipefail

DEST=/opt/tgrate
USER_NAME="${SUDO_USER:-$USER}"

if [[ $EUID -ne 0 ]]; then
  echo "Нужен root: sudo deploy/install.sh" >&2
  exit 1
fi

echo "Устанавливаю в $DEST от имени $USER_NAME"
mkdir -p "$DEST"
rsync -a --exclude .venv --exclude .git --exclude data ./ "$DEST/"
mkdir -p "$DEST/data"
chown -R "$USER_NAME:$USER_NAME" "$DEST"

# Венв через uv: на этом хосте нет python3-venv (ensurepip недоступен),
# а uv тащит свой pip внутри себя. Ставим install, а не sync: requirements.txt
# написан руками, не сгенерирован uv pip compile, и sync не резолвит
# зависимости — уехали бы starlette и экстры uvicorn[standard].
UV="$(command -v uv || true)"
[[ -z "$UV" ]] && UV="/home/$USER_NAME/.local/bin/uv"
if [[ ! -x "$UV" ]]; then
  echo "uv не найден ($UV). Поставьте uv или apt install python3.12-venv" >&2
  exit 1
fi

rm -rf "$DEST/.venv"
sudo -u "$USER_NAME" -H "$UV" venv -p /usr/bin/python3 "$DEST/.venv"
sudo -u "$USER_NAME" -H "$UV" pip install -q --python "$DEST/.venv/bin/python" \
  -r "$DEST/requirements.txt"

if [[ ! -f "$DEST/.env" ]]; then
  cp "$DEST/.env.example" "$DEST/.env"
  chown "$USER_NAME:$USER_NAME" "$DEST/.env"
  echo
  echo "Создан $DEST/.env — впишите WALLET_API_KEY, TG_SESSION и PANEL_TOKEN,"
  echo "затем запустите install.sh ещё раз."
  exit 0
fi
chmod 600 "$DEST/.env"

sed "s/%i/$USER_NAME/" deploy/tgrate-api.service > /etc/systemd/system/tgrate-api.service
sed "s/%i/$USER_NAME/" deploy/tgrate-bot.service > /etc/systemd/system/tgrate-bot.service

systemctl daemon-reload
systemctl enable --now tgrate-api

if grep -q '^TG_SESSION=.\+' "$DEST/.env"; then
  systemctl enable --now tgrate-bot
  echo "Панель и юзербот запущены."
else
  echo "TG_SESSION пуст — запустил только панель."
fi

echo
echo "Проверка:  cd $DEST && .venv/bin/python scripts/check.py"
echo "Логи:      journalctl -u tgrate-api -u tgrate-bot -f"
echo "Панель:    http://127.0.0.1:8080 (наружу — через deploy/nginx.conf)"
