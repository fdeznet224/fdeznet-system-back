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

async function descargarMediaConReintentos(
    mensaje,
    { intentos = 3, retrasoMs = 1200, esperarFn = esperar } = {}
) {
    let ultimoError = new Error('WhatsApp no entregó el archivo multimedia');

    for (let intento = 1; intento <= intentos; intento += 1) {
        try {
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

module.exports = { descargarMediaConReintentos, describirError };
