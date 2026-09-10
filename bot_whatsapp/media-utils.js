'use strict';

const esperar = (milisegundos) => new Promise(
    resolve => setTimeout(resolve, milisegundos)
);

function describirError(error) {
    if (error instanceof Error && error.message) return error.message;
    if (typeof error === 'string' && error.trim()) return error.trim();
    if (error?.response?.status) return `HTTP ${error.response.status}`;
    return 'error desconocido';
}

function repararIdSerializado(mensaje) {
    const id = mensaje?.id;
    if (!id || id._serialized) return Boolean(id?._serialized);

    const remoto = typeof id.remote === 'string'
        ? id.remote
        : id.remote?._serialized;
    const reconstruido = id.$1
        || id.serialized
        || (
            id.fromMe !== undefined && remoto && id.id
                ? `${id.fromMe}_${remoto}_${id.id}`
                : null
        );

    if (!reconstruido) return false;

    try {
        id._serialized = reconstruido;
    } catch (_error) {
        try {
            Object.defineProperty(id, '_serialized', {
                value: reconstruido,
                writable: true,
                configurable: true
            });
        } catch (_defineError) {
            return false;
        }
    }
    return id._serialized === reconstruido;
}

async function descargarMediaConReintentos(
    mensaje,
    { intentos = 3, retrasoMs = 1200, esperarFn = esperar } = {}
) {
    let ultimoError = new Error('WhatsApp no entregó el archivo multimedia');

    for (let intento = 1; intento <= intentos; intento += 1) {
        try {
            repararIdSerializado(mensaje);
            const media = await mensaje.downloadMedia();
            if (media?.data && media?.mimetype) return media;
            ultimoError = new Error('WhatsApp no entregó el archivo multimedia');
        } catch (error) {
            ultimoError = error;
        }

        if (intento < intentos) await esperarFn(retrasoMs * intento);
    }

    throw new Error(describirError(ultimoError));
}

module.exports = {
    descargarMediaConReintentos,
    describirError,
    repararIdSerializado
};
