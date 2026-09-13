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

module.exports = {
    comoIdTelefono,
    esTelefonoWhatsApp,
    resolverTelefonoEntrante,
};
