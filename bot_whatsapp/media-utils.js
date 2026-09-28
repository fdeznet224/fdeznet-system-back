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

// Respaldo para cuando Message.downloadMedia() de whatsapp-web.js 1.34.7
// truena con errores minificados ("t", "r"): su downloadAndMaybeDecrypt dejó
// de funcionar con WhatsApp Web 2.3000.x. Es el mismo método que usa la rama
// main de la librería (resolveMediaBlob): pedir a WhatsApp Web que resuelva
// el archivo y leerlo de su caché de blobs.
async function descargarDesdeCacheWeb(mensaje) {
    const pagina = mensaje?.client?.pupPage;
    const msgId = mensaje?.id?._serialized;
    if (!pagina || !msgId) return undefined;

    return pagina.evaluate(async (id) => {
        const { Msg } = window.require('WAWebCollections');
        const msg = Msg.get(id)
            || (await Msg.getMessagesById([id]))?.messages?.[0];
        if (!msg || !msg.mediaData || msg.mediaData.mediaStage === 'REUPLOADING') {
            return null;
        }

        await msg.downloadMedia({
            downloadEvenIfExpensive: true,
            rmrReason: 1,
            isUserInitiated: true
        });
        const etapa = msg.mediaData.mediaStage || '';
        if (etapa.includes('ERROR') || etapa === 'FETCHING') return null;

        const blob = window.require('WAWebMediaInMemoryBlobCache')
            .InMemoryMediaBlobCache.get(msg.mediaObject?.filehash)
            || msg.mediaObject?.mediaBlob?.forceToBlob();
        if (!blob) return null;

        const data = await new Promise((resolve, reject) => {
            const lector = new FileReader();
            lector.onload = () => resolve(String(lector.result).split(',')[1]);
            lector.onerror = reject;
            lector.readAsDataURL(blob);
        });
        return {
            data,
            mimetype: msg.mimetype,
            filename: msg.filename,
            filesize: msg.size
        };
    }, msgId);
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

        try {
            const media = await descargarDesdeCacheWeb(mensaje);
            if (media?.data && media?.mimetype) return media;
        } catch (error) {
            ultimoError = error;
        }

        if (intento < intentos) await esperarFn(retrasoMs * intento);
    }

    throw new Error(describirError(ultimoError));
}

module.exports = {
    descargarDesdeCacheWeb,
    descargarMediaConReintentos,
    describirError,
    repararIdSerializado
};
