'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    comoIdTelefono,
    recordarChat,
    resolverChatSalida,
    resolverTelefonoEntrante,
    variantesNumero,
} = require('../phone-utils');

test('conserva un remitente telefónico normal', async () => {
    const result = await resolverTelefonoEntrante({}, {
        from: '5219613699652@c.us',
    });
    assert.equal(result, '5219613699652@c.us');
});

test('traduce un LID al número telefónico mediante whatsapp-web.js', async () => {
    const client = {
        async getContactLidAndPhone(ids) {
            assert.deepEqual(ids, ['123456789012345@lid']);
            return [{
                lid: '123456789012345@lid',
                pn: '5219613699652@c.us',
            }];
        },
    };
    const result = await resolverTelefonoEntrante(client, {
        from: '123456789012345@lid',
    });
    assert.equal(result, '5219613699652@c.us');
});

test('no convierte un LID opaco en un teléfono inventado', async () => {
    const client = {
        async getContactLidAndPhone() {
            return [{ lid: '123456789012345@lid', pn: null }];
        },
    };
    const result = await resolverTelefonoEntrante(client, {
        from: '123456789012345@lid',
        async getContact() {
            return {
                id: { _serialized: '123456789012345@lid' },
                number: '123456789012345',
            };
        },
    });
    assert.equal(result, '123456789012345@lid');
});

test('normaliza un número sin sufijo', () => {
    assert.equal(comoIdTelefono('+52 1 961 369 9652'), '5219613699652@c.us');
});


test('un número mexicano se prueba con 521 y con 52', () => {
    assert.deepEqual(variantesNumero('5219613339474'), ['5219613339474', '529613339474']);
    assert.deepEqual(variantesNumero('529613339474'), ['529613339474', '5219613339474']);
});

test('usa el ID que WhatsApp reporta para el número (cuenta 52 sin el 1)', async () => {
    const consultas = [];
    const client = {
        async getNumberId(numero) {
            consultas.push(numero);
            return numero === '529613339474' ? { _serialized: '529613339474@c.us' } : null;
        },
    };
    assert.equal(await resolverChatSalida(client, '5219613339474'), '529613339474@c.us');
    assert.deepEqual(consultas, ['5219613339474', '529613339474']);
});

test('si ninguna variante tiene WhatsApp responde 404 sin enviar', async () => {
    const client = { async getNumberId() { return null; } };
    await assert.rejects(
        resolverChatSalida(client, '5219611172026'),
        (error) => error.statusCode === 404 && error.message.startsWith('NUMERO_SIN_WHATSAPP'),
    );
});

test('un chat @lid se usa tal cual', async () => {
    assert.equal(await resolverChatSalida({}, '199347383861472@lid'), '199347383861472@lid');
});

test('si WhatsApp no contesta la consulta se conserva el envío de siempre', async () => {
    const client = { async getNumberId() { throw new Error('timeout'); } };
    assert.equal(await resolverChatSalida(client, '5219610000001'), '5219610000001@c.us');
});

test('un número de 10 dígitos o con +52, guiones o espacios se prueba con 521 y 52', () => {
    assert.deepEqual(variantesNumero('9613339474'), ['5219613339474', '529613339474']);
    assert.deepEqual(variantesNumero('961-333-9474'), ['5219613339474', '529613339474']);
    assert.deepEqual(variantesNumero('+52 961 333 9474'), ['529613339474', '5219613339474']);
    assert.deepEqual(variantesNumero('044 961 333 9474'), ['5219613339474', '529613339474']);
    assert.deepEqual(variantesNumero('+1 805 555 0100'), ['18055550100']);
});

test('si la consulta falla después, usa el chat que ya funcionó con ese número', async () => {
    let responde = true;
    const client = {
        async getNumberId(numero) {
            if (!responde) return null;
            return numero === '529612220001' ? { _serialized: '529612220001@c.us' } : null;
        },
    };
    assert.equal(await resolverChatSalida(client, '9612220001'), '529612220001@c.us');
    responde = false;
    assert.equal(await resolverChatSalida(client, '5219612220001'), '529612220001@c.us');
});

test('recuerda el chat de quien nos escribe para responderle aunque la consulta falle', async () => {
    recordarChat('5219612220002', '123456789012345@lid');
    const client = { async getNumberId() { return null; } };
    assert.equal(await resolverChatSalida(client, '9612220002'), '123456789012345@lid');
});

test('si WhatsApp no encuentra el número pero ya hay chat en la sesión, se usa ese chat', async () => {
    const client = {
        async getNumberId() { return null; },
        async getContactLidAndPhone() { return [{ lid: '555000111222333@lid', pn: '529612220003@c.us' }]; },
        pupPage: {
            async evaluate(_fn, ids) {
                assert.ok(ids.includes('5219612220003@c.us') && ids.includes('555000111222333@lid'));
                return '555000111222333@lid';
            },
        },
    };
    assert.equal(await resolverChatSalida(client, '9612220003'), '555000111222333@lid');
});
