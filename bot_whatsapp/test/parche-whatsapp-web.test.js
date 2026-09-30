'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { parchearCodigo, MARCA } = require('../parche-whatsapp-web');

const UTILS = path.join(
    path.dirname(require.resolve('whatsapp-web.js/package.json')),
    'src', 'util', 'Injected', 'Utils.js'
);

test('el mensaje con archivo ya no conserva el __x_id de los datos del archivo', () => {
    const codigo = fs.readFileSync(UTILS, 'utf8');
    assert.ok(codigo.includes(MARCA), 'el parche debe estar aplicado en node_modules');

    // El borrado ocurre después de copiar mediaOptions y antes de usar el mensaje.
    const copia = codigo.indexOf('...mediaOptions,');
    const borrado = codigo.indexOf('delete message.__x_id;');
    const uso = codigo.indexOf("if (botOptions) {\n            delete message.canonicalUrl;");
    assert.ok(copia > 0 && copia < borrado && borrado < uso);
});

test('el parche no se duplica y no toca código que no reconoce', () => {
    const original = "const message = {};\n        // Bot's won't reply if canonicalUrl is set (linking)\n";
    const primero = parchearCodigo(original);
    assert.equal(primero.estado, 'aplicado');
    assert.equal(parchearCodigo(primero.codigo).estado, 'ya_aplicado');
    assert.equal(parchearCodigo('otra versión').estado, 'no_encontrado');
});

test('borrar __x_id deja intacto el id del mensaje', () => {
    const datosArchivo = { __x_id: undefined, mimetype: 'application/pdf' };
    const message = { id: 'clave-del-mensaje', ...datosArchivo };
    assert.ok('__x_id' in message);
    delete message.__x_id;
    assert.equal(message.id, 'clave-del-mensaje');
    assert.equal('__x_id' in message, false);
});
