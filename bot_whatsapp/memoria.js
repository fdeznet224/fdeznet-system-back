'use strict';

// Ajustes para que cada bot gaste menos RAM cuando corren varios ISP en la
// misma VPS de la nube.

const ARGUMENTOS_BASE = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-extensions',
    '--disable-gpu',
    '--disable-dev-shm-usage',      // CRÍTICO: Evita caídas de memoria compartida en Linux
    '--no-first-run',
    '--disable-accelerated-2d-canvas',
];

// Servicios de Chrome que WhatsApp Web no usa. Sin aislamiento por sitio los
// iframes (visor de PDF, vistas previas) no abren un proceso cada uno.
const ARGUMENTOS_LIGEROS = [
    '--disable-background-networking',
    '--disable-default-apps',
    '--disable-sync',
    '--disable-component-update',
    '--mute-audio',
    '--disable-site-isolation-trials',
    '--disable-features=Translate,MediaRouter,OptimizationHints,IsolateOrigins,site-per-process',
];

function argumentosChrome(env = process.env) {
    const extra = (env.BOT_CHROME_ARGS_EXTRA || '')
        .split(',')
        .map((arg) => arg.trim())
        .filter(Boolean);
    const ligero = env.BOT_CHROME_LIGERO !== '0';
    return [...ARGUMENTOS_BASE, ...(ligero ? ARGUMENTOS_LIGEROS : []), ...extra];
}

// Milisegundos hasta la próxima hora HH:MM (hora local del contenedor).
function msHastaHora(hora, ahora = new Date()) {
    const coincide = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(hora || '');
    if (!coincide) return null;
    const siguiente = new Date(ahora);
    siguiente.setHours(Number(coincide[1]), Number(coincide[2]), 0, 0);
    if (siguiente <= ahora) siguiente.setDate(siguiente.getDate() + 1);
    return siguiente - ahora;
}

module.exports = { argumentosChrome, msHastaHora };
