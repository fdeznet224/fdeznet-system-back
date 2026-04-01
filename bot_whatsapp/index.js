require('dotenv').config(); // ✅ Carga el archivo .env primero que nada

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const axios = require('axios');
const fs = require('fs');
const path = require('path');

// --- VARIABLES DE ENTORNO ---
const PORT = process.env.PORT || 3000;
const PUBLIC_URL = process.env.PUBLIC_URL || `http://localhost:${PORT}`;
const BACKEND_URL = process.env.API_BACKEND_URL || 'http://127.0.0.1:8000';

const app = express();
app.use(express.json());

// --- CONFIGURACIÓN DE CARPETA DE IMÁGENES ---
const UPLOADS_DIR = path.join(__dirname, 'uploads');
if (!fs.existsSync(UPLOADS_DIR)) {
    fs.mkdirSync(UPLOADS_DIR);
}
app.use('/uploads', express.static(UPLOADS_DIR));

const client = new Client({
    authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
    puppeteer: { headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox'] }
});

let isReady = false;
let lastQR = null;

// --- EVENTOS DEL CLIENTE ---
client.on('qr', (qr) => {
    lastQR = qr;
    console.log('📢 NUEVO CÓDIGO QR GENERADO:');
    qrcode.generate(qr, { small: true });
});

client.on('ready', () => {
    isReady = true;
    lastQR = null;
    console.log('✅ ¡WHATSAPP WEB LISTO!');
});

client.on('disconnected', () => {
    isReady = false;
    client.initialize();
});

// ESCUCHAR MENSAJES ENTRANTES
client.on('message', async (msg) => {
    if(msg.from.includes('@g.us') || msg.isStatus) return;

    try {
        let contenido = msg.body;

        // 📍 DETECTAR UBICACIÓN (Corregido sintaxis ${lat} y link estándar)
        if (msg.type === 'location') {
            const lat = msg.location.latitude;
            const lng = msg.location.longitude;
            contenido = `📍 Ubicación: https://maps.google.com/?q=${lat},${lng}`;
        }

        // 🖼️ DETECTAR IMAGEN / FOTO
        if (msg.hasMedia && (msg.type === 'image' || msg.type === 'sticker')) {
            const media = await msg.downloadMedia();
            if (media) {
                const fileName = `img_${Date.now()}.jpg`;
                const filePath = path.join(UPLOADS_DIR, fileName);

                fs.writeFileSync(filePath, media.data, { encoding: 'base64' });

                // ✅ Usa la URL del .env dinámicamente
                const fileUrl = `${PUBLIC_URL}/uploads/${fileName}`;
                contenido = `[IMAGE]${fileUrl}`;
                
                console.log(`📸 Imagen guardada: ${fileName}`);
            }
        }

        // ✅ Llama al backend dinámico
        await axios.post(`${BACKEND_URL}/whatsapp/webhook/recibir`, {
            telefono: msg.from,
            mensaje: contenido 
        });
    } catch (error) { 
        console.error("❌ Error procesando mensaje multimedia:", error.message); 
    }
});

// ESCUCHAR LECTURA DE MENSAJES (PALOMITAS)
client.on('message_ack', async (msg, ack) => {
    try {
        // ✅ Llama al backend dinámico
        await axios.post(`${BACKEND_URL}/whatsapp/webhook/ack`, {
            wa_id: msg.id.id,
            ack: ack 
        });
    } catch (error) { 
        console.error("❌ Error enviando ACK a Python:", error.message); 
    }
});

// --- ENDPOINTS ---
app.get('/status', (req, res) => res.json({ connected: isReady, qr: lastQR }));

app.post('/enviar-mensaje', async (req, res) => {
    const { numero, mensaje } = req.body;
    if (!isReady) return res.status(503).json({ error: 'No conectado' });
    try {
        const chatId = `${numero}@c.us`;
        const response = await client.sendMessage(chatId, mensaje);
        res.json({ status: 'sent', wa_id: response.id.id });
    } catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/logout', async (req, res) => {
    try { await client.logout(); isReady = false; res.json({ status: 'logged_out' }); } 
    catch (e) { res.status(500).json({ error: e.message }); }
});

client.initialize();

// ✅ Inicia el servidor usando el puerto del .env
app.listen(PORT, () => console.log(`🚀 Puente WWebJS en puerto ${PORT} con soporte de imágenes`));