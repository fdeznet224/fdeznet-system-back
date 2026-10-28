'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { argumentosChrome, msHastaHora } = require('../memoria');

test('Chrome ligero por defecto y se puede apagar', () => {
    const ligero = argumentosChrome({});
    assert.ok(ligero.includes('--no-sandbox'));
    assert.ok(ligero.includes('--disable-site-isolation-trials'));

    const normal = argumentosChrome({ BOT_CHROME_LIGERO: '0' });
    assert.ok(normal.includes('--no-sandbox'));
    assert.ok(!normal.includes('--disable-site-isolation-trials'));
});

test('argumentos extra separados por coma', () => {
    const args = argumentosChrome({ BOT_CHROME_ARGS_EXTRA: ' --a=1 ,, --b ' });
    assert.deepEqual(args.slice(-2), ['--a=1', '--b']);
});

test('reinicio diario: hoy si no ha pasado, mañana si ya pasó', () => {
    const ahora = new Date(2026, 9, 28, 3, 0, 0);
    assert.equal(msHastaHora('04:30', ahora), 90 * 60 * 1000);
    assert.equal(msHastaHora('03:00', ahora), 24 * 60 * 60 * 1000);
    assert.equal(msHastaHora('02:00', ahora), 23 * 60 * 60 * 1000);
});

test('hora inválida o vacía desactiva el reinicio', () => {
    assert.equal(msHastaHora(''), null);
    assert.equal(msHastaHora('25:00'), null);
    assert.equal(msHastaHora('4:30'), null);
});
