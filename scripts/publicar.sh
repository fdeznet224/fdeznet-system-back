#!/bin/bash
# Publica la versión indicada en el VPS (respaldo, frontend, migración, backend, salud).
# Uso en el VPS: nohup bash /root/publicar.sh 2.101.0 > /root/publicar.log 2>&1 &
VERSION="$1"
G="runuser -u fdeznet -- git"
set -x

/usr/local/sbin/fdeznet-maintenance backup || exit 1

cd /opt/fdeznet/frontend || exit 1
$G fetch -q origin && $G merge --ff-only origin/main || exit 1
npm run build > /root/publicar-build.log 2>&1 || { tail -20 /root/publicar-build.log; exit 1; }

cd /opt/fdeznet/backend || exit 1
cp .env ".env.backup-$VERSION"
$G fetch -q origin || exit 1
systemctl stop fdeznet-api fdeznet-bot
$G merge --ff-only origin/main && ./venv/bin/alembic upgrade head
RESULTADO=$?

BACK=$($G rev-parse --short HEAD)
FRONT=$(runuser -u fdeznet -- git -C /opt/fdeznet/frontend rev-parse --short HEAD)
if [ $RESULTADO -eq 0 ]; then
    sed -i "s/^FDEZNET_RELEASE_VERSION=.*/FDEZNET_RELEASE_VERSION=$VERSION/; s/^FDEZNET_BACKEND_COMMIT=.*/FDEZNET_BACKEND_COMMIT=$BACK/; s/^FDEZNET_FRONTEND_COMMIT=.*/FDEZNET_FRONTEND_COMMIT=$FRONT/" .env
fi
systemctl start fdeznet-api fdeznet-bot

for i in $(seq 30); do
    curl -fs http://127.0.0.1:8000/api/health/ready && { echo; echo "PUBLICADA $VERSION backend $BACK frontend $FRONT"; exit 0; }
    sleep 3
done
echo "LA API NO RESPONDE"
systemctl status fdeznet-api --no-pager | tail -20
exit 1
