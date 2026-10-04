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
function variantesNumero(valor) {
    const digitos = String(valor || '').replace(/\D/g, '');
    if (!digitos) return [];
    const variantes = [digitos];
    if (digitos.length === 13 && digitos.startsWith('521')) variantes.push(`52${digitos.slice(3)}`);
    if (digitos.length === 12 && digitos.startsWith('52')) variantes.push(`521${digitos.slice(2)}`);
    return variantes;
}

/**
 * Chat al que se envía un mensaje saliente. Le pregunta a WhatsApp el ID real
 * del número (como hace WhatsApp Web al escribir un número a mano) en vez de
 * suponer "521...@c.us", que falla con "No LID for user" en cuentas 52.
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
    let consultado = false;
    for (const variante of variantes) {
        try {
            const wid = await client.getNumberId(variante);
            consultado = true;
            if (wid?._serialized) return wid._serialized;
        } catch (error) {
            // Si WhatsApp no responde la consulta, se intenta la siguiente variante.
        }
    }
    if (!consultado) return `${variantes[0]}@c.us`;
    const error = new Error(`NUMERO_SIN_WHATSAPP: ${variantes.join(' / ')} no tiene WhatsApp`);
    error.statusCode = 404;
    throw error;
}

module.exports = {
    comoIdTelefono,
    resolverChatSalida,
    variantesNumero,
    esTelefonoWhatsApp,
    resolverTelefonoEntrante,
};
