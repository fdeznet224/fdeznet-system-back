# Bot de WhatsApp de una instancia de la nube (whatsapp-web.js + Chrome de puppeteer).
FROM node:22-bookworm-slim

# Librerías que necesita el Chrome que descarga puppeteer (unzip lo desempaca).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates tzdata unzip fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
        libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
        libx11-xcb1 libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/bot_whatsapp
RUN chown node:node /app/bot_whatsapp
USER node

ENV PUPPETEER_CACHE_DIR=/app/bot_whatsapp/.cache/puppeteer \
    HOST=0.0.0.0 \
    PORT=3000

COPY --chown=node:node bot_whatsapp/package.json bot_whatsapp/package-lock.json bot_whatsapp/parche-whatsapp-web.js ./
# puppeteer 25 ya no baja Chrome al instalarse: se instala la versión que fija.
RUN npm ci --omit=dev \
    && npx puppeteer browsers install chrome
COPY --chown=node:node bot_whatsapp/ ./
RUN mkdir -p uploads .wwebjs_auth

EXPOSE 3000
CMD ["node", "index.js"]
