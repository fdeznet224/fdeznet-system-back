#!/bin/bash
set -e

cd /opt/fdeznet/backend

ENV_BAK="/opt/fdeznet/backups/backend_env_$(date +%F_%H-%M).bak"
cp .env "$ENV_BAK"

systemctl stop fdeznet-api.service || true

git fetch origin
git reset --hard origin/main

cp "$ENV_BAK" .env

rm -rf venv
python3 -m venv venv

./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -r requirements.txt

./venv/bin/python -c "from src.main import app; print('Backend OK')"

systemctl restart fdeznet-api.service
systemctl restart fdeznet-bot.service || true

systemctl status fdeznet-api.service --no-pager
