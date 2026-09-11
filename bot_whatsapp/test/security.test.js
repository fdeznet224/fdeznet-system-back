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
    assert.match(source, /app\.listen\(PORT, '127\.0\.0\.1'/);
});

test('los nombres de adjuntos no son predecibles', () => {
    assert.match(source, /crypto\.randomUUID\(\)/);
});
