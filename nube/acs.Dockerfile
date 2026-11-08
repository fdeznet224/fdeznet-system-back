# ACS (GenieACS) de un ISP: cwmp (donde se reportan los módems) y nbi (lo que
# usa la API) en un solo contenedor. Misma versión que el ACS de producción.
FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g genieacs@1.2.16 && npm cache clean --force \
    && useradd --system --no-create-home genieacs \
    && mkdir -p /opt/genieacs/ext

COPY nube/acs/entrypoint-acs.sh /usr/local/bin/entrypoint-acs.sh
COPY nube/acs/preparar-acs.js /opt/genieacs/preparar-acs.js
RUN chmod 0755 /usr/local/bin/entrypoint-acs.sh

ENV GENIEACS_EXT_DIR=/opt/genieacs/ext \
    GENIEACS_CWMP_WORKER_PROCESSES=1 \
    GENIEACS_NBI_WORKER_PROCESSES=1

EXPOSE 7547 7557
ENTRYPOINT ["/usr/local/bin/entrypoint-acs.sh"]
