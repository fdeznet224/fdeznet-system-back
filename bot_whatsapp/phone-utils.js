'use strict';

function esTelefonoWhatsApp(valor) {
    const texto = String(valor || '').trim().toLowerCase();
    if (texto.endsWith('@lid')) return false;
    const digitos = texto.replace(/\D/g, '');
    return digitos.length >= 10;
}

function comoIdTelefono(valor) {
    const texto = String(valor || '').trim();
    if (!esTelefonoWhatsApp(texto)) return null;
    if (texto.includes('@')) return texto;
    return `${texto.replace(/\D/g, '')}@c.us`;
}

/**
 * WhatsApp puede entregar mensajes privados con un identificador opaco @lid.
 * Esta función usa la API de whatsapp-web.js que traduce LID a PN y solo
 * devuelve un número cuando la traducción es verificable.
 */
async function resolverTelefonoEntrante(client, msg) {
    const remitente = String(msg?.from || '').trim();
    if (!remitente.toLowerCase().endsWith('@lid')) {
        return comoIdTelefono(remitente) || remitente;
    }

    if (client && typeof client.getContactLidAndPhone === 'function') {
        try {
            const relaciones = await client.getContactLidAndPhone([remitente]);
            const relacion = Array.isArray(relaciones) ? relaciones[0] : relaciones;
            const telefono = comoIdTelefono(relacion?.pn);
            if (telefono) return telefono;
        } catch (error) {
            // Se intenta después con los datos del contacto ya cargado.
        }
    }

    try {
        const contacto = await msg.getContact();
        const idContacto = contacto?.id?._serialized;
        const idEsLid = String(idContacto || '').toLowerCase().endsWith('@lid');
        const telefono = comoIdTelefono(idContacto)
            || (!idEsLid ? comoIdTelefono(contacto?.number) : null);
        if (telefono) return telefono;
    } catch (error) {
        // El backend recibirá el LID y evitará tratarlo como un teléfono real.
    }

    return remitente;
}

/**
 * Variantes de un número mexicano: WhatsApp guarda las cuentas antiguas como
 * 521 + 10 dígitos y las nuevas como 52 + 10 dígitos.
 */
function diezDigitosMx(digitos) {
    let d = String(digitos || '');
    if (d.startsWith('00')) d = d.slice(2);
    if (d.length === 13 && d.startsWith('521')) return d.slice(3);
    if (d.length === 12 && d.startsWith('52')) return d.slice(2);
    if (d.length === 13 && (d.startsWith('044') || d.startsWith('045'))) return d.slice(3);
    if (d.length === 10) return d;
    return null;
}

/**
 * Variantes de un número: uno mexicano (10 dígitos, +52, +521, 044, con
 * espacios o guiones) se prueba como 521 + 10 dígitos y como 52 + 10 dígitos,
 * en el orden en que llegó. Los 10 dígitos solos no se consultan: sin lada de
 * país WhatsApp los tomaría como de otro país.
 */
function variantesNumero(valor) {
    const digitos = String(valor || '').replace(/\D/g, '');
    if (!digitos) return [];
    const diez = diezDigitosMx(digitos);
    if (!diez) return [digitos];
    const variantes = [`521${diez}`, `52${diez}`];
    if (digitos === `52${diez}`) variantes.reverse();
    return variantes;
}

// Chat que WhatsApp ya confirmó para cada número (10 dígitos), mientras el
// puente siga encendido. Sirve cuando la consulta del número falla un rato.
const chatsConfirmados = new Map();

function claveNumero(variantes) {
    return diezDigitosMx(variantes[0]) || variantes[0];
}

function recordarChat(numero, chatId) {
    const variantes = variantesNumero(numero);
    if (!variantes.length || !chatId) return;
    chatsConfirmados.set(claveNumero(variantes), String(chatId));
}

/**
 * Chat que ya existe en la sesión de WhatsApp para alguna variante (por
 * número o por su LID). Solo mira los chats cargados: no consulta al servidor.
 */
async function chatExistente(client, variantes) {
    const ids = variantes.map((v) => `${v}@c.us`);
    if (client && typeof client.getContactLidAndPhone === 'function') {
        try {
            const relaciones = await client.getContactLidAndPhone(ids);
            for (const relacion of [].concat(relaciones || [])) {
                if (relacion?.lid) ids.push(String(relacion.lid));
            }
        } catch (error) {
            // Sin la relación LID se buscan solo los chats por número.
        }
    }
    if (!client?.pupPage || typeof client.pupPage.evaluate !== 'function') return null;
    try {
        return await client.pupPage.evaluate((candidatos) => {
            const { Chat } = window.require('WAWebCollections');
            const crearWid = window.require('WAWebWidFactory').createWid;
            for (const id of candidatos) {
                try {
                    const chat = Chat.get(crearWid(id));
                    if (chat?.id?._serialized) return chat.id._serialized;
                } catch (error) {
                    // ID inválido para esta versión de WhatsApp Web.
                }
            }
            return null;
        }, ids);
    } catch (error) {
        return null;
    }
}

/**
 * Chat al que se envía un mensaje saliente. Le pregunta a WhatsApp el ID real
 * del número (como hace WhatsApp Web al escribir un número a mano) en vez de
 * suponer "521...@c.us", que falla con "No LID for user" en cuentas 52.
 * Si WhatsApp no lo encuentra, antes de darlo por "sin WhatsApp" se usa el
 * chat que ya funcionó o el que ya existe en la sesión: a veces la consulta
 * falla unos minutos con números que sí tienen WhatsApp.
 */
async function resolverChatSalida(client, numero) {
    const texto = String(numero || '').trim();
    if (texto.includes('@')) return texto;
    const variantes = variantesNumero(texto);
    if (!variantes.length) {
        const error = new Error('NUMERO_SIN_WHATSAPP: número vacío');
        error.statusCode = 404;
        throw error;
    }
    if (!client || typeof client.getNumberId !== 'function') return `${variantes[0]}@c.us`;
    const clave = claveNumero(variantes);
    let consultado = false;
    for (const variante of variantes) {
        try {
            const wid = await client.getNumberId(variante);
            consultado = true;
            if (wid?._serialized) {
                chatsConfirmados.set(clave, wid._serialized);
                return wid._serialized;
            }
        } catch (error) {
            // Si WhatsApp no responde la consulta, se intenta la siguiente variante.
        }
    }
    const conocido = chatsConfirmados.get(clave);
    if (conocido) return conocido;
    const existente = await chatExistente(client, variantes);
    if (existente) return existente;
    if (!consultado) return `${variantes[0]}@c.us`;
    const error = new Error(`NUMERO_SIN_WHATSAPP: ${variantes.join(' / ')} no tiene WhatsApp`);
    error.statusCode = 404;
    throw error;
}

module.exports = {
    comoIdTelefono,
    recordarChat,
    resolverChatSalida,
    variantesNumero,
    esTelefonoWhatsApp,
    resolverTelefonoEntrante,
};
