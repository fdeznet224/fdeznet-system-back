const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '..', 'index.js'), 'utf8');

test('los adjuntos no se publican como archivos estáticos', () => {
    assert.doesNotMatch(source, /express\.static\(UPLOADS_DIR\)/);
    assert.match(source, /whatsapp-media:\/\//);
});

test('el puente de WhatsApp solamente escucha en localhost', () => {
    // Solo localhost salvo que el contenedor pida HOST=0.0.0.0 para su red interna.
    assert.match(source, /const HOST = process\.env\.HOST \|\| '127\.0\.0\.1'/);
    assert.match(source, /app\.listen\(PORT, HOST/);
});

test('los nombres de adjuntos no son predecibles', () => {
    assert.match(source, /crypto\.randomUUID\(\)/);
});
