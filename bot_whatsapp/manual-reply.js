'use strict';

/**
 * Distingue lo que escribe una persona desde el celular de lo que envía el
 * sistema (recordatorios, recibos, respuestas del bot). Solo lo primero debe
 * pausar el bot.
 *
 * WhatsApp Web emite `message_create` también para los envíos del sistema, a
 * veces antes de que `sendMessage` devuelva su identificador. Por eso el
 * registro cuenta los envíos en curso y la revisión espera a que
 * terminen antes de decidir.
 */

const TTL_MS = 10 * 60 * 1000;
const ESPERA_MINIMA_MS = 3 * 1000;
const ESPERA_MAXIMA_MS = 90 * 1000;
const INTERVALO_MS = 500;
const EDAD_MAXIMA_S = 120;
// En chats con LID WhatsApp a veces no devuelve el id del envío; entonces se
// reconoce por texto durante este tiempo (una sola vez por envío).
const TTL_TEXTO_MS = 2 * 60 * 1000;

function normalizarTexto(texto) {
    return String(texto || '').replace(/\s+/g, ' ').trim();
}

function idsDeMensaje(mensaje) {
    const id = mensaje?.id;
    return [id?.id, id?._serialized, id?.serialized]
        .filter(Boolean)
        .map(String);
}

function crearRegistroEnvios() {
    const ids = new Map();
    const enCurso = new Map();
    const textos = [];

    return {
        iniciarEnvio(chatId) {
            enCurso.set(chatId, (enCurso.get(chatId) || 0) + 1);
        },
        terminarEnvio(chatId, respuesta, texto) {
            for (const id of idsDeMensaje(respuesta)) ids.set(id, Date.now());
            const normalizado = normalizarTexto(texto);
            if (normalizado) textos.push({ texto: normalizado, fecha: Date.now() });
            const pendientes = (enCurso.get(chatId) || 1) - 1;
            if (pendientes > 0) enCurso.set(chatId, pendientes);
            else enCurso.delete(chatId);
        },
        marcar(respuesta) {
            for (const id of idsDeMensaje(respuesta)) ids.set(id, Date.now());
        },
        hayEnviosEnCurso(chatId) {
            // Sin chat: cualquier envío. WhatsApp puede nombrar el mismo chat
            // como número (@c.us) o como identificador interno (@lid).
            return chatId === undefined ? enCurso.size > 0 : enCurso.has(chatId);
        },
        esDelSistema(mensaje, ahora = Date.now()) {
            if (idsDeMensaje(mensaje).some((id) => ids.has(id))) return true;
            const normalizado = normalizarTexto(mensaje?.body);
            const indice = textos.findIndex(
                (t) => t.texto === normalizado && ahora - t.fecha <= TTL_TEXTO_MS,
            );
            if (!normalizado || indice === -1) return false;
            textos.splice(indice, 1);
            return true;
        },
        limpiar(ahora = Date.now()) {
            for (const [id, fecha] of ids) {
                if (ahora - fecha > TTL_MS) ids.delete(id);
            }
            for (let i = textos.length - 1; i >= 0; i -= 1) {
                if (ahora - textos[i].fecha > TTL_TEXTO_MS) textos.splice(i, 1);
            }
        },
    };
}

function esRespuestaPropia(mensaje, ahoraSegundos = Math.floor(Date.now() / 1000)) {
    if (!mensaje?.fromMe || mensaje.isStatus) return false;
    const destino = String(mensaje.to || '');
    if (!destino || destino.includes('@g.us') || destino === 'status@broadcast') {
        return false;
    }
    const edad = ahoraSegundos - Number(mensaje.timestamp || ahoraSegundos);
    return edad <= EDAD_MAXIMA_S;
}

async function esEscritaPorPersona(
    mensaje,
    registro,
    { esperar = (ms) => new Promise((r) => setTimeout(r, ms)) } = {},
) {
    let esperado = 0;
    // Deja terminar los envíos del sistema para conocer sus IDs.
    do {
        await esperar(INTERVALO_MS);
        esperado += INTERVALO_MS;
    } while (
        (esperado < ESPERA_MINIMA_MS || registro.hayEnviosEnCurso())
        && esperado < ESPERA_MAXIMA_MS
    );
    return !registro.esDelSistema(mensaje);
}

module.exports = {
    crearRegistroEnvios,
    esEscritaPorPersona,
    esRespuestaPropia,
};
