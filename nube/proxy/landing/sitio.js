// Registro de demos: manda el formulario al panel (/api/registro, mismo dominio).
document.addEventListener('DOMContentLoaded', () => {
  const anio = document.getElementById('anio');
  if (anio) anio.textContent = String(new Date().getFullYear());

  const formulario = document.getElementById('formulario');
  const listo = document.getElementById('listo');
  if (!formulario || !listo) return;
  const error = formulario.querySelector('.error');
  const boton = formulario.querySelector('button[type="submit"]');

  const mostrarError = (texto) => {
    error.textContent = texto;
    error.hidden = false;
  };

  formulario.addEventListener('submit', async (evento) => {
    evento.preventDefault();
    error.hidden = true;
    const datos = Object.fromEntries(new FormData(formulario).entries());
    if (!datos.empresa.trim() || !datos.nombre.trim()) return mostrarError('Escribe el nombre de tu empresa y el tuyo.');
    if (datos.telefono.replace(/\D/g, '').length < 10) return mostrarError('Escribe tu WhatsApp a 10 dígitos.');

    boton.disabled = true;
    boton.textContent = 'Creando tu demo…';
    try {
      const respuesta = await fetch('/api/registro', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(datos),
      });
      const resultado = await respuesta.json().catch(() => ({}));
      if (!respuesta.ok || !resultado.ok) throw new Error(resultado.error || 'No se pudo enviar; intenta de nuevo.');
      formulario.hidden = true;
      listo.hidden = false;
      listo.querySelector('.listo-texto').textContent = resultado.mensaje;
      if (resultado.direccion) {
        const enlace = listo.querySelector('.listo-direccion a');
        enlace.href = resultado.direccion;
        enlace.textContent = resultado.direccion.replace('https://', '');
        listo.querySelector('.listo-direccion').hidden = false;
      }
    } catch (fallo) {
      mostrarError(fallo.message || 'No se pudo enviar; intenta de nuevo.');
      boton.disabled = false;
      boton.textContent = 'Crear mi demo gratis';
    }
  });
});
