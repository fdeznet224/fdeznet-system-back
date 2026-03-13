const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const axios = require('axios');
const fs = require('fs'); // ✅ Para manejar archivos
const path = require('path'); // ✅ Para rutas de carpetas

const app = express();
app.use(express.json());

// --- CONFIGURACIÓN DE CARPETA DE IMÁGENES ---
const UPLOADS_DIR = path.join(__dirname, 'uploads');
if (!fs.existsSync(UPLOADS_DIR)) {
    fs.mkdirSync(UPLOADS_DIR);
}
// ✅ Servir la carpeta para que las imágenes sean accesibles vía URL
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

        // 📍 DETECTAR UBICACIÓN (Formato mejorado para el Front)
        if (msg.type === 'location') {
            const lat = msg.location.latitude;
            const lng = msg.location.longitude;
            contenido = `📍 Ubicación: https://www.google.com/maps?q=${lat},${lng}`;
        }

        // 🖼️ DETECTAR IMAGEN / FOTO (Guardado físico)
        if (msg.hasMedia && (msg.type === 'image' || msg.type === 'sticker')) {
            const media = await msg.downloadMedia();
            if (media) {
                // Generar nombre único: img_17000000.jpg
                const fileName = `img_${Date.now()}.jpg`;
                const filePath = path.join(UPLOADS_DIR, fileName);

                // ✅ Guardar archivo en disco
                fs.writeFileSync(filePath, media.data, { encoding: 'base64' });

                // ✅ URL pública (Usa tu IP o dominio en lugar de localhost si accedes desde fuera)
                // Ejemplo: http://192.168.1.50:3000/uploads/img_...
                const fileUrl = `http://localhost:3000/uploads/${fileName}`;
                contenido = `[IMAGE]${fileUrl}`;
                
                console.log(`📸 Imagen guardada: ${fileName}`);
            }
        }

        await axios.post('http://127.0.0.1:8000/whatsapp/webhook/recibir', {
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
        await axios.post('http://127.0.0.1:8000/whatsapp/webhook/ack', {
            wa_id: msg.id.id,
            ack: ack 
        });
    } catch (error) { console.error("❌ Error enviando ACK a Python:", error.message); }
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
app.listen(3000, () => console.log('🚀 Puente WWebJS en puerto 3000 con soporte de imágenes'));