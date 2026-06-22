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

const app = express();
app.use(express.json());

const UPLOADS_DIR = path.join(__dirname, 'uploads');
if (!fs.existsSync(UPLOADS_DIR)) fs.mkdirSync(UPLOADS_DIR);
app.use('/uploads', express.static(UPLOADS_DIR));

let client = null; 
let isReady = false;
let lastQR = null;

function iniciarMotor() {
    console.log('⚡ Iniciando motor de WhatsApp con optimización de memoria...');
    
    client = new Client({
        authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
        webVersionCache: {
            type: 'remote',
            remotePath: 'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/2.2412.54.html',
        },
        puppeteer: { 
            headless: true, 
            args: [
                '--no-sandbox', 
                '--disable-setuid-sandbox', 
                '--disable-extensions',
                '--disable-gpu',
                '--disable-dev-shm-usage',      // CRÍTICO: Evita caídas de memoria compartida en Linux
                '--no-first-run',
                '--no-zygote',
                '--single-process',             // Reduce drásticamente el consumo de RAM de Chromium
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

        // 🔥 CORRECCIÓN 1: EL PERRO GUARDIÁN
        // Vigila si Chromium muere silenciosamente (por falta de RAM o el OOM Killer de Linux)
        client.pupBrowser.on('disconnected', () => {
            console.error('☠️ FATAL: El navegador Chromium se cerró inesperadamente (Posible pico de RAM).');
            console.log('🔄 Forzando cierre para que Systemd reinicie el servicio limpio...');
            process.exit(1); 
        });
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
            });

        } catch (e) { 
            console.error("❌ Error Webhook Recibir:", e.message); 
        }
    });

    client.on('message_ack', async (msg, ack) => {
        try { 
            await axios.post(`${BACKEND_URL}/whatsapp/webhook/ack`, { 
                wa_id: msg.id.id, 
                ack 
            }); 
        } catch (e) { 
            console.error("❌ Error Webhook Ack:", e.message); 
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
    const { numero, mensaje, ruta } = req.body; 

    if (!client || !isReady) return res.status(503).json({ error: 'WhatsApp no conectado' });

    try {
        const chatId = numero.includes('@') ? numero : `${numero}@c.us`;
        let response;

        if (ruta && fs.existsSync(ruta)) {
            const media = MessageMedia.fromFilePath(ruta);
            response = await client.sendMessage(chatId, media, { caption: mensaje });
        } else {
            response = await client.sendMessage(chatId, mensaje);
        }

        res.json({ status: 'sent', wa_id: response.id.id });
    } catch (e) { 
        res.status(500).json({ error: e.message }); 
    }
});

app.listen(PORT, () => {
    console.log(`🚀 Motor WhatsApp FdezNet en puerto ${PORT}`);
    iniciarMotor();
});