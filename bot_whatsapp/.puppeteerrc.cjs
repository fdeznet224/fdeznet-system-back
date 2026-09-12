const path = require('path');

module.exports = {
  // La caché pertenece a la aplicación y no al usuario que ejecutó npm. Así
  // Chromium sigue disponible cuando el servicio corre sin privilegios.
  cacheDirectory: path.join(__dirname, '.cache', 'puppeteer'),
};
