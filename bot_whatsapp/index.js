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
// URL de tu API de FastAPI
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
    console.log('⚡ Iniciando motor de WhatsApp...');
    
    client = new Client({
        authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
        puppeteer: { 
            headless: true, 
            args: [
                '--no-sandbox', 
                '--disable-setuid-sandbox', 
                '--disable-extensions',
                '--disable-gpu'
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
    });

    client.on('disconnected', async (reason) => {
        console.log('❌ CLIENTE DESCONECTADO:', reason);
        await detenerMotor();
    });

    // --- EVENTOS DE MENSAJERÍA (CORE DEL BOT) ---
    client.on('message', async (msg) => {
        // Ignorar grupos y estados para ahorrar recursos
        if(msg.from.includes('@g.us') || msg.isStatus) return;

        try {
            let contenido = msg.body;
            let mediaUrl = null;

            // 1. Manejo de Ubicación
            if (msg.type === 'location') {
                const { latitude: lat, longitude: lng } = msg.location;
                contenido = `📍 Ubicación: http://maps.google.com/maps?q=${lat},${lng}`;
            }

            // 2. Manejo de Multimedia (Imágenes de tickets, etc.)
            if (msg.hasMedia) {
                const media = await msg.downloadMedia();
                if (media) {
                    let ext = mime.extension(media.mimetype) || 'bin';
                    const fileName = `${msg.type}_${Date.now()}.${ext}`;
                    const filePath = path.join(UPLOADS_DIR, fileName);
                    
                    fs.writeFileSync(filePath, media.data, { encoding: 'base64' });
                    mediaUrl = `${PUBLIC_URL}/uploads/${fileName}`;

                    // Etiquetamos el contenido para que Python sepa qué hacer
                    if (msg.type === 'image') {
                        contenido = `[FOTO_COMPROBANTE]`;
                    } else if (msg.type === 'audio' || msg.type === 'ptt') {
                        contenido = `[AUDIO]`;
                    } else {
                        contenido = `[ARCHIVO]`;
                    }
                }
            }

            // 🔥 3. DESENMASCARAR EL LID: Obtener el número real del contacto
            let numeroReal = msg.from;
            try {
                const contact = await msg.getContact();
                if (contact && contact.number) {
                    numeroReal = `${contact.number}@c.us`; // Número real extraído
                }
            } catch (err) {
                console.log("⚠️ No se pudo obtener el número real del contacto:", err.message);
            }

            // 4. ENVIAR AL WEBHOOK DE FASTAPI
            await axios.post(`${BACKEND_URL}/whatsapp/webhook/recibir`, { 
                telefono: numeroReal,       // Para buscar en la BD de Python (Ej: 5219614708391@c.us)
                telefono_raw: msg.from,     // Para enviarle mensajes de vuelta (Ej: 59889191751761@lid o @c.us)
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
        detenerMotor();
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
    lastQR = null;
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

// ENVIAR MENSAJE (Desde Python -> Usuario)
app.post('/enviar-mensaje', async (req, res) => {
    const { numero, mensaje, ruta } = req.body; 

    if (!client || !isReady) return res.status(503).json({ error: 'WhatsApp no conectado' });

    try {
        // 🔥 CORRECCIÓN: Si ya trae @lid o @c.us, úsalo. Si no, agrégale @c.us
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
});