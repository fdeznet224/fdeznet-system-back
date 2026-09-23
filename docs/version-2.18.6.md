# Versión 2.18.6 — Reconexión VPN y monitoreo tolerante a cortes

Fecha de liberación: 23 de septiembre de 2026.

- Backend: `eac1f656a5f93aff00a3112eccb23959c600f2db`
- Frontend: `5d091b91716fd4668fb6a519ee80755fcdce904c` (sin cambios)

## Objetivo

Con internet inestable en el sitio del MikroTik, el panel mostraba routers que
se conectaban y desconectaban, con alertas de WhatsApp ONLINE/OFFLINE cada
minuto, y algunos túneles WireGuard no se recuperaban solos después de un corte.
Esta versión corrige ambos problemas.

## Diagnóstico que originó el cambio

Se revisó la VPS central (`fdezpay.com`) el 22–23 de septiembre de 2026:

- La VPS estaba sana: `wg0` llevaba 21 días arriba, sin errores UDP y sin
  cambios de configuración.
- La MTU **no** era la causa: paquetes de 1420 bytes pasan completos por el
  túnel.
- Las fallas venían del enlace de cada router. Un router con enlace saturado
  entregaba respuestas con segundos de retraso, que el firewall de la VPS
  bloqueaba porque llegaban cuando la conexión ya se había cerrado.
- Otros routers dejaron de negociar el túnel durante horas aunque el internet
  de su sitio funcionaba: otro router detrás de la misma IP pública seguía
  conectado. La VPS enviaba handshakes al puerto NAT anterior y el MikroTik
  nunca iniciaba uno nuevo. Esto corresponde a una conexión UDP obsoleta en el
  connection tracking de RouterOS después del corte.
- El monitoreo hacía un solo `GET /system/identity` por minuto, y una sola
  pérdida bastaba para marcar el router OFFLINE y enviar una alerta.

## Vigilante de reconexión en MikroTik

El script que genera **Configuración → Túneles VPN** al crear una conexión de
router agrega al final un bloque llamado `fdeznet-vpn-watchdog`:

- Un `/system script` hace `ping` al gateway VPN (`10.8.0.1`) con 3 intentos.
- Si no responde ninguno:
  - borra de `/ip firewall connection` la conexión UDP hacia
    `IP_VPS:puerto_WireGuard`;
  - desactiva y vuelve a activar el peer de `wg-fdeznet`;
  - registra `FdezNet VPN: tunel reiniciado por el vigilante` en el log.
- Un `/system scheduler` lo ejecuta al arrancar y cada 2 minutos.
- El bloque borra primero el script y el scheduler con ese nombre, así que se
  puede volver a pegar sin duplicarlos.

Los túneles creados antes de esta versión no se modifican en la base de datos.
Al listarlos, `GET /vpn/tunnels/` agrega el bloque al final del script con el
endpoint, el puerto y el gateway que ya tenía ese script. En los routers ya
instalados solo se pega el bloque que empieza con
`# --- Vigilante de reconexion VPN`, porque el resto ya está aplicado. Las
configuraciones de técnicos (archivo `.conf` con `[Interface]`) no reciben el
vigilante.

Código: `construir_vigilante_mikrotik` y `asegurar_vigilante` en
`src/application/services/vpn_service.py`.

## Monitoreo de routers

`tarea_monitoreo_routers` (`src/jobs.py`) usa las reglas de
`src/application/services/router_monitor_service.py`:

- Un router se marca **OFFLINE** después de 3 fallas seguidas
  (`FALLAS_PARA_OFFLINE`), aproximadamente 3 minutos.
- Una sola respuesta lo vuelve a marcar **ONLINE** y reinicia el conteo.
- El conteo vive en memoria del proceso. Si se reinicia el API, solo se retrasa
  unos minutos la siguiente alerta.
- En cada ciclo se lee `sudo wg show wg0 dump` (`leer_handshakes_wireguard`) y
  la alerta de caída agrega el motivo según el último handshake del peer:
  - más de 180 s (`HANDSHAKE_VIGENTE_SEGUNDOS`): *Túnel VPN caído (último
    contacto hace X min).*
  - reciente: *El túnel VPN sigue activo, pero el MikroTik no responde a la
    API (enlace lento o saturado).*
  - nunca conectado: *El túnel VPN nunca se ha conectado.*
- Si WireGuard no está disponible, la alerta se envía sin ese detalle.

La lectura de handshakes usa el permiso de sudoers que ya otorga `install.sh`
al usuario del servicio (`/usr/bin/wg`).

## Pruebas

- `tests/test_vpn_service.py`: contenido del vigilante, que no se duplique en
  scripts anteriores, que se excluyan los `.conf` de técnicos y el parseo de
  `wg show dump`.
- `tests/test_router_monitor_service.py`: umbral de fallas, recuperación
  inmediata y mensajes según el handshake.
- Resultado: 280 pruebas de backend aprobadas.

## Despliegue en la central

1. Respaldo cifrado verificado:
   `/var/backups/fdeznet/20260923-004838.tar.gz.gpg`.
2. `git reset --hard eac1f65` en `/opt/fdeznet/backend`, `pip install -r
   requirements.txt` y `alembic upgrade head`. No hubo migraciones.
3. Reinicio de `fdeznet-api` y verificación local y pública de
   `/api/health/ready`.
4. Actualización de `FDEZNET_RELEASE_VERSION` y `FDEZNET_BACKEND_COMMIT` en
   `.env`.
5. Registro de la versión 2.18.6 como activa en **Licencias y versiones**. Al
   momento de este documento no se había asignado a las instalaciones de
   clientes.

## Pendientes operativos

- Pegar el bloque del vigilante en los MikroTik ya instalados, empezando por
  los peers `10.8.0.4` y `10.8.0.6`, que seguían sin handshake al momento de
  la liberación.
- Confirmar si el peer `10.8.0.5` sigue en uso; llevaba 6.6 días sin conectar.
- La VPS recibe intentos de fuerza bruta por SSH contra `root`. Se recomienda
  dejar solo el acceso con llave o instalar fail2ban.
