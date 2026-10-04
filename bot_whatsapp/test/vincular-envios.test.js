'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { crearVinculador } = require('../vincular-envios');

test('liga el id cuando message_create llega después del envío', () => {
    const v = crearVinculador();
    assert.equal(v.envioSinId('¡Pago Confirmado!\n Hola', '52198@c.us', 13004), null);
    assert.equal(v.mensajeCreado('¡Pago Confirmado! Hola', '52198@c.us', 'WA1'), 13004);
});

test('liga el id cuando message_create llega antes de que termine el envío', () => {
    const v = crearVinculador();
    assert.equal(v.mensajeCreado('Recordatorio', '52198@c.us', 'WA2'), null);
    assert.equal(v.envioSinId('Recordatorio', '52198@c.us', 77), 'WA2');
});

test('con el mismo texto a dos chats prefiere el chat que coincide', () => {
    const v = crearVinculador();
    v.envioSinId('Alerta: router caído', 'admin1@c.us', 1);
    v.envioSinId('Alerta: router caído', 'admin2@c.us', 2);
    assert.equal(v.mensajeCreado('Alerta: router caído', 'admin2@c.us', 'WA3'), 2);
    assert.equal(v.mensajeCreado('Alerta: router caído', 'admin1@c.us', 'WA4'), 1);
});

test('un mensaje escrito a mano no se liga a nada', () => {
    const v = crearVinculador();
    v.envioSinId('Pago confirmado', 'a@c.us', 5);
    assert.equal(v.mensajeCreado('Hola, ¿cómo estás?', 'a@c.us', 'WA5'), null);
});

test('cada envío se liga una sola vez y caduca a los 2 minutos', () => {
    let reloj = 0;
    const v = crearVinculador(() => reloj);
    v.envioSinId('Aviso', 'a@c.us', 9);
    assert.equal(v.mensajeCreado('Aviso', 'a@c.us', 'WA6'), 9);
    assert.equal(v.mensajeCreado('Aviso', 'a@c.us', 'WA7'), null);
    v.envioSinId('Viejo', 'a@c.us', 10);
    reloj = 3 * 60 * 1000;
    assert.equal(v.mensajeCreado('Viejo', 'a@c.us', 'WA8'), null);
});
