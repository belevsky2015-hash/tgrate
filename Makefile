.PHONY: install check test run bot deploy logs fmt

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

install:            ## поставить зависимости
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements.txt
	@test -f .env || (cp .env.example .env && echo "Создан .env — впишите WALLET_API_KEY")
	@echo "Готово. Дальше: make check"

check:              ## проверить ключ, стакан и семантику side
	$(PY) scripts/check.py

test:               ## тесты движка, сеть не нужна
	$(PY) -m pytest tests/ -q

run:                ## панель на 127.0.0.1:8080
	$(PY) -m uvicorn app.main:app --host $${HOST:-127.0.0.1} --port $${PORT:-8080} --reload

bot:                ## юзербот на готовой сессии
	$(PY) -m app.userbot

session:            ## получить StringSession из существующей сессии
	$(PY) scripts/session.py

deploy:             ## поставить systemd-юниты
	sudo deploy/install.sh

logs:
	journalctl -u tgrate-api -u tgrate-bot -f -n 50
