'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    descargarMediaConReintentos,
    describirError,
    repararIdSerializado
} = require('../media-utils');

test('recupera el nuevo identificador $1 usado por WhatsApp Web', () => {
    const mensaje = {
        id: {
            fromMe: false,
            remote: '5215550000000@lid',
            id: 'ABC123',
            $1: 'false_5215550000000@lid_ABC123'
        }
    };

    assert.equal(repararIdSerializado(mensaje), true);
    assert.equal(
        mensaje.id._serialized,
        'false_5215550000000@lid_ABC123'
    );
});

test('reconstruye el identificador cuando $1 tampoco está disponible', () => {
    const mensaje = {
        id: {
            fromMe: false,
            remote: '5215550000000@c.us',
            id: 'XYZ789'
        }
    };

    assert.equal(repararIdSerializado(mensaje), true);
    assert.equal(
        mensaje.id._serialized,
        'false_5215550000000@c.us_XYZ789'
    );
});

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
