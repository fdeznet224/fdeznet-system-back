require('dotenv').config();

const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const axios = require('axios');
const fs = require('fs');
const path = require('path');
const mime = require('mime-types');

const PORT = process.env.PORT || 3000;
const PUBLIC_URL = process.env.PUBLIC_URL || `http://localhost:${PORT}`;
const BACKEND_URL = process.env.API_BACKEND_URL || 'http://127.0.0.1:8000';
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || '';

if (!WEBHOOK_SECRET) {
    throw new Error('WEBHOOK_SECRET es obligatorio para autenticar los webhooks');
}

const webhookHeaders = {
    headers: { 'X-Webhook-Secret': WEBHOOK_SECRET }
};

const app = express();
app.use(express.json());

// El backend es el único consumidor del motor. Protegemos también el canal
// de control local para que un proceso vecino no pueda enviar mensajes,
// cerrar sesión o solicitar el QR sin el secreto compartido.
app.use((req, res, next) => {
    const rutasControladas = ['/status', '/init', '/logout', '/enviar-mensaje'];
    if (!rutasControladas.includes(req.path)) return next();
    const recibido = req.get('X-Webhook-Secret') || '';
    if (recibido.length !== WEBHOOK_SECRET.length || recibido !== WEBHOOK_SECRET) {
        return res.status(401).json({ error: 'No autorizado' });
    }
    return next();
});

const UPLOADS_DIR = path.join(__dirname, 'uploads');
if (!fs.existsSync(UPLOADS_DIR)) fs.mkdirSync(UPLOADS_DIR);
app.use('/uploads', express.static(UPLOADS_DIR));

let client = null; 
let isReady = false;
let lastQR = null;
const backendIdPorWaId = new Map();
const enviosCompletados = new Map();
const enviosEnCurso = new Map();
const TTL_IDEMPOTENCIA_MS = 24 * 60 * 60 * 1000;

setInterval(() => {
    const limite = Date.now() - TTL_IDEMPOTENCIA_MS;
    for (const [clave, valor] of enviosCompletados.entries()) {
        if (valor.fecha < limite) enviosCompletados.delete(clave);
    }
}, 60 * 60 * 1000).unref();

function iniciarMotor() {
    console.log('⚡ Iniciando motor de WhatsApp con optimización de memoria...');
    
    client = new Client({
        authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
        puppeteer: { 
            headless: true, 
            args: [
                '--no-sandbox', 
                '--disable-setuid-sandbox', 
                '--disable-extensions',
                '--disable-gpu',
                '--disable-dev-shm-usage',      // CRÍTICO: Evita caídas de memoria compartida en Linux
                '--no-first-run',
                '--disable-accelerated-2d-canvas'
            ] 
        }
    });

    client.on('qr', (qr) => {
        lastQR = qr;
        isReady = false;
        console.log('📢 NUEVO QR GENERADO');
        qrcode.generate(qr, { small: true });
    });

    client.on('ready', () => {
        isReady = true;
        lastQR = null;
        console.log('✅ WHATSAPP CONECTADO Y LISTO');

        // 1. Vigila si Chromium muere (El Perro Guardián que ya tenías)
        client.pupBrowser.on('disconnected', () => {
            console.error('☠️ FATAL: El navegador Chromium se cerró inesperadamente.');
            process.exit(1); 
        });

        // 🔥 2. Heartbeat tolerante contra estados temporales de WhatsApp
        // WhatsApp Web a veces devuelve null aunque siga conectado.
        // Por eso NO reiniciamos en el primer fallo.
        if (global.fdeznetHeartbeatTimer) {
            clearInterval(global.fdeznetHeartbeatTimer);
        }

        let heartbeatFails = 0;

        global.fdeznetHeartbeatTimer = setInterval(async () => {
            try {
                const state = await client.getState();

                if (state === 'CONNECTED') {
                    heartbeatFails = 0;
                    return;
                }

                if (state === null || state === undefined) {
                    console.warn(`⚠️ Heartbeat temporal: WhatsApp devolvió ${state}. No se reinicia ni se borra sesión.`);
                    return;
                }

                heartbeatFails += 1;
                console.warn(`⚠️ Heartbeat WhatsApp estado=${state}. Fallo ${heartbeatFails}/3.`);

                if (heartbeatFails >= 3) {
                    console.error('☠️ WhatsApp no volvió a CONNECTED después de 3 intentos. Delegando reinicio a Systemd...');
                    process.exit(1);
                }
            } catch (error) {
                heartbeatFails += 1;
                console.error(`⚠️ Falló ping a WhatsApp. Fallo ${heartbeatFails}/3:`, error.message);

                if (heartbeatFails >= 3) {
                    console.error('☠️ Heartbeat falló 3 veces seguidas. Reiniciando proceso...');
                    process.exit(1);
                }
            }
        }, 300000); 
});

    client.on('auth_failure', async (msg) => {
        console.error('❌ FALLO DE AUTENTICACIÓN:', msg);
        isReady = false;
        await detenerMotor();
        const authPath = path.join(__dirname, '.wwebjs_auth');
        if (fs.existsSync(authPath)) fs.rmSync(authPath, { recursive: true, force: true });
        console.log('🔄 Sesión limpia. Por favor, reinicia o llama a /init para reescanear.');
    });

    client.on('disconnected', async (reason) => {
        console.log('❌ CLIENTE DESCONECTADO:', reason);
        isReady = false;
        
        // 🔥 CORRECCIÓN 2: DELEGAR REINICIO A SYSTEMD
        // Eliminamos el setTimeout que causaba fugas de memoria y procesos zombies.
        // Al salir con código de error (1), Systemd detecta la caída y levanta Node.js fresco al instante.
        console.log('🔄 Delegando auto-reconexión a Systemd...');
        process.exit(1);
    });

    client.on('message', async (msg) => {
        // 1. Ignorar grupos y estados
        if(msg.from.includes('@g.us') || msg.isStatus) return;

        // 2. Filtro Anti-Avalancha de mensajes viejos
        const tiempoActual = Math.floor(Date.now() / 1000);
        const edadDelMensaje = tiempoActual - msg.timestamp;

        if (edadDelMensaje > 120) {
            return; // Detiene la ejecución aquí, no envía nada al webhook de Python
        }

        try {
            let contenido = msg.body;
            let mediaUrl = null;

            if (msg.type === 'location') {
                const { latitude: lat, longitude: lng } = msg.location;
                contenido = `📍 Ubicación: http://maps.google.com/maps?q=${lat},${lng}`;
            }

            if (msg.hasMedia) {
                const media = await msg.downloadMedia();
                if (media) {
                    let ext = mime.extension(media.mimetype) || 'bin';
                    const fileName = `${msg.type}_${Date.now()}.${ext}`;
                    const filePath = path.join(UPLOADS_DIR, fileName);
                    
                    fs.writeFileSync(filePath, media.data, { encoding: 'base64' });
                    mediaUrl = `${PUBLIC_URL}/uploads/${fileName}`;

                    if (msg.type === 'image') {
                        contenido = `[FOTO_COMPROBANTE]`;
                    } else if (msg.type === 'audio' || msg.type === 'ptt') {
                        contenido = `[AUDIO]`;
                    } else {
                        contenido = `[ARCHIVO]`;
                    }
                }
            }

            let numeroReal = msg.from;
            try {
                const contact = await msg.getContact();
                if (contact && contact.number) {
                    numeroReal = `${contact.number}@c.us`; 
                }
            } catch (err) {
                console.log("⚠️ No se pudo obtener el número real del contacto:", err.message);
            }

            await axios.post(`${BACKEND_URL}/whatsapp/webhook/recibir`, {
                telefono: numeroReal,       
                telefono_raw: msg.from,     
                mensaje: contenido,
                mediaUrl: mediaUrl,
                wa_id: msg.id.id
            }, webhookHeaders);

        } catch (e) { 
            console.error("❌ Error Webhook Recibir:", e.message); 
        }
    });

    client.on('message_ack', async (msg, ack) => {
        const payload = {
            wa_id: msg.id.id,
            ack,
            mensaje_chat_id: backendIdPorWaId.get(msg.id.id) || null
        };
        for (let intento = 0; intento < 4; intento += 1) {
            try {
                const respuesta = await axios.post(
                    `${BACKEND_URL}/whatsapp/webhook/ack`,
                    payload,
                    webhookHeaders
                );
                if (respuesta.data?.matched !== false) {
                    if (ack >= 3) backendIdPorWaId.delete(msg.id.id);
                    return;
                }
                await new Promise(resolve => setTimeout(resolve, 500));
            } catch (e) {
                if (intento === 3) {
                    console.error("❌ Error Webhook Ack:", e.message);
                    return;
                }
                await new Promise(resolve => setTimeout(resolve, 500));
            }
        }
    });

    client.initialize().catch(err => {
        console.error("❌ Fallo crítico al inicializar:", err.message);
        // Si falla al arrancar de cero, también nos suicidamos para que Systemd intente de nuevo
        process.exit(1);
    });
}

async function detenerMotor() {
    console.log('🛑 Deteniendo motor y limpiando memoria...');
    if (client) {
        try { 
            await client.destroy(); 
        } catch (e) {
            console.log('Clean up: El proceso ya estaba cerrado.');
        }
    }
    client = null;
    isReady = false;
}

// --- ENDPOINTS API ---

app.get('/status', (req, res) => {
    res.json({ active: client !== null, connected: isReady, qr: lastQR });
});

app.post('/init', (req, res) => {
    if (client) return res.json({ status: 'already_running' });
    iniciarMotor();
    res.json({ status: 'initializing' });
});

app.post('/logout', async (req, res) => {
    try {
        if (client && isReady) await client.logout();
    } catch (e) {}
    await detenerMotor();
    const authPath = path.join(__dirname, '.wwebjs_auth');
    if (fs.existsSync(authPath)) fs.rmSync(authPath, { recursive: true, force: true });
    res.json({ status: 'stopped' });
});

app.post('/enviar-mensaje', async (req, res) => {
    const { numero, mensaje, ruta, mensaje_chat_id: mensajeChatId } = req.body;

    if (!client || !isReady) return res.status(503).json({ error: 'WhatsApp no conectado' });

    const claveIdempotencia = mensajeChatId ? String(mensajeChatId) : null;
    if (claveIdempotencia && enviosCompletados.has(claveIdempotencia)) {
        return res.json(enviosCompletados.get(claveIdempotencia).resultado);
    }

    try {
        let promesaEnvio = (
            claveIdempotencia
            ? enviosEnCurso.get(claveIdempotencia)
            : null
        );
        if (!promesaEnvio) {
            promesaEnvio = (async () => {
                const chatId = numero.includes('@') ? numero : `${numero}@c.us`;
                let response;

                if (ruta) {
                    if (!fs.existsSync(ruta)) {
                        const error = new Error('El archivo adjunto ya no existe');
                        error.statusCode = 400;
                        throw error;
                    }
                    const media = MessageMedia.fromFilePath(ruta);
                    response = await client.sendMessage(
                        chatId,
                        media,
                        { caption: mensaje }
                    );
                } else {
                    response = await client.sendMessage(chatId, mensaje);
                }
                const resultado = {
                    status: 'sent',
                    wa_id: response.id.id,
                    mensaje_chat_id: mensajeChatId || null
                };
                if (mensajeChatId) {
                    backendIdPorWaId.set(response.id.id, Number(mensajeChatId));
                }
                return resultado;
            })();
            if (claveIdempotencia) {
                enviosEnCurso.set(claveIdempotencia, promesaEnvio);
            }
        }

        const resultado = await promesaEnvio;
        if (claveIdempotencia) {
            enviosCompletados.set(
                claveIdempotencia,
                { resultado, fecha: Date.now() }
            );
        }
        res.json(resultado);
    } catch (e) { 
        res.status(e.statusCode || 500).json({ error: e.message });
    } finally {
        if (claveIdempotencia) enviosEnCurso.delete(claveIdempotencia);
    }
});

app.listen(PORT, () => {
    console.log(`🚀 Motor WhatsApp FdezNet en puerto ${PORT}`);
    iniciarMotor();
});
