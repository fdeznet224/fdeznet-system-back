"""Plantillas TR-069: cómo debe quedar cada módem, y que quede así solo.

El técnico solo le pone al equipo la WAN de internet y el ACS. En cuanto se reporta
(o cada vez que arranca o lo restablecen) el sistema le pone lo que le falte de su
plantilla, con los datos de su cliente:

  - WAN: la del ACS (la que puso el técnico) con el PPPoE del cliente, y las demás
    (p. ej. la de fichas en puente, VLAN 4) se crean si no están.
  - Redes WiFi: la del cliente (WLAN 1 y su 5 GHz) con el nombre y la contraseña que
    el sistema tiene guardados de él, y las extra (p. ej. "Zona FdezNet" abierta).
  - Enlace de puertos: qué LAN y qué redes salen por cada WAN ("red:<nombre>").
  - LAN (opcional): IP del módem y DHCP.

Lo que ya está bien no se toca. Solo se aplica sola si la plantilla es automática y el
equipo acaba de arrancar o de entrar al ACS; a los demás se les revisa desde la pantalla.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.config_onu_service import (
    CONEXIONES, IGD, WLANS, ConfigOnuService, configuracion, marca_de, serial_reportado, valores_lan, HOST,
)
from src.application.services.red_cliente_service import _candado, serial_tr069
from src.infrastructure.models import (
    AprovisionamientoOnuModel, ClienteModel, EstadoEquipoTr069Model, InventarioONUModel, PlantillaTr069Model, ServicioModel,
)

logger = logging.getLogger(__name__)

RED_CLIENTE = "cliente"
BANDAS = ("2.4 GHz", "5 GHz")
RE_LAN = re.compile(r"^LAN\d+$")
# Arrancó hace menos de esto y no se ha revisado desde entonces: se le aplica su plantilla.
RECIENTE = timedelta(minutes=15)
# Si falló, se vuelve a intentar pasado este tiempo mientras siga siendo de su último arranque.
REINTENTO = timedelta(minutes=30)
VENTANA_REINTENTO = timedelta(hours=6)


# ------------------------------------------------------------------ definición
def validar(definicion: Dict[str, Any]) -> Dict[str, Any]:
    """La deja limpia o explica qué está mal."""
    if not isinstance(definicion, dict):
        raise ValueError("Plantilla no válida.")
    redes = []
    nombres = set()
    for red in definicion.get("redes") or []:
        nombre = str(red.get("nombre") or "").strip()
        if not nombre or len(nombre) > 32 or not re.fullmatch(r"[\x20-\x7e]+", nombre):
            raise ValueError("Cada red WiFi necesita un nombre de 1 a 32 caracteres, sin acentos ni ñ.")
        if nombre in nombres:
            raise ValueError(f"La red {nombre} está dos veces.")
        nombres.add(nombre)
        bandas = [b for b in (red.get("bandas") or list(BANDAS)) if b in BANDAS]
        if not bandas:
            raise ValueError(f"Elige al menos una banda para la red {nombre}.")
        redes.append({"nombre": nombre, "bandas": bandas, "oculta": bool(red.get("oculta"))})
    if RED_CLIENTE not in nombres:
        redes.insert(0, {"nombre": RED_CLIENTE, "bandas": list(BANDAS), "oculta": False})
    wans = []
    acs = 0
    for wan in definicion.get("wans") or []:
        tipo, modo = wan.get("tipo"), wan.get("modo")
        if tipo not in ("pppoe", "ip") or modo not in ("ruta", "puente"):
            raise ValueError("Cada WAN es PPPoE o IP, en ruta o en puente.")
        try:
            vlan = int(wan.get("vlan"))
        except (TypeError, ValueError) as error:
            raise ValueError("Cada WAN necesita su VLAN.") from error
        if not 1 <= vlan <= 4094:
            raise ValueError("La VLAN va de 1 a 4094.")
        puertos = []
        for puerto in wan.get("puertos") or []:
            puerto = str(puerto).strip()
            if RE_LAN.match(puerto.upper()):
                puertos.append(puerto.upper())
            elif puerto.startswith("red:") and puerto[4:] in nombres | {RED_CLIENTE}:
                puertos.append(puerto)
            else:
                raise ValueError(f"Puerto no válido: {puerto}.")
        es_acs = bool(wan.get("acs"))
        acs += es_acs
        if es_acs and (tipo != "pppoe" or modo != "ruta"):
            raise ValueError("La WAN del ACS es la PPPoE de internet, en ruta.")
        wans.append({
            "nombre": str(wan.get("nombre") or "").strip()[:32] or f"VLAN {vlan}",
            "acs": es_acs, "tipo": tipo, "modo": modo, "vlan": vlan,
            "servicio": str(wan.get("servicio") or "INTERNET").strip().upper()[:40],
            "nat": bool(wan.get("nat", modo == "ruta")),
            "pppoe_cliente": bool(wan.get("pppoe_cliente", es_acs)),
            "puertos": puertos,
        })
    if acs != 1:
        raise ValueError("La plantilla necesita exactamente una WAN marcada como la del ACS (la de internet).")
    usados: Dict[str, str] = {}
    for wan in wans:
        for puerto in wan["puertos"]:
            if puerto in usados:
                raise ValueError(f"{puerto} está en dos WAN ({usados[puerto]} y {wan['nombre']}).")
            usados[puerto] = wan["nombre"]
    lan = definicion.get("lan")
    if lan:
        valores_lan(lan, f"{HOST}.IPInterface.1")  # valida
        lan = {k: lan.get(k) for k in ("ip", "mascara", "dhcp", "inicio", "fin", "tiempo", "dns") if lan.get(k) is not None}
    return {"wans": wans, "redes": redes, "lan": lan or None}


def desde_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Una plantilla con lo que tiene un equipo que ya está bien (para copiarla a los demás)."""
    principales = indices_cliente(config["wifi"])
    nombre_de: Dict[int, str] = {}
    redes: Dict[str, Dict[str, Any]] = {}
    for red in config["wifi"]:
        if not red["habilitada"]:
            continue
        nombre = RED_CLIENTE if red["indice"] in principales else red["ssid"]
        nombre_de[red["indice"]] = nombre
        actual = redes.setdefault(nombre, {"nombre": nombre, "bandas": [], "oculta": red["oculta"]})
        if red["banda"] not in actual["bandas"]:
            actual["bandas"].append(red["banda"])
    wans = []
    for wan in config["wans"]:
        if not wan["habilitada"]:
            continue
        puertos = []
        for puerto in wan["puertos"]:
            m = re.fullmatch(r"SSID(\d+)", puerto)
            if not m:
                puertos.append(puerto)
            elif int(m.group(1)) in nombre_de and f"red:{nombre_de[int(m.group(1))]}" not in puertos:
                puertos.append(f"red:{nombre_de[int(m.group(1))]}")
        wans.append({
            "nombre": wan["nombre"], "acs": wan["tr069"], "tipo": wan["tipo"], "modo": wan["modo"],
            "vlan": wan["vlan"] or 1, "servicio": wan["servicio"] or "INTERNET", "nat": wan["nat"],
            "pppoe_cliente": wan["tr069"], "puertos": puertos,
        })
    return {"wans": wans, "redes": sorted(redes.values(), key=lambda r: r["nombre"] != RED_CLIENTE), "lan": None}


def elegir(plantillas: List[Any], marca: str, modelo: str) -> Optional[Any]:
    """La de su marca que nombra su modelo; si no hay, la de su marca sin modelos."""
    modelo = (modelo or "").upper().replace("%2D", "-")
    candidatas = [p for p in plantillas if p.marca == marca]
    for plantilla in candidatas:
        modelos = [m.strip().upper() for m in (plantilla.modelos or "").split(",") if m.strip()]
        if modelos and any(m in modelo for m in modelos):
            return plantilla
    return next((p for p in candidatas if not (p.modelos or "").strip()), None)


# ------------------------------------------------------------------ comparar
def indices_cliente(wifi: List[Dict[str, Any]]) -> set:
    """La red del cliente: la WLAN 1, la primera de 5 GHz que no sea de otra red y las que se llamen igual que la 1."""
    if not wifi:
        return set()
    primera = min(wifi, key=lambda r: r["indice"])
    indices = {r["indice"] for r in wifi if r["ssid"] == primera["ssid"]}
    if not any(r["banda"] == "5 GHz" for r in wifi if r["indice"] in indices):
        cinco = next((r for r in wifi if r["banda"] == "5 GHz" and r["indice"] == 5), None)
        if cinco:
            indices.add(cinco["indice"])
    return indices


def asignar_redes(config: Dict[str, Any], definicion: Dict[str, Any]) -> Dict[str, Dict[str, Optional[int]]]:
    """{red: {banda: índice o None}} según lo que el equipo ya tiene."""
    wifi = config["wifi"]
    cliente = indices_cliente(wifi)
    salida: Dict[str, Dict[str, Optional[int]]] = {}
    for red in definicion["redes"]:
        por_banda: Dict[str, Optional[int]] = {}
        for banda in red["bandas"]:
            if red["nombre"] == RED_CLIENTE:
                indice = next((r["indice"] for r in wifi if r["indice"] in cliente and r["banda"] == banda), None)
            else:
                indice = next((r["indice"] for r in wifi
                               if r["ssid"] == red["nombre"] and r["banda"] == banda and r["indice"] not in cliente), None)
            por_banda[banda] = indice
        salida[red["nombre"]] = por_banda
    return salida


def wan_del_equipo(config: Dict[str, Any], wan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if wan["acs"]:
        return next((w for w in config["wans"] if w["tr069"]), None)
    return next((w for w in config["wans"]
                 if not w["tr069"] and w["tipo"] == wan["tipo"] and w["modo"] == wan["modo"] and w["vlan"] == wan["vlan"]), None)


def puertos_deseados(wan: Dict[str, Any], asignacion: Dict[str, Dict[str, Optional[int]]]) -> List[str]:
    salida = []
    for puerto in wan["puertos"]:
        if puerto.startswith("red:"):
            salida += [f"SSID{i}" for i in (asignacion.get(puerto[4:]) or {}).values() if i]
        else:
            salida.append(puerto)
    return salida


def enlaces_pendientes(config: Dict[str, Any], definicion: Dict[str, Any]) -> List[Tuple[Dict[str, Any], List[str]]]:
    """[(WAN del equipo, puertos que debe quedar teniendo)] de las que no están como dice la plantilla."""
    asignacion = asignar_redes(config, definicion)
    deseados = {}
    for wan in definicion["wans"]:
        del_equipo = wan_del_equipo(config, wan)
        if del_equipo:
            deseados[del_equipo["ruta"]] = (del_equipo, puertos_deseados(wan, asignacion))
    reclamados = {p for _, puertos in deseados.values() for p in puertos}
    pendientes = []
    for del_equipo, puertos in deseados.values():
        actuales = set(del_equipo["puertos"])
        # Se queda con lo suyo de la plantilla y con lo que la plantilla no reparte.
        queda = sorted(set(puertos) | (actuales - reclamados), key=lambda p: (p.startswith("SSID"), int(re.sub(r"\D", "", p))))
        if set(queda) != actuales:
            pendientes.append((del_equipo, queda))
    return pendientes


def lo_que_falta(config: Dict[str, Any], definicion: Dict[str, Any], cliente: Optional[Dict[str, Any]]) -> List[str]:
    faltan = []
    for wan in definicion["wans"]:
        del_equipo = wan_del_equipo(config, wan)
        if not del_equipo:
            faltan.append("WAN de internet con el ACS" if wan["acs"] else f"WAN {wan['nombre']} ({wan['modo']}, VLAN {wan['vlan']})")
            continue
        if not del_equipo["habilitada"]:
            faltan.append(f"WAN {wan['nombre']} apagada")
        if (wan["acs"] and wan["pppoe_cliente"] and cliente and cliente.get("usuario_pppoe")
                and del_equipo["usuario"] != cliente["usuario_pppoe"]):
            faltan.append(f"PPPoE del cliente ({cliente['usuario_pppoe']}; tiene {del_equipo['usuario'] or 'otro'})")
    asignacion = asignar_redes(config, definicion)
    por_indice = {r["indice"]: r for r in config["wifi"]}
    for red in definicion["redes"]:
        for banda, indice in asignacion[red["nombre"]].items():
            nombre = "red del cliente" if red["nombre"] == RED_CLIENTE else f"red {red['nombre']}"
            if indice is None:
                if red["nombre"] != RED_CLIENTE:
                    faltan.append(f"{nombre} {banda}")
                continue
            actual = por_indice[indice]
            if not actual["habilitada"]:
                faltan.append(f"{nombre} {banda} apagada")
            if red["nombre"] == RED_CLIENTE and cliente and cliente.get("ssid") and actual["ssid"] != cliente["ssid"]:
                faltan.append(f"WiFi del cliente {banda} se llama {actual['ssid']} (debe ser {cliente['ssid']})")
    for del_equipo, _ in enlaces_pendientes(config, definicion):
        faltan.append(f"enlace de puertos de {del_equipo['nombre']}")
    lan = definicion.get("lan")
    if lan and any(str(lan.get(k)) != str(config["lan"].get(k)) for k in ("ip", "inicio", "fin") if lan.get(k)):
        faltan.append("LAN distinta a la plantilla")
    return faltan


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _fecha(texto: Optional[str]) -> Optional[datetime]:
    if not texto:
        return None
    try:
        return datetime.fromisoformat(str(texto).replace("Z", "+00:00"))
    except ValueError:
        return None


def marca_del_documento(documento: Dict[str, Any]) -> str:
    """Por el fabricante que reporta (llega desde el primer aviso); si no, por sus parámetros."""
    ident = documento.get("_deviceId") or {}
    texto = f"{ident.get('_Manufacturer', '')} {ident.get('_SerialNumber', '')}".upper()
    if "ZTE" in texto:
        return "zte"
    if "HUAWEI" in texto or str(ident.get("_SerialNumber", "")).upper().startswith("48575443"):
        return "huawei"
    return marca_de(documento.get(IGD) or {})[0]


def ultimo_arranque(documento: Dict[str, Any]) -> Optional[datetime]:
    fechas = [f for f in (_fecha(documento.get("_lastBoot")), _fecha(documento.get("_lastBootstrap"))) if f]
    return max(fechas) if fechas else None


# ------------------------------------------------------------------ clientes
async def clientes_por_equipo(db: AsyncSession) -> tuple:
    """({serie: cliente}, {ip: cliente}) para ligar cada equipo del ACS con su cliente."""
    por_serie: Dict[str, Dict[str, Any]] = {}
    por_ip: Dict[str, Dict[str, Any]] = {}
    filas = (await db.execute(
        select(ClienteModel.id, ClienteModel.nombre, ClienteModel.ip_asignada, InventarioONUModel.identificador)
        .outerjoin(InventarioONUModel, InventarioONUModel.id == ClienteModel.onu_id)
    )).all()
    filas += (await db.execute(
        select(ClienteModel.id, ClienteModel.nombre, ServicioModel.ip_asignada, InventarioONUModel.identificador)
        .join(ServicioModel, ServicioModel.cliente_id == ClienteModel.id)
        .outerjoin(InventarioONUModel, InventarioONUModel.id == ServicioModel.onu_id)
    )).all()
    for cliente_id, nombre, ip, identificador in filas:
        cliente = {"id": cliente_id, "nombre": nombre}
        if identificador:
            limpio = re.sub(r"[^0-9A-Za-z]", "", identificador).upper()
            for serie in filter(None, {limpio, serial_tr069(identificador)}):
                por_serie.setdefault(serie, cliente)
        if ip:
            por_ip.setdefault(ip.strip(), cliente)
    return por_serie, por_ip


def cliente_de(documento: Dict[str, Any], mapas: tuple) -> Optional[Dict[str, Any]]:
    from src.application.services.config_onu_service import ip_de_gestion

    por_serie, por_ip = mapas
    return por_serie.get(serial_reportado(documento)) or por_ip.get(ip_de_gestion(documento) or "")


# ------------------------------------------------------------------ servicio
class PlantillaTr069Service:
    def __init__(self, db: AsyncSession, nbi: Optional[str] = None, transporte=None):
        self.db = db
        self.cfg = ConfigOnuService(db, nbi, transporte)

    async def plantillas(self, solo_automaticas: bool = False) -> List[PlantillaTr069Model]:
        consulta = select(PlantillaTr069Model).order_by(PlantillaTr069Model.marca, PlantillaTr069Model.nombre)
        if solo_automaticas:
            consulta = consulta.where(PlantillaTr069Model.automatica.is_(True))
        return list((await self.db.execute(consulta)).scalars().all())

    async def datos_cliente(self, cliente_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if not cliente_id:
            return None
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente:
            return None
        registro = (await self.db.execute(
            select(AprovisionamientoOnuModel).where(AprovisionamientoOnuModel.cliente_id == cliente_id)
            .order_by(AprovisionamientoOnuModel.id.desc()).limit(1)
        )).scalars().first()
        conocido = registro if registro and registro.estado in ("aplicado", "guardado", "manual") else None
        return {
            "id": cliente.id, "nombre": cliente.nombre,
            "usuario_pppoe": cliente.user_pppoe or None, "clave_pppoe": cliente.pass_pppoe or None,
            "ssid": conocido.ssid if conocido else None, "clave_wifi": conocido.clave_wifi if conocido else None,
            "pendiente": bool(registro and registro.estado == "pendiente"),
        }

    async def _estado(self, device_id: str) -> EstadoEquipoTr069Model:
        estado = await self.db.get(EstadoEquipoTr069Model, device_id)
        if not estado:
            estado = EstadoEquipoTr069Model(device_id=device_id)
            self.db.add(estado)
        return estado

    async def _guardar(self, device_id: str, plantilla, faltan: List[str], hecho: Optional[List[str]] = None,
                       error: Optional[str] = None) -> Dict[str, Any]:
        estado = await self._estado(device_id)
        estado.plantilla_id = plantilla.id if plantilla else None
        estado.conforme = not faltan and not error
        estado.faltan = json.dumps(faltan, ensure_ascii=False)
        estado.error = (error or "")[:255] or None
        estado.revisado_en = datetime.now()
        if hecho is not None:
            estado.hecho = json.dumps(hecho, ensure_ascii=False)
            if hecho:
                estado.aplicado_en = datetime.now()
        await self.db.commit()
        return estado_dict(estado, plantilla)

    async def _contexto(self, http, device_id: str) -> tuple:
        documento = await self.cfg._documento(http, device_id)
        if not documento:
            raise LookupError("Ese equipo no está en el ACS.")
        marca = marca_del_documento(documento)
        modelo = str((documento.get("_deviceId") or {}).get("_ProductClass") or "")
        plantilla = elegir(await self.plantillas(), marca, modelo)
        cliente = cliente_de(documento, await clientes_por_equipo(self.db))
        return documento, plantilla, await self.datos_cliente(cliente["id"] if cliente else None)

    async def revisar_equipo(self, device_id: str) -> Dict[str, Any]:
        """Lee el equipo y lo compara con su plantilla, sin cambiarle nada."""
        async with self.cfg._http() as http:
            documento, plantilla, cliente = await self._contexto(http, device_id)
            if not plantilla:
                return {"plantilla": None, "conforme": None, "faltan": [], "error": None}
            async with _candado(device_id):
                documento = await self.cfg._releer(http, device_id)
        faltan = lo_que_falta(configuracion(documento), json.loads(plantilla.definicion), cliente)
        return await self._guardar(device_id, plantilla, faltan)

    async def aplicar_equipo(self, device_id: str, usuario=None) -> Dict[str, Any]:
        async with self.cfg._http() as http:
            documento, plantilla, cliente = await self._contexto(http, device_id)
            if not plantilla:
                raise ValueError("No hay plantilla para la marca y modelo de este equipo.")
            resultado = await self._aplicar(http, device_id, json.loads(plantilla.definicion), cliente)
        estado = await self._guardar(device_id, plantilla, resultado["faltan"], resultado["hecho"])
        if resultado["hecho"]:
            await self.cfg._bitacora(usuario, f"Aplicar plantilla {plantilla.nombre}", device_id, ", ".join(resultado["hecho"]))
        return {**estado, "hecho": resultado["hecho"]}

    async def _aplicar(self, http, device_id: str, definicion: Dict[str, Any], cliente: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
        hecho: List[str] = []
        cfg = self.cfg
        async with _candado(device_id):
            documento = await cfg._releer(http, device_id)
            igd = documento.get(IGD) or {}
            marca, _ = marca_de(igd)
            config = configuracion(documento)
            if not any(w["tr069"] for w in config["wans"]):
                return {"hecho": [], "faltan": ["no se encontró la WAN del ACS"]}

            # 1) WAN que falten o estén apagadas (la del ACS no se toca aquí).
            cambio = False
            for wan in definicion["wans"]:
                del_equipo = wan_del_equipo(config, wan)
                if wan["acs"]:
                    continue
                if not del_equipo:
                    datos = {k: wan[k] for k in ("tipo", "modo", "vlan", "servicio", "nombre")}
                    if wan["modo"] == "ruta":
                        datos["nat"] = wan["nat"]
                    if wan["tipo"] == "pppoe" and wan["modo"] == "ruta":
                        if not (wan["pppoe_cliente"] and cliente and cliente.get("usuario_pppoe") and cliente.get("clave_pppoe")):
                            continue
                        datos.update(usuario=cliente["usuario_pppoe"], clave=cliente["clave_pppoe"])
                    await cfg.crear_wan(http, device_id, igd, datos)
                    hecho.append(f"WAN {wan['nombre']} (VLAN {wan['vlan']})")
                    cambio = True
                elif not del_equipo["habilitada"]:
                    await cfg._aplicar(http, device_id, [[f"{del_equipo['ruta']}.Enable", True, "xsd:boolean"]])
                    hecho.append(f"encendió WAN {wan['nombre']}")
                    cambio = True
            if cambio:
                documento = await cfg._releer(http, device_id, ["wan"])
                igd, config = documento.get(IGD) or {}, configuracion(documento)

            # 2) Redes WiFi.
            cambio = False
            asignacion = asignar_redes(config, definicion)
            por_indice = {r["indice"]: r for r in config["wifi"]}
            ocupados = {i for bandas in asignacion.values() for i in bandas.values() if i} | indices_cliente(config["wifi"])
            for red in definicion["redes"]:
                for banda, indice in asignacion[red["nombre"]].items():
                    if red["nombre"] == RED_CLIENTE:
                        if indice and not por_indice[indice]["habilitada"]:
                            await cfg._aplicar(http, device_id, [[f"{WLANS}.{indice}.Enable", True, "xsd:boolean"]])
                            hecho.append(f"encendió la red del cliente {banda}")
                            cambio = True
                        continue
                    if indice is None:
                        libres = [r["indice"] for r in config["wifi"]
                                  if r["banda"] == banda and not r["habilitada"] and r["indice"] not in ocupados and r["indice"] != 1]
                        for libre in libres:
                            if await self._red_abierta(http, device_id, libre, red["nombre"], red["oculta"], marca):
                                ocupados.add(libre)
                                hecho.append(f"red {red['nombre']} {banda}")
                                cambio = True
                                break
                    elif not por_indice[indice]["habilitada"]:
                        await cfg._aplicar(http, device_id, [[f"{WLANS}.{indice}.Enable", True, "xsd:boolean"]])
                        hecho.append(f"encendió red {red['nombre']} {banda}")
                        cambio = True
            # La red del cliente con su nombre y contraseña (si el sistema los conoce).
            if cliente and cliente.get("ssid") and cliente.get("clave_wifi"):
                suyos = sorted(i for i in asignacion[RED_CLIENTE].values() if i)
                if suyos and any(por_indice[i]["ssid"] != cliente["ssid"] for i in suyos):
                    nombres = [[f"{WLANS}.{i}.SSID", cliente["ssid"], "xsd:string"] for i in suyos]
                    await cfg.red.poner_con_clave_wifi(http, device_id, suyos, cliente["clave_wifi"], antes=nombres)
                    hecho.append(f"WiFi del cliente {cliente['ssid']}")
                    cambio = True
            if cambio:
                documento = await cfg._releer(http, device_id, ["wifi", "wan"])
                igd, config = documento.get(IGD) or {}, configuracion(documento)

            # 3) Enlace de puertos.
            pendientes = enlaces_pendientes(config, definicion)
            for del_equipo, puertos in pendientes:
                await cfg._poner_enlaces(http, device_id, igd, del_equipo["ruta"], puertos)
                hecho.append(f"enlace de {del_equipo['nombre']}")
                documento = await cfg._releer(http, device_id, ["wan"])
                igd, config = documento.get(IGD) or {}, configuracion(documento)

            # 4) LAN.
            lan = definicion.get("lan")
            if lan and any(str(lan.get(k)) != str(config["lan"].get(k)) for k in ("ip", "inicio", "fin") if lan.get(k)):
                host = (((igd.get("LANDevice") or {}).get("1") or {}).get("LANHostConfigManagement") or {})
                n = next((k for k, v in sorted(host.get("IPInterface", {}).items()) if k.isdigit()
                          and str((v.get("Enable") or {}).get("_value")).lower() in ("true", "1")), "1")
                await cfg._aplicar(http, device_id, valores_lan(lan, f"{HOST}.IPInterface.{n}"))
                hecho.append("LAN")
                documento = await cfg._releer(http, device_id, ["lan"])
                config = configuracion(documento)

            # 5) Al final el PPPoE del cliente: al cambiarlo la ONU se reconecta (y el ACS va por ahí).
            acs_wan = next(w for w in definicion["wans"] if w["acs"])
            del_equipo = wan_del_equipo(config, acs_wan)
            if (acs_wan["pppoe_cliente"] and del_equipo and cliente and cliente.get("usuario_pppoe") and cliente.get("clave_pppoe")
                    and del_equipo["usuario"] != cliente["usuario_pppoe"]):
                ruta = del_equipo["ruta"]
                enviado = await cfg.red._tarea(http, device_id, {"name": "setParameterValues", "parameterValues": [
                    [f"{ruta}.Username", cliente["usuario_pppoe"], "xsd:string"],
                    [f"{ruta}.Password", cliente["clave_pppoe"], "xsd:string"]]})
                hecho.append(f"PPPoE {cliente['usuario_pppoe']}" + ("" if enviado else " (enviado)"))
                del_equipo["usuario"] = cliente["usuario_pppoe"]

        return {"hecho": hecho, "faltan": lo_que_falta(config, definicion, cliente)}

    async def _red_abierta(self, http, device_id: str, indice: int, nombre: str, oculta: bool, marca: str) -> bool:
        """Enciende esa red abierta con ese nombre; False si el firmware no la deja (se prueba otra)."""
        red = self.cfg.red
        ruta = f"{WLANS}.{indice}"
        try:
            await red._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": [
                [f"{ruta}.SSID", nombre, "xsd:string"], [f"{ruta}.BasicAuthenticationMode", "None", "xsd:string"],
                [f"{ruta}.BasicEncryptionModes", "None", "xsd:string"],
                [f"{ruta}.SSIDAdvertisementEnabled", not oculta, "xsd:boolean"]]})
        except ValueError:
            return False
        # Una red abierta se enciende con BeaconType "Basic" en la Huawei y "None" o "Basic" según el firmware de la ZTE.
        for seguridad in (("Basic", "None") if marca == "huawei" else ("None", "Basic")):
            try:
                await red._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": [
                    [f"{ruta}.BeaconType", seguridad, "xsd:string"]]})
                await red._obligada(http, device_id, {"name": "setParameterValues", "parameterValues": [
                    [f"{ruta}.Enable", True, "xsd:boolean"]]})
            except ValueError:
                continue
            await red._obligada(http, device_id, {"name": "getParameterValues",
                                                  "parameterNames": [f"{ruta}.Enable", f"{ruta}.SSID"]})
            wlan = (((((await self.cfg._documento(http, device_id)).get(IGD) or {}).get("LANDevice") or {}).get("1") or {})
                    .get("WLANConfiguration") or {}).get(str(indice)) or {}
            return (str((wlan.get("Enable") or {}).get("_value")).lower() in ("true", "1")
                    and (wlan.get("SSID") or {}).get("_value") == nombre)
        return False

    # ------------------------------------------------------------ automática
    async def revisar_recientes(self) -> Dict[str, int]:
        """Una vuelta: los equipos que arrancaron o entraron al ACS hace poco y tienen plantilla automática."""
        conteo = {"revisados": 0, "configurados": 0, "errores": 0}
        plantillas = await self.plantillas(solo_automaticas=True)
        if not plantillas:
            return conteo
        desde = (_ahora() - RECIENTE).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        async with self.cfg._http() as http:
            respuesta = await http.get("/devices", params={
                "query": json.dumps({"$or": [{"_lastBoot": {"$gt": desde}}, {"_lastBootstrap": {"$gt": desde}},
                                             {"_registered": {"$gt": desde}}]}),
                "projection": "_id,_deviceId,_lastBoot,_lastBootstrap,_registered,_lastInform,"
                              f"{IGD}.ManagementServer.ConnectionRequestURL,{IGD}.WANDevice.1.WANConnectionDevice",
            })
            respuesta.raise_for_status()
            mapas = None
            for documento in respuesta.json():
                device_id = documento["_id"]
                plantilla = elegir(plantillas, marca_del_documento(documento),
                                   str((documento.get("_deviceId") or {}).get("_ProductClass") or ""))
                if not plantilla:
                    continue
                arranque = ultimo_arranque(documento) or _fecha(documento.get("_registered"))
                estado = await self.db.get(EstadoEquipoTr069Model, device_id)
                # revisado_en se guarda en hora local sin zona; GenieACS da UTC.
                revisado = estado.revisado_en.astimezone() if estado and estado.revisado_en else None
                if revisado and arranque and revisado > arranque:
                    if not (estado.error and _ahora() - revisado > REINTENTO and _ahora() - arranque < VENTANA_REINTENTO):
                        continue
                mapas = mapas or await clientes_por_equipo(self.db)
                cliente = cliente_de(documento, mapas)
                datos = await self.datos_cliente(cliente["id"] if cliente else None)
                if datos and datos["pendiente"]:
                    continue  # La configuración del alta (aprovisionamiento) va primero.
                conteo["revisados"] += 1
                try:
                    resultado = await self._aplicar(http, device_id, json.loads(plantilla.definicion), datos)
                    await self._guardar(device_id, plantilla, resultado["faltan"], resultado["hecho"])
                    if resultado["hecho"]:
                        conteo["configurados"] += 1
                        await self.cfg._bitacora(None, f"Plantilla automática {plantilla.nombre}", device_id, ", ".join(resultado["hecho"]))
                    if resultado["faltan"]:
                        conteo["errores"] += 1
                except (ValueError, LookupError, httpx.HTTPError) as error:
                    conteo["errores"] += 1
                    await self._guardar(device_id, plantilla, [], [], error=str(error) or "sin respuesta")
        return conteo


def estado_dict(estado: Optional[EstadoEquipoTr069Model], plantilla=None) -> Dict[str, Any]:
    if not estado:
        return {"plantilla": None, "conforme": None, "faltan": [], "error": None, "revisado_en": None, "aplicado_en": None}
    return {
        "plantilla": ({"id": plantilla.id, "nombre": plantilla.nombre} if plantilla else
                      ({"id": estado.plantilla_id, "nombre": None} if estado.plantilla_id else None)),
        "conforme": estado.conforme,
        "faltan": json.loads(estado.faltan or "[]"),
        "hecho": json.loads(estado.hecho or "[]"),
        "error": estado.error,
        "revisado_en": estado.revisado_en.isoformat() if estado.revisado_en else None,
        "aplicado_en": estado.aplicado_en.isoformat() if estado.aplicado_en else None,
    }


def plantilla_dict(p: PlantillaTr069Model) -> Dict[str, Any]:
    return {"id": p.id, "nombre": p.nombre, "marca": p.marca, "modelos": p.modelos or "", "automatica": p.automatica,
            "definicion": json.loads(p.definicion), "actualizada_en": p.actualizada_en.isoformat() if p.actualizada_en else None}
