'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    comoIdTelefono,
    resolverTelefonoEntrante,
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
