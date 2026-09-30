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

test('un envío del sistema a un LID sin id se reconoce por su texto una sola vez', async () => {
    const registro = crearRegistroEnvios();
    registro.iniciarEnvio('250904087396523@lid');
    // WhatsApp no devolvió identificador para el envío al LID.
    registro.terminarEnvio('250904087396523@lid', undefined, '¡Buenos días!\nCon quien tengo el gusto?');

    const eco = mensaje({ id: { id: '3EB0', _serialized: 'true_5219613699652@c.us_3EB0' }, body: '¡Buenos días! Con quien tengo el gusto?' });
    assert.equal(await esEscritaPorPersona(eco, registro, sinEspera), false);

    // Si una persona escribe lo mismo después, ya no se confunde con el envío.
    const repetido = mensaje({ id: { id: '3EB1', _serialized: 'true_5219613699652@c.us_3EB1' }, body: '¡Buenos días! Con quien tengo el gusto?' });
    assert.equal(await esEscritaPorPersona(repetido, registro, sinEspera), true);
});

test('un texto distinto escrito desde el celular sí cuenta como persona', async () => {
    const registro = crearRegistroEnvios();
    registro.terminarEnvio('250904087396523@lid', undefined, 'Respuesta del agente');
    const persona = mensaje({ id: { id: 'XYZ' }, body: 'Ahorita le reviso, un momento' });
    assert.equal(await esEscritaPorPersona(persona, registro, sinEspera), true);
});

test('el texto de un envío caduca a los dos minutos', () => {
    const registro = crearRegistroEnvios();
    registro.terminarEnvio('chat@lid', undefined, 'Hola');
    const eco = mensaje({ id: { id: 'Q' }, body: 'Hola' });
    assert.equal(registro.esDelSistema(eco, Date.now() + 3 * 60 * 1000), false);
});
