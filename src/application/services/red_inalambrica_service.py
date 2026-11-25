"""Red inalámbrica: AP, PtP y demás equipos entre el MikroTik y las antenas de los clientes.

- Ping desde su MikroTik cada 10 minutos (igual que las antenas de los clientes).
- En los Ubiquiti se lee airOS (señal, ruido, CCQ, capacidad, estaciones). Con la
  lista de estaciones del AP se sabe qué clientes están en cada AP y con qué señal.
- Al equipo se llega directo por la VPN o, si no, por un puente temporal en su
  MikroTik (dst-nat de un puerto de su IP de la VPN al equipo), como las antenas.
"""

import asyncio
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.acceso_remoto_service import RUTAS_PUENTE, _responde, reglas_puente, url_de
from src.application.services.antena_service import PINGS, resumir_ping
from src.infrastructure.mikrotik_service import MikroTikService
from src.infrastructure.models import ClienteModel, EquipoRedModel, LecturaOpticaModel, OLTModel, RouterModel

logger = logging.getLogger(__name__)

TIPOS = {
    "router": "MikroTik / router",
    "ptp_base": "PtP (base)",
    "ptp_estacion": "PtP (estación)",
    "ap": "AP / sectorial",
    "switch": "Switch",
    "otro": "Otro",
}
MARCAS = {"ubiquiti": "Ubiquiti", "mimosa": "Mimosa", "mikrotik": "MikroTik", "otra": "Otra"}
PUENTE_EQUIPO = "FDEZNET-EQUIPO"
PUENTE_EQUIPO_BASE = 19000
PUENTE_EQUIPO_MINUTOS = 30
FORMATO_EXPIRA = "%Y-%m-%dT%H:%M"
ESPERA_AIROS = 12.0
# Ping rápido cada minuto: caído hasta fallar 2 veces seguidas (un ping perdido no es falla).
PINGS_RAPIDOS = 2
FALLAS_PARA_CAIDO = 2
SIMULTANEOS = 10


def registrar_ping(obj: Any, responde: bool, latencia: Optional[float], ahora: datetime, olt: bool = False) -> Optional[str]:
    """Guarda el ping y dice si el equipo "cayo" o "regreso" (o nada)."""
    campo_resp, campo_lat, campo_fecha = ("ping_responde", "ping_latencia_ms", "ping_revisado_en") if olt else ("responde", "latencia_ms", "revisado_en")
    setattr(obj, campo_fecha, ahora)
    if responde:
        obj.fallas_seguidas = 0
        setattr(obj, campo_resp, True)
        setattr(obj, campo_lat, latencia)
        if obj.caido_desde is not None:
            obj.caido_desde = None
            return "regreso"
        return None
    obj.fallas_seguidas = (obj.fallas_seguidas or 0) + 1
    if obj.fallas_seguidas >= FALLAS_PARA_CAIDO:
        setattr(obj, campo_resp, False)
        setattr(obj, campo_lat, None)
        if obj.caido_desde is None:
            obj.caido_desde = ahora
            return "cayo"
    return None


def _clientes_debajo(nodo: Dict[str, Any]) -> int:
    """Clientes que dependen de un nodo del árbol (radio, fibra y lo de su MikroTik)."""
    total = len(nodo.get("clientes") or []) + len(nodo.get("clientes_sin_ap") or [])
    total += sum(len(p["clientes"]) for o in nodo.get("olts") or [] for p in o.get("pons", []))
    for hijo in (nodo.get("hijos") or []) + (nodo.get("equipos") or []):
        total += _clientes_debajo(hijo)
    if nodo.get("router"):
        total += _clientes_debajo(nodo["router"])
    return total


def puntos_de_falla(arbol: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Equipos u OLT caídos cuyo equipo de arriba sí responde: ahí está la falla
    (corte de luz o equipo dañado); lo de abajo cae por consecuencia."""
    fallas: List[Dict[str, Any]] = []

    def visitar(nodo: Dict[str, Any], tipo: str, arriba_caido: bool) -> None:
        caido = nodo.get("responde") is False
        if caido and not arriba_caido and nodo.get("caido_desde"):
            fallas.append({
                "clave": f"{'o' if tipo == 'olt' else 'e'}{nodo['id']}", "tipo": tipo, "id": nodo["id"],
                "nombre": nodo["nombre"] if tipo != "olt" else f"OLT {nodo['nombre']}", "ip": nodo.get("ip"),
                "tipo_nombre": nodo.get("tipo_nombre") or "OLT", "desde": nodo["caido_desde"],
                "clientes_afectados": _clientes_debajo(nodo) if tipo != "olt" else sum(len(p["clientes"]) for p in nodo.get("pons", [])),
            })
        abajo = arriba_caido or caido
        for hijo in nodo.get("hijos") or []:
            visitar(hijo, "equipo", abajo)
        for o in nodo.get("olts") or []:
            visitar(o, "olt", abajo)
        if nodo.get("router"):
            visitar_router(nodo["router"], abajo)

    def visitar_router(router: Dict[str, Any], arriba_caido: bool) -> None:
        abajo = arriba_caido or router.get("en_linea") is False
        for e in router.get("equipos") or []:
            visitar(e, "equipo", abajo)
        for o in router.get("olts") or []:
            visitar(o, "olt", abajo)

    for raiz in arbol.get("troncal") or []:
        visitar(raiz, "equipo", False)
    for router in arbol.get("routers") or []:
        visitar_router(router, False)
    return fallas


# ------------------------------------------------------------------ airOS
def _numero(valor: Any) -> Optional[float]:
    try:
        return float(re.sub(r"[^\d.\-]", "", str(valor))) if valor not in (None, "") else None
    except ValueError:
        return None


def resumir_status(datos: Dict[str, Any]) -> Dict[str, Any]:
    """status.cgi de airOS 6 y 8: lo que se muestra del equipo."""
    host = datos.get("host") or {}
    wifi = datos.get("wireless") or {}
    polling = wifi.get("polling") or {}
    # airOS 8 trae la capacidad airMAX del enlace en el primer "sta" cuando es estación.
    sta = (wifi.get("sta") or [{}])[0] if isinstance(wifi.get("sta"), list) and wifi.get("sta") else {}
    airmax = sta.get("airmax") or {}
    frecuencia = wifi.get("frequency")
    return {
        "senal": _numero(wifi.get("signal")),
        "ruido": _numero(wifi.get("noisef")),
        "ccq": _numero(wifi.get("ccq")),
        "capacidad_tx": _numero(polling.get("dl_capacity") or airmax.get("dl_capacity") or polling.get("capacity")),
        "capacidad_rx": _numero(polling.get("ul_capacity") or airmax.get("ul_capacity")),
        "frecuencia": (f"{frecuencia} MHz" if isinstance(frecuencia, (int, float)) else str(frecuencia))[:30] if frecuencia else None,
        "ancho_canal": (str(wifi.get("chanbw") or wifi.get("chwidth") or "") or None),
        "ssid": wifi.get("essid"),
        "modo": wifi.get("mode"),
        "estaciones": int(wifi.get("count")) if str(wifi.get("count") or "").isdigit() else None,
        "firmware": host.get("fwversion"),
        "modelo": host.get("devmodel"),
        "nombre_equipo": host.get("hostname"),
        "uptime_s": int(host.get("uptime")) if str(host.get("uptime") or "").isdigit() else None,
    }


def resumir_estaciones(lista: Any) -> List[Dict[str, Any]]:
    """sta.cgi: cada antena conectada al AP (MAC, IP, señal, nombre)."""
    estaciones = []
    for sta in lista if isinstance(lista, list) else []:
        if not isinstance(sta, dict):
            continue
        remoto = sta.get("remote") or {}
        estaciones.append({
            "mac": str(sta.get("mac") or "").upper() or None,
            "ip": sta.get("lastip") if sta.get("lastip") not in (None, "", "0.0.0.0") else None,
            "senal": _numero(sta.get("signal")),
            "ccq": _numero(sta.get("ccq")),
            "nombre": remoto.get("hostname") or sta.get("name") or None,
        })
    return estaciones


async def leer_airos(base: str, usuario: str, clave: str, http: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """Entra a airOS (8: /api/auth; 6: /login.cgi) y lee status.cgi y sta.cgi."""
    cliente = http or httpx.AsyncClient(verify=False, timeout=ESPERA_AIROS, follow_redirects=True)
    try:
        cabeceras: Dict[str, str] = {}
        respuesta = await cliente.post(f"{base}/api/auth", data={"username": usuario, "password": clave})
        if respuesta.status_code == 200:
            if respuesta.headers.get("x-csrf-id"):
                cabeceras["X-CSRF-ID"] = respuesta.headers["x-csrf-id"]
        else:
            # airOS 6: primero se pide la sesión y luego se entra.
            await cliente.get(f"{base}/login.cgi")
            respuesta = await cliente.post(
                f"{base}/login.cgi", files={"uri": (None, "/status.cgi"), "username": (None, usuario), "password": (None, clave)},
            )
        status = await cliente.get(f"{base}/status.cgi", headers=cabeceras)
        if status.status_code in (401, 403) or "json" not in status.headers.get("content-type", "") and not status.text.lstrip().startswith("{"):
            raise PermissionError("Usuario o clave de airOS incorrectos")
        datos = status.json()
        # airOS 8 trae las estaciones dentro de status.cgi; airOS 6 en sta.cgi.
        lista = (datos.get("wireless") or {}).get("sta")
        if not isinstance(lista, list):
            estaciones = await cliente.get(f"{base}/sta.cgi", headers=cabeceras)
            lista = estaciones.json() if estaciones.status_code == 200 and estaciones.text.lstrip().startswith("[") else []
        return {"status": resumir_status(datos), "estaciones": resumir_estaciones(lista)}
    finally:
        if http is None:
            await cliente.aclose()


def ancestros(equipo_id: int, padres: Dict[int, Optional[int]]) -> set:
    """Los equipos de los que cuelga (hacia arriba), sin quedarse dando vueltas si hay un círculo."""
    vistos: set = set()
    actual = padres.get(equipo_id)
    while actual is not None and actual not in vistos:
        vistos.add(actual)
        actual = padres.get(actual)
    return vistos


def nombre_comparable(texto: Any) -> str:
    """Sin acentos, mayúsculas ni espacios de más: "Ximena Zuñiga " == "ximena zuniga"."""
    plano = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return " ".join(plano.lower().split())


def mismo_nombre(equipo: Any, cliente: Any) -> bool:
    """El nombre de la antena es el del cliente (o uno contiene al otro, con 2+ palabras)."""
    a, b = nombre_comparable(equipo), nombre_comparable(cliente)
    if not a or not b:
        return False
    if a == b:
        return True
    corto, largo = sorted((a, b), key=len)
    return len(corto.split()) >= 2 and f" {corto} " in f" {largo} "


# ------------------------------------------------------------------ puente
def comentario_equipo(equipo_id: int, expira: datetime) -> str:
    return f"{PUENTE_EQUIPO} {equipo_id} {expira.strftime(FORMATO_EXPIRA)}"


def _partes(comentario: Any) -> List[str]:
    partes = str(comentario or "").split()
    return partes if len(partes) == 3 and partes[0] == PUENTE_EQUIPO else []


def puente_equipo_vencido(comentario: Any, ahora: datetime) -> bool:
    partes = _partes(comentario)
    if not partes:
        return False
    try:
        return datetime.strptime(partes[2], FORMATO_EXPIRA) <= ahora
    except ValueError:
        return True


def _crear_puente(mk: MikroTikService, equipo_id: int, reglas: Dict[str, dict]) -> None:
    for ruta in RUTAS_PUENTE:
        for regla in mk._request("GET", ruta, raise_on_error=True) or []:
            partes = _partes(regla.get("comment"))
            if partes and partes[1] == str(equipo_id):
                mk._request("DELETE", f"{ruta}/{regla['.id']}", raise_on_error=True)
    mk._request("PUT", "/ip/firewall/nat", reglas["dstnat"], raise_on_error=True)
    mk._request("PUT", "/ip/firewall/nat", reglas["srcnat"], raise_on_error=True)
    creada = mk._request("PUT", "/ip/firewall/filter", reglas["forward"], raise_on_error=True)
    if isinstance(creada, dict) and creada.get(".id"):
        mk._mover_al_inicio("/ip/firewall/filter", "forward", [creada[".id"]])


def _cerrar_vencidos(mk: MikroTikService, ahora: datetime) -> int:
    cerradas = 0
    for ruta in RUTAS_PUENTE:
        for regla in mk._request("GET", ruta, raise_on_error=True) or []:
            if puente_equipo_vencido(regla.get("comment"), ahora):
                mk._request("DELETE", f"{ruta}/{regla['.id']}", raise_on_error=True)
                cerradas += 1
    return cerradas


def _mk(router: RouterModel) -> MikroTikService:
    return MikroTikService(router.ip_vpn, router.user_api, router.pass_api, router.port_api)


# ------------------------------------------------------------------ servicio
class RedInalambricaService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _router(self, equipo: EquipoRedModel) -> RouterModel:
        router = await self.db.get(RouterModel, equipo.router_id) if equipo.router_id else None
        if not router:
            raise ValueError("Elige por cuál MikroTik se llega a este equipo.")
        return router

    async def direccion_web(self, equipo: EquipoRedModel, minutos: int = PUENTE_EQUIPO_MINUTOS) -> Dict[str, Any]:
        """Directo si el servidor llega por la VPN; si no, puente en su MikroTik."""
        puerto = str(equipo.puerto_web or "443")
        if await _responde(equipo.ip, puerto):
            return {"url": url_de(equipo.ip, puerto), "puente": False, "expira": None}
        router = await self._router(equipo)
        puerto_puente = PUENTE_EQUIPO_BASE + equipo.id % 1000
        expira = datetime.now() + timedelta(minutes=minutos)
        reglas = reglas_puente(router.ip_vpn, puerto_puente, equipo.ip, puerto, comentario_equipo(equipo.id, expira))
        await asyncio.to_thread(_crear_puente, _mk(router), equipo.id, reglas)
        return {
            "url": url_de(router.ip_vpn, str(puerto_puente), https=puerto == "443"),
            "puente": True,
            "expira": expira.isoformat(timespec="minutes"),
        }

    async def abrir(self, equipo_id: int) -> Dict[str, Any]:
        equipo = await self._equipo(equipo_id)
        return {**await self.direccion_web(equipo), "router": (await self._router(equipo)).nombre}

    async def _equipo(self, equipo_id: int) -> EquipoRedModel:
        equipo = await self.db.get(EquipoRedModel, equipo_id)
        if not equipo:
            raise ValueError("Equipo no encontrado.")
        return equipo

    # --------------------------------------------------------- revisar
    async def _ping(self, equipo: EquipoRedModel, router: RouterModel) -> None:
        respuestas = await asyncio.to_thread(
            _mk(router)._request, "POST", "/ping", {"address": equipo.ip, "count": str(PINGS)}, True,
        )
        resultado = resumir_ping(respuestas)
        registrar_ping(equipo, resultado["responde"], resultado["latencia_ms"], datetime.now())
        equipo.perdida = resultado["perdida"]

    async def _leer_airos(self, equipo: EquipoRedModel, http: Optional[httpx.AsyncClient] = None) -> None:
        if equipo.marca != "ubiquiti" or not equipo.usuario or not equipo.clave:
            return
        try:
            web = await self.direccion_web(equipo, minutos=15)
            datos = await leer_airos(web["url"], equipo.usuario, equipo.clave, http=http)
        except PermissionError as exc:
            equipo.airos_error = str(exc)
            return
        except Exception as exc:
            equipo.airos_error = f"No se pudo leer airOS: {type(exc).__name__}"[:300]
            logger.info("airOS de %s: %s", equipo.nombre, exc)
            return
        for campo, valor in datos["status"].items():
            if campo == "nombre_equipo":
                continue
            if campo == "modelo":
                equipo.modelo = valor or equipo.modelo
            else:
                setattr(equipo, campo, valor)
        if equipo.estaciones is None:
            equipo.estaciones = len(datos["estaciones"])
        equipo.airos_leido_en = datetime.now()
        equipo.airos_error = None
        await self._vincular_estaciones(equipo, datos["estaciones"])

    async def _vincular_estaciones(self, equipo: EquipoRedModel, estaciones: List[Dict[str, Any]]) -> None:
        """Las antenas conectadas al AP: qué cliente (o equipo) es cada una y su señal."""
        if not estaciones:
            return
        ips = {e["ip"]: e for e in estaciones if e["ip"]}
        macs = {e["mac"]: e for e in estaciones if e["mac"]}
        filtros = [ClienteModel.tipo_conexion == "radio", ClienteModel.estado != "cancelado"]
        if equipo.router_id:
            filtros.append(ClienteModel.router_id == equipo.router_id)
        clientes = (await self.db.execute(select(ClienteModel).where(*filtros))).scalars().all()
        usadas: set = set()
        pendientes = []
        for cliente in clientes:
            sta = ips.get((cliente.ip_antena or "").strip()) or macs.get((cliente.mac_address or "").strip().upper())
            if sta:
                usadas.add(id(sta))
                cliente.equipo_red_id = equipo.id
                cliente.antena_senal = sta["senal"]
            else:
                pendientes.append(cliente)
        # Respaldo: la antena se llama como el cliente (IP 0.0.0.0 o cambiada).
        for cliente in pendientes:
            sta = next((e for e in estaciones if id(e) not in usadas and mismo_nombre(e["nombre"], cliente.nombre)), None)
            if sta:
                usadas.add(id(sta))
                cliente.equipo_red_id = equipo.id
                cliente.antena_senal = sta["senal"]
        # Un PtP estación que se ve en esta base (o AP) cuelga de ella, si no se eligió otro.
        # Nunca al revés: una estación también ve a su base y eso armaría un círculo.
        if equipo.tipo not in ("ptp_base", "ap"):
            return
        todos = (await self.db.execute(select(EquipoRedModel))).scalars().all()
        padres = {e.id: e.padre_id for e in todos}
        for otro in todos:
            if (otro.id != equipo.id and otro.padre_id is None and otro.tipo == "ptp_estacion"
                    and otro.ip in ips and otro.id not in ancestros(equipo.id, padres)):
                otro.padre_id = equipo.id

    async def revisar(self, equipo_id: int) -> Dict[str, Any]:
        equipo = await self._equipo(equipo_id)
        await self._ping(equipo, await self._router(equipo))
        await self._leer_airos(equipo)
        await self.db.commit()
        return self._datos(equipo)

    async def revisar_todos(self) -> Dict[str, int]:
        equipos = (
            await self.db.execute(select(EquipoRedModel).where(EquipoRedModel.activo.is_(True)))
        ).scalars().all()
        routers = {r.id: r for r in (await self.db.execute(select(RouterModel))).scalars().all()}
        conteo = {"revisados": 0, "responden": 0, "errores": 0}
        for equipo in equipos:
            router = routers.get(equipo.router_id)
            if not router:
                continue
            try:
                # El ping lo hace la pasada de cada minuto; aquí solo se lee airOS de los que responden.
                conteo["revisados"] += 1
                conteo["responden"] += int(bool(equipo.responde))
                if equipo.responde:
                    await self._leer_airos(equipo)
                await self.db.commit()
            except Exception as exc:
                # Sin MikroTik no se sabe nada del equipo: no se marca caído.
                await self.db.rollback()
                conteo["errores"] += 1
                logger.warning("No se pudo revisar el equipo %s: %s", equipo.nombre, exc)
        return conteo

    async def ping_rapido(self) -> Dict[str, int]:
        """Cada minuto: ping a cada equipo y OLT desde su MikroTik (todos a la vez) y avisos de falla."""
        equipos = (await self.db.execute(select(EquipoRedModel).where(EquipoRedModel.activo.is_(True)))).scalars().all()
        olts = (await self.db.execute(select(OLTModel).where(OLTModel.is_active.is_(True), OLTModel.router_id.is_not(None)))).scalars().all()
        routers = {r.id: r for r in (await self.db.execute(select(RouterModel))).scalars().all()}
        limite = asyncio.Semaphore(SIMULTANEOS)

        async def pingar(router: RouterModel, ip: str) -> Optional[Dict[str, Any]]:
            async with limite:
                try:
                    respuestas = await asyncio.to_thread(
                        _mk(router)._request, "POST", "/ping", {"address": ip, "count": str(PINGS_RAPIDOS)}, True,
                    )
                except Exception:
                    return None  # sin MikroTik no se sabe nada: no cuenta como falla
                return resumir_ping(respuestas, PINGS_RAPIDOS)

        trabajos = [(e, False) for e in equipos if routers.get(e.router_id)] + [(o, True) for o in olts if routers.get(o.router_id) and o.ip]
        resultados = await asyncio.gather(*(pingar(routers[obj.router_id], obj.ip.strip()) for obj, _ in trabajos))
        ahora = datetime.now()
        conteo = {"revisados": 0, "cayeron": 0, "regresaron": 0}
        regresaron = []
        for (obj, es_olt), resultado in zip(trabajos, resultados):
            if resultado is None:
                continue
            conteo["revisados"] += 1
            if not es_olt:
                obj.perdida = resultado["perdida"]
            desde = obj.caido_desde
            cambio = registrar_ping(obj, resultado["responde"], resultado["latencia_ms"], ahora, olt=es_olt)
            if cambio == "cayo":
                conteo["cayeron"] += 1
            elif cambio == "regreso":
                conteo["regresaron"] += 1
                if obj.alerta_enviada:
                    regresaron.append((obj, desde))
        await self.db.commit()
        await self._avisar(regresaron)
        return conteo

    async def _avisar(self, regresaron: List[tuple]) -> None:
        """WhatsApp a los teléfonos de alerta: el punto de falla (no lo que cae por consecuencia) y cuando regresa."""
        from src.infrastructure.models import ConfiguracionSistema

        config = await self.db.get(ConfiguracionSistema, 1)
        numeros = [n.strip() for n in (getattr(config, "telefonos_alerta", "") or "").split(",") if n.strip()]
        enlace = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
        mensajes: List[str] = []
        for obj, desde in regresaron:
            minutos = int((datetime.now() - desde).total_seconds() // 60) if desde else None
            nombre = f"OLT {obj.nombre}" if isinstance(obj, OLTModel) else obj.nombre
            mensajes.append(f"🟢 *Regresó* {nombre} ({obj.ip}). Ya responde de nuevo"
                            + (f"; estuvo sin responder {minutos} min." if minutos is not None else "."))
            obj.alerta_enviada = False
        fallas = puntos_de_falla(await self.arbol())
        for falla in fallas:
            modelo = OLTModel if falla["tipo"] == "olt" else EquipoRedModel
            obj = await self.db.get(modelo, falla["id"])
            if obj is None or obj.alerta_enviada:
                continue
            obj.alerta_enviada = True
            hora = obj.caido_desde.strftime("%H:%M") if obj.caido_desde else ""
            mensajes.append(
                f"🔴 *Falla en la red*: {falla['tipo_nombre']} *{falla['nombre']}* ({falla['ip']}) no responde desde las {hora}.\n"
                f"{falla['clientes_afectados']} clientes afectados. Puede ser corte de luz o falla del equipo."
                + (f"\nVer: {enlace}/admin/topologia" if enlace else "")
            )
        await self.db.commit()
        if not mensajes or not numeros:
            return
        from src.infrastructure.models import MensajeChatModel
        from src.infrastructure.whatsapp_client import whatsapp_queue

        for texto in mensajes:
            for numero in numeros:
                try:
                    mensaje = MensajeChatModel(
                        cliente_id=None, telefono=numero, direccion="salida", tipo_mensaje="texto",
                        tipo_evento="alerta_red", leido=True, ack=0, estado_envio="pendiente", mensaje=texto,
                    )
                    self.db.add(mensaje)
                    await self.db.commit()
                    await whatsapp_queue.agregar_tarea({"mensaje_chat_id": mensaje.id, "intervalo": 0, "tipo_evento": "alerta_red"})
                except Exception:
                    logger.exception("No se pudo avisar de la red a %s", numero)

    async def cerrar_puentes_vencidos(self) -> int:
        routers = (
            await self.db.execute(
                select(RouterModel).join(EquipoRedModel, EquipoRedModel.router_id == RouterModel.id).distinct()
            )
        ).scalars().all()
        ahora, cerradas = datetime.now(), 0
        for router in routers:
            try:
                cerradas += await asyncio.to_thread(_cerrar_vencidos, _mk(router), ahora)
            except Exception as exc:
                logger.warning("No se pudieron cerrar los puentes a equipos en %s: %s", router.nombre, exc)
        return cerradas

    # --------------------------------------------------------- alta y árbol
    async def guardar(self, datos: Dict[str, Any], equipo_id: Optional[int] = None) -> Dict[str, Any]:
        equipo = await self._equipo(equipo_id) if equipo_id else EquipoRedModel()
        if datos.get("tipo") not in TIPOS:
            raise ValueError("Tipo de equipo no válido.")
        if datos.get("padre_id") and equipo_id:
            if int(datos["padre_id"]) == equipo_id:
                raise ValueError("Un equipo no puede colgar de sí mismo.")
            padres = {e.id: e.padre_id for e in (await self.db.execute(select(EquipoRedModel))).scalars().all()}
            if equipo_id in ancestros(int(datos["padre_id"]), padres):
                raise ValueError("Ese equipo cuelga de este; elige uno que esté más arriba.")
        if datos.get("router_hijo_id") and datos.get("router_hijo_id") == datos.get("router_id") and not datos.get("troncal"):
            raise ValueError("Un equipo de la zona no puede tener colgado a su propio MikroTik; márcalo como troncal.")
        for campo in ("nombre", "tipo", "marca", "modelo", "ip", "router_id", "padre_id", "zona_id", "troncal", "router_hijo_id",
                      "ubicacion", "latitud", "longitud", "usuario", "puerto_web", "activo"):
            if campo in datos:
                setattr(equipo, campo, datos[campo])
        if datos.get("clave"):
            equipo.clave = datos["clave"]
        if equipo_id is None:
            self.db.add(equipo)
        await self.db.commit()
        await self.db.refresh(equipo)
        return self._datos(equipo)

    async def colgar_olt(self, olt_id: int, equipo_id: Optional[int]) -> None:
        """La OLT cuelga de ese equipo (el enlace que le llega); sin equipo, de su MikroTik."""
        olt = await self.db.get(OLTModel, olt_id)
        if not olt:
            raise ValueError("OLT no encontrada.")
        if equipo_id is not None:
            await self._equipo(equipo_id)
        olt.equipo_padre_id = equipo_id
        await self.db.commit()

    async def colgar_router(self, router_id: int, equipo_id: Optional[int]) -> None:
        """El MikroTik cuelga de ese equipo (p. ej. el Mimosa que le llega); sin equipo, va suelto arriba."""
        router = await self.db.get(RouterModel, router_id)
        if not router:
            raise ValueError("MikroTik no encontrado.")
        equipos = (await self.db.execute(select(EquipoRedModel))).scalars().all()
        if equipo_id is not None:
            elegido = next((e for e in equipos if e.id == equipo_id), None)
            if elegido is None:
                raise ValueError("Equipo no encontrado.")
            # Lo que está dentro de este MikroTik (no troncal, o colgando de algo suyo) no puede ser su padre.
            padres = {e.id: e.padre_id for e in equipos}
            dentro = {e.id for e in equipos if e.router_id == router_id and not e.troncal}
            if elegido.id in dentro or ancestros(elegido.id, padres) & dentro:
                raise ValueError("Ese equipo está dentro de este MikroTik; elige uno que le llegue desde arriba.")
        for e in equipos:
            if e.router_hijo_id == router_id:
                e.router_hijo_id = None
        if equipo_id is not None:
            next(e for e in equipos if e.id == equipo_id).router_hijo_id = router_id
        await self.db.commit()

    async def borrar(self, equipo_id: int) -> None:
        equipo = await self._equipo(equipo_id)
        for hijo in (await self.db.execute(select(EquipoRedModel).where(EquipoRedModel.padre_id == equipo_id))).scalars().all():
            hijo.padre_id = equipo.padre_id
        for cliente in (await self.db.execute(select(ClienteModel).where(ClienteModel.equipo_red_id == equipo_id))).scalars().all():
            cliente.equipo_red_id = None
        await self.db.delete(equipo)
        await self.db.commit()

    @staticmethod
    def _datos(equipo: EquipoRedModel) -> Dict[str, Any]:
        return {
            "id": equipo.id, "nombre": equipo.nombre, "tipo": equipo.tipo, "tipo_nombre": TIPOS.get(equipo.tipo, equipo.tipo),
            "marca": equipo.marca, "modelo": equipo.modelo, "ip": equipo.ip, "router_id": equipo.router_id,
            "padre_id": equipo.padre_id, "zona_id": equipo.zona_id, "ubicacion": equipo.ubicacion,
            "troncal": bool(equipo.troncal), "router_hijo_id": equipo.router_hijo_id,
            "latitud": equipo.latitud, "longitud": equipo.longitud, "usuario": equipo.usuario,
            "tiene_clave": bool(equipo.clave), "puerto_web": equipo.puerto_web, "activo": bool(equipo.activo),
            "responde": equipo.responde, "latencia_ms": equipo.latencia_ms, "perdida": equipo.perdida,
            "revisado_en": equipo.revisado_en.isoformat() if equipo.revisado_en else None,
            "senal": equipo.senal, "ruido": equipo.ruido, "ccq": equipo.ccq,
            "capacidad_tx": equipo.capacidad_tx, "capacidad_rx": equipo.capacidad_rx,
            "frecuencia": equipo.frecuencia, "ancho_canal": equipo.ancho_canal, "ssid": equipo.ssid,
            "modo": equipo.modo, "estaciones": equipo.estaciones, "firmware": equipo.firmware,
            "uptime_s": equipo.uptime_s, "airos_error": equipo.airos_error,
            "caido_desde": equipo.caido_desde.isoformat(timespec="minutes") if getattr(equipo, "caido_desde", None) else None,
            "airos_leido_en": equipo.airos_leido_en.isoformat() if equipo.airos_leido_en else None,
        }

    async def _olts_con_clientes(self) -> List[Dict[str, Any]]:
        """Cada OLT con sus clientes de fibra agrupados por PON (de la última lectura óptica)."""
        olts = (await self.db.execute(select(OLTModel).where(OLTModel.is_active.is_(True)).order_by(OLTModel.nombre))).scalars().all()
        if not olts:
            return []
        clientes = (
            await self.db.execute(
                select(ClienteModel).where(
                    ClienteModel.olt_id.in_([o.id for o in olts]), ClienteModel.estado != "cancelado",
                    ClienteModel.tipo_conexion != "radio",
                ).order_by(ClienteModel.nombre)
            )
        ).scalars().all()
        ultima = (
            select(func.max(LecturaOpticaModel.id))
            .where(LecturaOpticaModel.cliente_id.in_([c.id for c in clientes] or [0]))
            .group_by(LecturaOpticaModel.cliente_id)
        )
        lecturas = {
            l.cliente_id: l for l in (
                await self.db.execute(select(LecturaOpticaModel).where(LecturaOpticaModel.id.in_(ultima)))
            ).scalars().all()
        } if clientes else {}
        resultado = []
        for olt in olts:
            pons: Dict[Optional[int], List[Dict[str, Any]]] = {}
            for c in (c for c in clientes if c.olt_id == olt.id):
                lectura = lecturas.get(c.id)
                pons.setdefault(lectura.pon if lectura else None, []).append({
                    "id": c.id, "contrato": c.cedula, "nombre": c.nombre, "estado": c.estado,
                    "responde": bool(c.is_online) if c.is_online is not None else None,
                    "potencia": float(lectura.potencia_rx_dbm) if lectura and lectura.potencia_rx_dbm is not None else None,
                })
            # La OLT está en línea si alguno de sus clientes está conectado; si tiene
            # clientes y ninguno lo está, lo más probable es que la caída sea de la OLT.
            estados = [c["responde"] for lista in pons.values() for c in lista if c["responde"] is not None]
            ping = getattr(olt, "ping_responde", None)
            resultado.append({
                # Primero el ping a su IP de administración; si no hay, lo que dicen sus clientes.
                "responde": ping if ping is not None else (any(estados) if estados else None),
                "latencia_ms": getattr(olt, "ping_latencia_ms", None),
                "caido_desde": olt.caido_desde.isoformat(timespec="minutes") if getattr(olt, "caido_desde", None) else None,
                "id": olt.id, "nombre": olt.nombre, "ip": olt.ip, "tecnologia": olt.tecnologia, "router_id": olt.router_id,
                "equipo_padre_id": getattr(olt, "equipo_padre_id", None),
                "pons": [{"pon": pon, "clientes": lista} for pon, lista in sorted(pons.items(), key=lambda par: (par[0] is None, par[0] or 0))],
            })
        return resultado

    async def arbol(self) -> Dict[str, Any]:
        """La red completa: la troncal arriba (nodo principal, troncal, enlaces a cada zona),
        de cada enlace cuelga el MikroTik de la zona y de él sus AP y sus clientes."""
        routers = (await self.db.execute(select(RouterModel).order_by(RouterModel.nombre))).scalars().all()
        equipos = (await self.db.execute(select(EquipoRedModel).order_by(EquipoRedModel.nombre))).scalars().all()
        clientes = (
            await self.db.execute(
                select(ClienteModel).where(ClienteModel.tipo_conexion == "radio", ClienteModel.estado != "cancelado")
                .order_by(ClienteModel.nombre)
            )
        ).scalars().all()
        por_equipo: Dict[Optional[int], List[Dict[str, Any]]] = {}
        sin_ap: Dict[Optional[int], List[Dict[str, Any]]] = {}
        for c in clientes:
            dato = {
                "id": c.id, "contrato": c.cedula, "nombre": c.nombre, "estado": c.estado, "ip_antena": c.ip_antena,
                "responde": c.antena_responde, "latencia_ms": c.antena_latencia_ms, "senal": c.antena_senal,
            }
            if c.equipo_red_id:
                por_equipo.setdefault(c.equipo_red_id, []).append(dato)
            else:
                sin_ap.setdefault(c.router_id, []).append(dato)
        # Un equipo en un círculo (A cuelga de B y B de A) se dibuja arriba en vez de perderse.
        padres = {e.id: e.padre_id for e in equipos}
        en_circulo = {e.id for e in equipos if e.id in ancestros(e.id, padres)}
        ids = {e.id for e in equipos} - en_circulo
        colgados = {e.router_hijo_id for e in equipos if e.router_hijo_id}
        olts = await self._olts_con_clientes()
        ids_todos = {e.id for e in equipos}

        def nodo_router(router: RouterModel) -> Dict[str, Any]:
            return {
                "id": router.id, "nombre": router.nombre, "ip": router.ip_vpn, "en_linea": bool(router.is_online),
                "equipos": [nodo(e) for e in equipos
                            if e.router_id == router.id and not e.troncal and e.padre_id not in ids],
                "clientes_sin_ap": sin_ap.get(router.id, []),
                "olts": [o for o in olts if o["router_id"] == router.id and o["equipo_padre_id"] not in ids_todos],
            }

        por_id = {r.id: r for r in routers}

        def nodo(equipo: EquipoRedModel) -> Dict[str, Any]:
            hijo = por_id.get(equipo.router_hijo_id) if equipo.router_hijo_id else None
            return {
                **self._datos(equipo),
                "hijos": [nodo(h) for h in equipos if h.padre_id == equipo.id and h.id not in en_circulo],
                "clientes": por_equipo.get(equipo.id, []),
                "router": nodo_router(hijo) if hijo else None,
                "olts": [o for o in olts if o["equipo_padre_id"] == equipo.id],
            }

        troncal = [nodo(e) for e in equipos if e.troncal and e.padre_id not in ids]
        resultado = []
        for router in routers:
            if router.id in colgados:
                continue  # se dibuja dentro de su enlace de la troncal
            dato = nodo_router(router)
            if dato["equipos"] or dato["clientes_sin_ap"] or dato["olts"]:
                resultado.append(dato)
        sin_router = [nodo(e) for e in equipos if not e.router_id and not e.troncal and e.padre_id not in ids]
        if sin_router:
            resultado.append({"id": None, "nombre": "Sin MikroTik", "ip": None, "en_linea": None,
                              "equipos": sin_router, "clientes_sin_ap": sin_ap.get(None, []), "olts": []})
        datos = {"troncal": troncal, "routers": resultado, "tipos": TIPOS, "marcas": MARCAS}
        datos["fallas"] = puntos_de_falla(datos)
        return datos
