'use strict';

/**
 * Liga cada envío del sistema con su id de WhatsApp.
 *
 * Con WhatsApp Web actual, `sendMessage` a veces no devuelve el id del
 * mensaje; sin él no se pueden ligar los ACK (entregado / leído) y todo se
 * queda en "enviado". `message_create` sí trae el id: se empareja por texto
 * (y chat si coincide) con el envío pendiente, en el orden que lleguen.
 */

const TTL_MS = 2 * 60 * 1000;

function normalizar(texto) {
    return String(texto || '').replace(/\s+/g, ' ').trim();
}

function crearVinculador(ahora = () => Date.now()) {
    const enviosSinId = [];   // { texto, chat, mensajeChatId, fecha }
    const creadosSinDueno = []; // { texto, chat, waId, fecha }

    function limpiar() {
        const limite = ahora() - TTL_MS;
        for (const lista of [enviosSinId, creadosSinDueno]) {
            for (let i = lista.length - 1; i >= 0; i -= 1) {
                if (lista[i].fecha < limite) lista.splice(i, 1);
            }
        }
    }

    function tomar(lista, texto, chat) {
        const candidatos = lista
            .map((item, indice) => ({ item, indice }))
            .filter(({ item }) => item.texto === texto);
        if (!candidatos.length) return null;
        const elegido = candidatos.find(({ item }) => item.chat && item.chat === chat) || candidatos[0];
        lista.splice(elegido.indice, 1);
        return elegido.item;
    }

    return {
        /** sendMessage terminó sin id. Devuelve el waId si ya se vio el mensaje. */
        envioSinId(texto, chat, mensajeChatId) {
            limpiar();
            const normalizado = normalizar(texto);
            if (!normalizado || !mensajeChatId) return null;
            const creado = tomar(creadosSinDueno, normalizado, chat);
            if (creado) return creado.waId;
            enviosSinId.push({ texto: normalizado, chat, mensajeChatId, fecha: ahora() });
            return null;
        },
        /** message_create de un mensaje propio. Devuelve el mensajeChatId si era un envío del sistema. */
        mensajeCreado(texto, chat, waId) {
            limpiar();
            const normalizado = normalizar(texto);
            if (!normalizado || !waId) return null;
            const envio = tomar(enviosSinId, normalizado, chat);
            if (envio) return envio.mensajeChatId;
            creadosSinDueno.push({ texto: normalizado, chat, waId, fecha: ahora() });
            return null;
        },
    };
}

module.exports = { crearVinculador };
