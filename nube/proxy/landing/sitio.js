// Formularios de la página: demo de WisPay (/api/registro) y cotización de
// WisPay Fichas (/api/interes). Los atiende el panel en este mismo dominio.
document.addEventListener('DOMContentLoaded', () => {
  const anio = document.getElementById('anio');
  if (anio) anio.textContent = String(new Date().getFullYear());

  conectar('formulario', '/api/registro', document.getElementById('listo'), 'Crear mi demo gratis', 'Creando tu demo…');
  const fichas = document.getElementById('formulario-fichas');
  if (fichas) conectar('formulario-fichas', '/api/interes', fichas.nextElementSibling, 'Quiero mi cotización', 'Enviando…');
});

function conectar(id, url, listo, textoBoton, textoEnviando) {
  const formulario = document.getElementById(id);
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
    boton.textContent = textoEnviando;
    try {
      const respuesta = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(datos),
      });
      const resultado = await respuesta.json().catch(() => ({}));
      if (!respuesta.ok || !resultado.ok) throw new Error(resultado.error || 'No se pudo enviar; intenta de nuevo.');
      formulario.hidden = true;
      listo.hidden = false;
      listo.querySelector('.listo-texto').textContent = resultado.mensaje;
      const direccion = listo.querySelector('.listo-direccion');
      if (direccion && resultado.direccion) {
        const enlace = direccion.querySelector('a');
        enlace.href = resultado.direccion;
        enlace.textContent = resultado.direccion.replace('https://', '');
        direccion.hidden = false;
      }
    } catch (fallo) {
      mostrarError(fallo.message || 'No se pudo enviar; intenta de nuevo.');
      boton.disabled = false;
      boton.textContent = textoBoton;
    }
  });
}
