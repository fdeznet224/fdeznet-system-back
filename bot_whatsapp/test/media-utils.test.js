'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    descargarMediaConReintentos,
    describirError
} = require('../media-utils');

test('reintenta cuando WhatsApp aún no entrega el archivo', async () => {
    let llamadas = 0;
    const mensaje = {
        async downloadMedia() {
            llamadas += 1;
            if (llamadas < 3) return undefined;
            return { data: 'base64', mimetype: 'image/jpeg' };
        }
    };

    const media = await descargarMediaConReintentos(mensaje, {
        intentos: 3,
        retrasoMs: 0,
        esperarFn: async () => {}
    });

    assert.equal(llamadas, 3);
    assert.equal(media.mimetype, 'image/jpeg');
});

test('conserva un error entendible al agotar los reintentos', async () => {
    const mensaje = {
        async downloadMedia() {
            throw 'fallo temporal';
        }
    };

    await assert.rejects(
        descargarMediaConReintentos(mensaje, {
            intentos: 2,
            retrasoMs: 0,
            esperarFn: async () => {}
        }),
        /fallo temporal/
    );
});

test('describe errores HTTP sin imprimir cuerpos sensibles', () => {
    assert.equal(describirError({ response: { status: 503 } }), 'HTTP 503');
});
