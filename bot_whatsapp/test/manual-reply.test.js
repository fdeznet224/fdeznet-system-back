'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    crearRegistroEnvios,
    esEscritaPorPersona,
    esRespuestaPropia,
} = require('../manual-reply');

const AHORA = 1_800_000_000;
const sinEspera = { esperar: async () => {} };

function mensaje(extra = {}) {
    return {
        fromMe: true,
        to: '5219611234567@c.us',
        timestamp: AHORA,
        id: { id: 'ABC', _serialized: 'true_5219611234567@c.us_ABC' },
        ...extra,
    };
}

test('solo cuenta mensajes propios a chats privados y recientes', () => {
    assert.equal(esRespuestaPropia(mensaje(), AHORA), true);
    assert.equal(esRespuestaPropia(mensaje({ fromMe: false }), AHORA), false);
    assert.equal(esRespuestaPropia(mensaje({ to: '123@g.us' }), AHORA), false);
    assert.equal(esRespuestaPropia(mensaje({ isStatus: true }), AHORA), false);
    assert.equal(esRespuestaPropia(mensaje({ timestamp: AHORA - 600 }), AHORA), false);
});

test('un mensaje escrito en el celular se reconoce como de una persona', async () => {
    const registro = crearRegistroEnvios();

    assert.equal(await esEscritaPorPersona(mensaje(), registro, sinEspera), true);
});

test('un envio del sistema no se confunde con una persona', async () => {
    const registro = crearRegistroEnvios();
    registro.marcar({ id: { id: 'ABC' } });

    assert.equal(await esEscritaPorPersona(mensaje(), registro, sinEspera), false);
});

test('espera a que termine un envio del sistema que aun no devuelve su id', async () => {
    const registro = crearRegistroEnvios();
    const chat = '5219611234567@c.us';
    registro.iniciarEnvio(chat);
    let vueltas = 0;
    const esperar = async () => {
        vueltas += 1;
        if (vueltas === 10) {
            registro.terminarEnvio(chat, { id: { _serialized: 'true_5219611234567@c.us_ABC' } });
        }
    };

    assert.equal(await esEscritaPorPersona(mensaje(), registro, { esperar }), false);
    assert.equal(registro.hayEnviosEnCurso(chat), false);
});

test('los ids viejos se limpian', () => {
    const registro = crearRegistroEnvios();
    registro.marcar({ id: { id: 'VIEJO' } });

    registro.limpiar(Date.now() + 11 * 60 * 1000);

    assert.equal(registro.esDelSistema({ id: { id: 'VIEJO' } }), false);
});
