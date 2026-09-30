'use strict';

// Desde el 17/09/2026 WhatsApp Web (2.3000.10477+) rechaza todos los envíos
// con archivo de whatsapp-web.js 1.34.7: "Data passed to getter must include
// an id property (it's how we memoize) but got undefined". Al copiar los datos
// del archivo en el mensaje se arrastra `__x_id: undefined`, que WhatsApp toma
// como el id del mensaje. Se borra antes de enviarlo, como propone el reporte
// https://github.com/wwebjs/whatsapp-web.js/issues/201922.
// Quitar cuando una versión oficial de la librería incluya la corrección.

const fs = require('fs');
const path = require('path');

const MARCA = 'FDEZNET_PARCHE_MEDIA_ID';
const ANCLA = "        // Bot's won't reply if canonicalUrl is set (linking)";
const PARCHE = [
    `        // ${MARCA}: WhatsApp Web 2.3000.10477+ toma __x_id como id del mensaje.`,
    '        delete message.__x_id;',
    '',
    '',
].join('\n');

function rutaUtils() {
    return path.join(
        path.dirname(require.resolve('whatsapp-web.js/package.json')),
        'src', 'util', 'Injected', 'Utils.js'
    );
}

function parchearCodigo(codigo) {
    if (codigo.includes(MARCA)) return { estado: 'ya_aplicado', codigo };
    if (!codigo.includes(ANCLA)) return { estado: 'no_encontrado', codigo };
    return { estado: 'aplicado', codigo: codigo.replace(ANCLA, PARCHE + ANCLA) };
}

function aplicarParche(ruta = rutaUtils()) {
    const resultado = parchearCodigo(fs.readFileSync(ruta, 'utf8'));
    if (resultado.estado === 'aplicado') {
        fs.writeFileSync(ruta, resultado.codigo);
    }
    return resultado.estado;
}

if (require.main === module) {
    try {
        const estado = aplicarParche();
        console.log(`Parche de envío de archivos de WhatsApp: ${estado}`);
        if (estado === 'no_encontrado') {
            console.warn(
                'La librería cambió; revisa si ya corrige el envío de archivos.'
            );
        }
    } catch (error) {
        // Nunca bloquear la instalación: el bot lo reintenta al arrancar.
        console.warn(`No se pudo aplicar el parche: ${error.message}`);
    }
}

module.exports = { aplicarParche, parchearCodigo, MARCA };
