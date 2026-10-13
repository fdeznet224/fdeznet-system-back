"""Panel de la OLT VSOL dentro del sistema: la misma lista de ONU y las mismas acciones.

Cada acción manda a /action/ lo mismo que el panel web de la VSOL (sacado de
su código). Nunca se le da "Save" a la OLT: tras un apagón debe arrancar
limpia (ver guardar_en_olt). Antes de tocar una ONU se confirma que en ese
PON y número sigue el serial que el usuario ve en pantalla.
"""

import asyncio
import re
import unicodedata
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.application.services.cache_olt import olvidar
from src.application.services.descripcion_onu_service import DescripcionOnuService, LARGO_MAXIMO
from src.application.services.vsol_api_service import VsolApiService, guardar_en_olt
from src.infrastructure.models import (
    ClienteModel, InventarioONUModel, LogActividadModel, OLTModel, ServicioModel,
)

# who de /action/gpononuauthinfo para cada botón de la fila (como el panel).
# OJO: who=0 borra; who=2 borra TODO el PON.
WHO_FILA = {
    "eliminar": 0,
    "reiniciar": 1,
    "marcar": 5,
    "desmarcar": 6,
    "activar": 20,
    "desactivar": 21,
}
NOMBRE_ACCION = {
    "eliminar": "Eliminar ONU",
    "reiniciar": "Reiniciar ONU",
    "marcar": "Marcar ONU",
    "desmarcar": "Desmarcar ONU",
    "activar": "Activar ONU",
    "desactivar": "Desactivar ONU",
}


def _texto(valor: Any) -> str:
    return str(valor if valor is not None else "").strip()


def _plano(valor: Any) -> str:
    """Minúsculas, sin acentos ni separadores: "Pérez" = "perez", "HWTC-12" = "hwtc12"."""
    texto = unicodedata.normalize("NFKD", _texto(valor)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[\s:\-_./]+", "", texto)


def coincide(buscar: str, auth: Dict[str, Any], del_cliente: str = "") -> bool:
    """Busca en SN, descripción, ONU (0/1:5) y los datos del cliente del sistema."""
    aguja = _plano(buscar)
    if not aguja:
        return True
    pajar = "|".join(_plano(v) for v in (
        auth.get("info"), auth.get("description"), auth.get("onu_id"), auth.get("model"), del_cliente,
    ))
    return aguja in pajar


def fila_onu(auth: Dict[str, Any], optica: Optional[Dict[str, Any]], estado: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Una fila con los nombres de columna del panel."""
    optica, estado = optica or {}, estado or {}
    return {
        "onu_id": _texto(auth.get("onu_id")),
        "pon": int(auth.get("pon_id") or 0),
        "onuid": int(auth.get("onuid") or _texto(auth.get("onu_id")).split(":")[-1] or 0),
        "online": _texto(auth.get("state")) == "1",
        "descripcion": _texto(auth.get("description")),
        "modelo": _texto(auth.get("model")),
        "perfil": _texto(auth.get("profile")),
        "modo": _texto(auth.get("mode")),
        "serial": _texto(auth.get("info")),
        "marcada": _texto(auth.get("attention_status")) == "1",
        # El panel muestra "Deactivate" mientras la ONU está activa.
        "activa": _texto(auth.get("active_action_state")) != "Activate",
        "rx": _texto(optica.get("rx_power")) or None,
        "tx": _texto(optica.get("tx_power")) or None,
        "estado_admin": _texto(estado.get("admin_state")) or None,
        "estado_omcc": _texto(estado.get("omcc_state")) or None,
        "fase": _texto(estado.get("phase_state")) or None,
        "ultimo_registro": _texto(estado.get("last_register_time")) or None,
        "ultima_baja": _texto(estado.get("last_deregister_time")) or None,
        "causa_ultima_baja": _texto(estado.get("last_deregister_reason")) or None,
        "tiempo_activa": _texto(estado.get("alive_time")) or None,
    }


def payload_fila(accion: str, pon: int, onuid: int) -> Dict[str, Any]:
    if accion not in WHO_FILA:
        raise ValueError("Acción no válida.")
    datos = {"who": WHO_FILA[accion], "slotid": 0, "portid": int(pon), "onuid": int(onuid)}
    if accion == "eliminar":
        datos["select"] = int(pon)
    return datos


def payload_eliminar_varias(onus: List[Dict[str, int]]) -> Dict[str, Any]:
    datos: Dict[str, Any] = {"who": 3}
    for i, onu in enumerate(onus, start=1):
        datos[f"select{i}"] = f"slot[0]_pon[{int(onu['pon'])}]_onu[{int(onu['onuid'])}]"
    datos["selectNums"] = len(onus)
    return datos


def payload_eliminar_pon(pon: int, solo_offline: bool) -> Dict[str, Any]:
    return {"slotid": 0, "portid": int(pon), "who": 4 if solo_offline else 2}


def _respuesta_ok(respuesta: Dict[str, Any]) -> None:
    if str(respuesta.get("retcode")) != "0":
        datos = respuesta.get("data") or {}
        raise ValueError(f"La OLT no aceptó el cambio: {datos.get('msg') or datos.get('result') or respuesta}")


class PanelOltService:
    def __init__(self, db, vsol: Optional[VsolApiService] = None):
        self.db = db
        self.vsol = vsol or VsolApiService(db)

    async def _olt(self, olt_id: int) -> OLTModel:
        olt = await self.db.get(OLTModel, olt_id)
        if not olt:
            raise ValueError("OLT no encontrada.")
        if not olt.api_enabled:
            raise ValueError(f"La OLT {olt.nombre} no se puede manejar desde el sistema.")
        return olt

    # ------------------------------------------------------------ lectura
    def _pons_sync(self, olt: OLTModel) -> List[Dict[str, Any]]:
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            pons = []
            for payload in self.vsol._descubrir_payloads_pon_sync(opener, base_url, verify_ssl):
                datos = dict(urllib.parse.parse_qsl(payload.decode("utf-8")))
                if datos.get("portid"):
                    pons.append({"pon": int(datos["portid"]), "port_id": int(datos.get("port_id") or 0),
                                 "nombre": f"PON 0/{datos['portid']}"})
            return pons

    def _lista_sync(self, olt: OLTModel, pon: Optional[int],
                    filtro: Optional[Callable[[Dict[str, Any]], bool]]) -> List[Dict[str, Any]]:
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            todos = [dict(urllib.parse.parse_qsl(p.decode("utf-8")))
                     for p in self.vsol._descubrir_payloads_pon_sync(opener, base_url, verify_ssl)]
            leer = lambda accion, datos, lista: (
                (VsolApiService._post_form(self.vsol, opener, base_url, accion, datos, verify_ssl).get("data") or {})
                .get(lista) or []
            )
            if filtro:
                # "Buscar en TODAS las PON": se filtra aquí y no con el who=10
                # de la OLT, que no encuentra por SN completo ni por cliente.
                auth = [a for datos in todos for a in leer("gpononuauthinfo", datos, "onuAuth_list") if filtro(a)]
                pons_con_resultado = {str(a.get("pon_id")) for a in auth}
                consultas = [p for p in todos if str(p.get("portid")) in pons_con_resultado]
            else:
                consultas = [p for p in todos if pon is None or str(p.get("portid")) == str(pon)]
                auth = [a for datos in consultas for a in leer("gpononuauthinfo", datos, "onuAuth_list")]
            optica, estado = {}, {}
            for datos in consultas:
                optica.update({o.get("onu_id"): o for o in leer("gpononuopticalinfo", datos, "onuOpticalInfo_list")})
                estado.update({o.get("onu_id"): o for o in leer("gpononustatusinfo", datos, "onuStatus_list")})
            filas = [fila_onu(a, optica.get(a.get("onu_id")), estado.get(a.get("onu_id"))) for a in auth]
            filas.sort(key=lambda f: (f["pon"], f["onuid"]))
            return filas

    async def pons(self, olt_id: int) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._pons_sync, await self._olt(olt_id))

    async def lista(self, olt_id: int, pon: Optional[int] = None, buscar: Optional[str] = None) -> Dict[str, Any]:
        olt = await self._olt(olt_id)
        clientes = await DescripcionOnuService(self.db, self.vsol)._clientes_por_onu()
        texto = (buscar or "").strip()
        filtro = None
        if texto:
            # Se arma antes de salir del hilo de la base: solo textos.
            del_cliente = {
                serial: " ".join(_texto(v) for v in (c.nombre, c.cedula, c.user_pppoe, c.ip_asignada, c.telefono))
                for serial, c in clientes.items()
            }
            filtro = lambda a: coincide(texto, a, del_cliente.get(VsolApiService._normalizar_sn(a.get("info")), ""))
        filas = await asyncio.to_thread(self._lista_sync, olt, pon, filtro)
        for fila in filas:
            cliente = clientes.get(VsolApiService._normalizar_sn(fila["serial"]))
            fila["cliente_id"] = cliente.id if cliente else None
            fila["cliente_nombre"] = cliente.nombre if cliente else None
        online = sum(1 for f in filas if f["online"])
        return {"olt": olt.nombre, "onus": filas, "total": len(filas), "online": online, "offline": len(filas) - online}

    def _detalle_sync(self, olt: OLTModel, pon: int, onuid: int) -> Dict[str, Any]:
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            ubicacion = {"slotid": 0, "ponid": pon, "onuid": onuid}
            post = lambda accion, datos: VsolApiService._post_form(self.vsol, opener, base_url, accion, datos, verify_ssl)
            detalle = VsolApiService._propiedades((post("gpononudetail", {**ubicacion, "who": 100}).get("data") or {}).get("onu_detail_info"))
            optica = VsolApiService._propiedades((post("gpononuoptical", ubicacion).get("data") or {}).get("onu_optical_info"))
            auth = self.vsol._onu_en_pon_sync(opener, base_url, verify_ssl, pon, onuid) or {}
            return {"onu_id": f"GPON0/{pon}:{onuid}", "pon": pon, "onuid": onuid,
                    "online": _texto(auth.get("state")) == "1", "serial": _texto(auth.get("info")) or detalle.get("SN"),
                    "detalle": detalle, "optica": optica}

    async def detalle(self, olt_id: int, pon: int, onuid: int) -> Dict[str, Any]:
        datos = await asyncio.to_thread(self._detalle_sync, await self._olt(olt_id), int(pon), int(onuid))
        datos["cliente"] = await self.cliente_de_onu(datos.get("serial"))
        return datos

    async def cliente_de_onu(self, serial: Optional[str]) -> Optional[Dict[str, Any]]:
        """Lo que el sistema sabe del cliente de esa ONU (contrato, IP, PPPoE...)."""
        serial = VsolApiService._normalizar_sn(serial)
        if not serial:
            return None
        onu = (await self.db.execute(
            select(InventarioONUModel).where(InventarioONUModel.identificador == serial)
        )).scalars().first()
        if not onu:
            return None
        servicio = (await self.db.execute(
            select(ServicioModel)
            .options(selectinload(ServicioModel.cliente), selectinload(ServicioModel.plan),
                     selectinload(ServicioModel.zona), selectinload(ServicioModel.router),
                     selectinload(ServicioModel.caja_nap))
            .where(ServicioModel.onu_id == onu.id, ServicioModel.estado != "cancelado")
            .order_by(ServicioModel.id)
        )).scalars().first()
        cliente = servicio.cliente if servicio else (await self.db.execute(
            select(ClienteModel)
            .options(selectinload(ClienteModel.plan), selectinload(ClienteModel.zona),
                     selectinload(ClienteModel.router), selectinload(ClienteModel.caja_nap))
            .where(ClienteModel.onu_id == onu.id, ClienteModel.estado != "cancelado")
        )).scalars().first()
        if not cliente:
            return None
        origen = servicio or cliente
        nombre = lambda rel: getattr(getattr(origen, rel, None), "nombre", None)
        return {
            "id": cliente.id,
            "nombre": cliente.nombre,
            "contrato": cliente.cedula,
            "telefono": cliente.telefono,
            "estado_cliente": cliente.estado,
            "servicio_id": servicio.id if servicio else None,
            "estado_servicio": servicio.estado if servicio else None,
            "direccion": origen.direccion or cliente.direccion,
            "zona": nombre("zona"),
            "plan": nombre("plan"),
            "mikrotik": nombre("router"),
            "ip": origen.ip_asignada,
            "usuario_pppoe": origen.user_pppoe,
            "en_linea": bool(origen.is_online),
            "caja_nap": nombre("caja_nap"),
            "puerto_nap": origen.puerto_nap,
        }

    # ------------------------------------------------------------ acciones
    def _confirmar_serial(self, opener, base_url, verify_ssl, pon: int, onuid: int, serial: str) -> Dict[str, Any]:
        actual = self.vsol._onu_en_pon_sync(opener, base_url, verify_ssl, pon, onuid)
        if not actual or VsolApiService._normalizar_sn(actual.get("info")) != VsolApiService._normalizar_sn(serial):
            raise ValueError(f"En GPON0/{pon}:{onuid} ya no está la ONU {serial}. Refresca la lista.")
        return actual

    def _accion_sync(self, olt, accion, pon, onuid, serial):
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            self._confirmar_serial(opener, base_url, verify_ssl, pon, onuid, serial)
            _respuesta_ok(VsolApiService._post_form(self.vsol, opener, base_url, "gpononuauthinfo",
                                                    payload_fila(accion, pon, onuid), verify_ssl))
            self._guardar_si_toca(opener, base_url, verify_ssl, accion)

    def _guardar_si_toca(self, opener, base_url, verify_ssl, accion: str) -> None:
        # Reiniciar o marcar no cambian la configuración; lo demás solo se guarda si el ISP lo pidió.
        if accion not in ("reiniciar",) and guardar_en_olt():
            self.vsol._guardar_configuracion_sync(opener, base_url, verify_ssl)

    async def _bitacora(self, usuario, accion: str, ruta: str, detalle: str) -> None:
        self.db.add(LogActividadModel(
            usuario_id=getattr(usuario, "id", None), usuario_nombre=getattr(usuario, "usuario", None),
            accion=f"Panel OLT: {accion}", metodo="POST", ruta=ruta, estado_http=200, detalle=detalle[:1000],
        ))
        await self.db.commit()

    async def accion(self, olt_id: int, pon: int, onuid: int, accion: str, serial: str, usuario=None) -> Dict[str, Any]:
        olt = await self._olt(olt_id)
        payload_fila(accion, pon, onuid)  # valida la acción antes de entrar a la OLT
        await asyncio.to_thread(self._accion_sync, olt, accion, int(pon), int(onuid), serial)
        olvidar(f"vsol:{olt.id}")
        # La ruta con /reiniciar hace que Radar diga que la última caída fue un reinicio.
        ruta = f"/api/olts/{olt.id}/onus/{int(pon)}/{int(onuid)}/{'reiniciar' if accion == 'reiniciar' else accion}"
        await self._bitacora(usuario, NOMBRE_ACCION[accion], ruta, f"{olt.nombre} GPON0/{pon}:{onuid} · {serial}")
        return {"ok": True, "accion": accion}

    def _descripcion_sync(self, olt, pon, onuid, serial, texto):
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            self._confirmar_serial(opener, base_url, verify_ssl, pon, onuid, serial)
            _respuesta_ok(VsolApiService._post_form(
                self.vsol, opener, base_url, "gpononudetail",
                VsolApiService.payload_descripcion_onu(pon, onuid, texto), verify_ssl))
            self._guardar_si_toca(opener, base_url, verify_ssl, "descripcion")

    async def descripcion(self, olt_id: int, pon: int, onuid: int, serial: str, texto: str, usuario=None) -> Dict[str, Any]:
        texto = _texto(texto)
        if len(texto) > LARGO_MAXIMO:
            raise ValueError(f"La OLT guarda máximo {LARGO_MAXIMO} caracteres.")
        olt = await self._olt(olt_id)
        await asyncio.to_thread(self._descripcion_sync, olt, int(pon), int(onuid), serial, texto)
        olvidar(f"vsol:{olt.id}")
        await self._bitacora(usuario, "Descripción de ONU", f"/api/olts/{olt.id}/panel/onus/{pon}/{onuid}/descripcion",
                             f"{olt.nombre} GPON0/{pon}:{onuid} · {serial} → {texto}")
        return {"ok": True, "descripcion": texto}

    def _eliminar_varias_sync(self, olt, onus):
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            for onu in onus:
                self._confirmar_serial(opener, base_url, verify_ssl, int(onu["pon"]), int(onu["onuid"]), onu["serial"])
            _respuesta_ok(VsolApiService._post_form(self.vsol, opener, base_url, "gpononuauthinfo",
                                                    payload_eliminar_varias(onus), verify_ssl))
            self._guardar_si_toca(opener, base_url, verify_ssl, "eliminar")

    async def eliminar_varias(self, olt_id: int, onus: List[Dict[str, Any]], usuario=None) -> Dict[str, Any]:
        if not onus:
            raise ValueError("Selecciona al menos una ONU.")
        olt = await self._olt(olt_id)
        await asyncio.to_thread(self._eliminar_varias_sync, olt, onus)
        olvidar(f"vsol:{olt.id}")
        await self._bitacora(usuario, "Eliminar ONUs seleccionadas", f"/api/olts/{olt.id}/panel/eliminar-varias",
                             f"{olt.nombre}: " + ", ".join(f"GPON0/{o['pon']}:{o['onuid']} {o['serial']}" for o in onus))
        return {"ok": True, "eliminadas": len(onus)}

    def _eliminar_pon_sync(self, olt, pon, solo_offline):
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            _respuesta_ok(VsolApiService._post_form(self.vsol, opener, base_url, "gpononuauthinfo",
                                                    payload_eliminar_pon(pon, solo_offline), verify_ssl))
            self._guardar_si_toca(opener, base_url, verify_ssl, "eliminar")

    async def eliminar_pon(self, olt_id: int, pon: int, solo_offline: bool, usuario=None) -> Dict[str, Any]:
        olt = await self._olt(olt_id)
        await asyncio.to_thread(self._eliminar_pon_sync, olt, int(pon), solo_offline)
        olvidar(f"vsol:{olt.id}")
        que = "Eliminar Offline" if solo_offline else "Eliminar Todo"
        await self._bitacora(usuario, f"{que} del PON", f"/api/olts/{olt.id}/panel/pon/{pon}/{'eliminar-offline' if solo_offline else 'eliminar-todo'}",
                             f"{olt.nombre} PON 0/{pon}")
        return {"ok": True}


# ------------------------------------------------------------------ detalle por pestañas
# Lecturas que hace el panel VSOL en cada pestaña de la ONU (sus mismos who de lectura).
LECTURAS_LINEA = (
    ("tcont", "gpononuTcont", {}, "tcont_list"),
    ("gemport", "gpononuGemport", {}, "gemport_list"),
    ("servicio", "gpononuService", {}, "service_list"),
    ("vlan_puerto", "gpononuPortVlan", {}, "portvlan_info"),
)
SECCIONES = ("linea", "puerto", "mac", "igmp", "host_ip", "tr069")

# Modos de autenticación del panel ("Añadir Manualmente" y el lápiz de la fila).
MODOS_AUTH = {"0": "SN", "1": "LOID", "2": "LOID + contraseña", "3": "Contraseña", "4": "SN + contraseña"}


def payload_alta_manual(pon: int, onuid: int, datos: Dict[str, Any]) -> Dict[str, Any]:
    """Lo que manda "Añadir Manualmente" del panel (gpononumanualadd, who=0)."""
    modo = _texto(datos.get("modo"))
    if modo not in MODOS_AUTH:
        raise ValueError("Elige el modo de autenticación.")
    payload: Dict[str, Any] = {"slot_select": 0, "port_select": int(pon), "slotid": 0, "portid": int(pon),
                               "onuid": int(onuid or 0), "authmode": modo}
    sn, loid, clave = _texto(datos.get("sn")).upper(), _texto(datos.get("loid")), _texto(datos.get("clave"))
    requeridos = {"0": [("gponsn", sn)], "1": [("loid", loid)], "2": [("loid", loid), ("pw", clave)],
                  "3": [("pw", clave)], "4": [("sn", sn), ("pw", clave)]}[modo]
    for campo, valor in requeridos:
        if not valor:
            raise ValueError("Falta el SN, LOID o contraseña de la ONU.")
        payload[campo] = valor
    payload.update({
        "onutype": _texto(datos.get("perfil_onu")) or "default",
        "s_linePro": _texto(datos.get("perfil_linea")) or "N/A",
        "s_srvPro": _texto(datos.get("perfil_servicio")) or "N/A",
        "s_alarmPro": _texto(datos.get("perfil_alarma")) or "N/A",
        "s_fmtPro": _texto(datos.get("perfil_formato")) or "N/A",
    })
    if _texto(datos.get("perfil_privado")) not in ("", "N/A"):
        payload["s_priPro"] = _texto(datos.get("perfil_privado"))
    payload["who"] = 0
    return payload


def payload_modificar(pon: int, onuid: int, modo: str, valor: str) -> Dict[str, Any]:
    """El lápiz de la fila: cambia el SN o LOID con el que se autoriza (gpononumodify, who=0)."""
    modo, valor = _texto(modo), _texto(valor)
    if modo not in ("0", "1") or not valor:
        raise ValueError("Escribe el SN o LOID nuevo.")
    datos: Dict[str, Any] = {"authmode": modo, "who": 0, "slotid": 0, "ponid": int(pon), "onuid": int(onuid)}
    datos["gponsn" if modo == "0" else "loid"] = valor.upper() if modo == "0" else valor
    return datos


def payload_perfil(pon: int, onuid: int, perfil: str) -> Dict[str, Any]:
    if not _texto(perfil):
        raise ValueError("Elige el perfil de ONU.")
    return {"who": 1, "slotid": 0, "ponid": int(pon), "onuid": int(onuid), "onuptype": _texto(perfil)}


class PanelOltDetalle(PanelOltService):
    """Pestañas del detalle de la ONU, versión, alta manual y modificar."""

    def _seccion_sync(self, olt, pon: int, onuid: int, seccion: str) -> Dict[str, Any]:
        onu = {"slotid": 0, "ponid": pon, "onuid": onuid}
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            def post(accion, extra=None):
                return VsolApiService._post_form(self.vsol, opener, base_url, accion, {**onu, **(extra or {})},
                                                 verify_ssl).get("data") or {}

            def puertos() -> int:
                try:
                    return max(1, min(8, int(post("gpononuMacLimit", {"who": 400}).get("eth_port_num") or 1)))
                except (TypeError, ValueError):
                    return 1

            if seccion == "linea":
                datos = {clave: post(accion, extra).get(lista) or [] for clave, accion, extra, lista in LECTURAS_LINEA}
                consulta = urllib.parse.urlencode(onu)
                datos["service_port"] = (self.vsol._request_json_sync(
                    opener, f"{base_url}/action/gpononuServicePort?{consulta}", method="GET", verify_ssl=verify_ssl,
                ).get("data") or {}).get("service_port_list") or []
                return datos
            if seccion == "puerto":
                return {"puertos": [post("gpononuPortBasic", {"selectonuport": i, "who": 300}) for i in range(1, puertos() + 1)],
                        # Estado real de cada puerto (enlace, velocidad): lo que la ONU reporta.
                        "estado": post("gpononuPortBasic", {"selectonuport": 1, "who": 200}).get("port_status_table") or []}
            if seccion == "mac":
                return {"general": post("gpononuMiscCfg", {"who": 300}), "puertos_eth": puertos()}
            if seccion == "igmp":
                return {"puertos": [post("gpononuIgmp", {"port": i, "interface": 1}) for i in range(1, puertos() + 1)]}
            if seccion == "host_ip":
                return {"cantidad": post("gpononuIphostCfg", {"who": 300}).get("iphostNum"),
                        "tabla": post("gpononuIphostCfg", {"who": 200}).get("iphost_table") or []}
            if seccion == "tr069":
                return {**post("gpononuTr069", {"who": 200}), **post("gpononuTr069", {"who": 300})}
        raise ValueError("Sección no válida.")

    async def seccion(self, olt_id: int, pon: int, onuid: int, seccion: str) -> Dict[str, Any]:
        if seccion not in SECCIONES:
            raise ValueError("Sección no válida.")
        olt = await self._olt(olt_id)
        return await asyncio.to_thread(self._seccion_sync, olt, int(pon), int(onuid), seccion)

    def _versiones_sync(self, olt, pon: int) -> List[Dict[str, Any]]:
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            datos = next((dict(urllib.parse.parse_qsl(p.decode("utf-8")))
                          for p in self.vsol._descubrir_payloads_pon_sync(opener, base_url, verify_ssl)
                          if dict(urllib.parse.parse_qsl(p.decode("utf-8"))).get("portid") == str(pon)), None)
            if not datos:
                raise ValueError("PON no encontrado.")
            filas, inicio = [], 1
            # El panel las pide de 32 en 32.
            while inicio <= 128:
                pagina = (VsolApiService._post_form(
                    self.vsol, opener, base_url, "gpononuversioninfo",
                    {"slotid": 0, "portid": pon, "port_id": datos.get("port_id", ""), "start": inicio, "end": inicio + 31},
                    verify_ssl).get("data") or {}).get("onuVersionInfo_list") or []
                filas += pagina
                if len(pagina) < 32:
                    break
                inicio += 32
            return [{"onu_id": _texto(f.get("onu_id")), "descripcion": _texto(f.get("description")),
                     "version_principal": _texto(f.get("main_software_version")),
                     "version_standby": _texto(f.get("standby_software_version")),
                     "version_hw": _texto(f.get("version"))} for f in filas]

    async def versiones(self, olt_id: int, pon: int) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._versiones_sync, await self._olt(olt_id), int(pon))

    def _perfiles_sync(self, olt, pon: int) -> Dict[str, Any]:
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            datos = self.vsol._request_json_sync(opener, f"{base_url}/action/gpononumanualadd?portid={int(pon)}",
                                                 method="GET", verify_ssl=verify_ssl).get("data") or {}
            listas = list(datos.get("gponallprofile_list") or []) + [[]] * 7
            nombres = lambda i: [_texto(p.get("name")) for p in listas[i] if _texto(p.get("name"))]
            onu = self.vsol._request_json_sync(opener, f"{base_url}/action/gpononuprofileinfo",
                                               method="GET", verify_ssl=verify_ssl).get("data") or {}
            return {
                "onu_id_sugerido": _texto(datos.get("manual_add_onuid")),
                "perfil_linea": nombres(0), "perfil_servicio": nombres(1), "perfil_alarma": nombres(2),
                "perfil_onu": [_texto(p.get("onuprofile_name")) for p in onu.get("gpon_onu_profile_list") or []] or nombres(3),
                "perfil_privado": nombres(4), "perfil_formato": nombres(5),
                "modos": MODOS_AUTH,
            }

    async def perfiles(self, olt_id: int, pon: int) -> Dict[str, Any]:
        return await asyncio.to_thread(self._perfiles_sync, await self._olt(olt_id), int(pon))

    def _alta_sync(self, olt, payload):
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            _respuesta_ok(VsolApiService._post_form(self.vsol, opener, base_url, "gpononumanualadd", payload, verify_ssl))
            self._guardar_si_toca(opener, base_url, verify_ssl, "alta")

    async def alta_manual(self, olt_id: int, pon: int, datos: Dict[str, Any], usuario=None) -> Dict[str, Any]:
        olt = await self._olt(olt_id)
        payload = payload_alta_manual(pon, int(datos.get("onuid") or 0), datos)
        await asyncio.to_thread(self._alta_sync, olt, payload)
        olvidar(f"vsol:{olt.id}")
        ident = payload.get("gponsn") or payload.get("sn") or payload.get("loid") or "(contraseña)"
        await self._bitacora(usuario, "Añadir ONU manualmente", f"/api/olts/{olt.id}/panel/pon/{pon}/alta",
                             f"{olt.nombre} PON 0/{pon} · ONU {payload['onuid'] or 'automática'} · {ident} · perfil {payload['onutype']}")
        return {"ok": True}

    def _modificar_sync(self, olt, pon, onuid, serial, payloads):
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            self._confirmar_serial(opener, base_url, verify_ssl, pon, onuid, serial)
            for payload in payloads:
                _respuesta_ok(VsolApiService._post_form(self.vsol, opener, base_url, "gpononumodify", payload, verify_ssl))
            self._guardar_si_toca(opener, base_url, verify_ssl, "modificar")

    async def modificar(self, olt_id: int, pon: int, onuid: int, serial: str, datos: Dict[str, Any], usuario=None) -> Dict[str, Any]:
        olt = await self._olt(olt_id)
        payloads, cambios = [], []
        if _texto(datos.get("valor")):
            payloads.append(payload_modificar(pon, onuid, _texto(datos.get("modo")) or "0", datos.get("valor")))
            cambios.append(f"{'SN' if payloads[-1].get('gponsn') else 'LOID'} → {payloads[-1].get('gponsn') or payloads[-1].get('loid')}")
        if _texto(datos.get("perfil")):
            payloads.append(payload_perfil(pon, onuid, datos.get("perfil")))
            cambios.append(f"perfil → {_texto(datos.get('perfil'))}")
        if not payloads:
            raise ValueError("No hay cambios que aplicar.")
        await asyncio.to_thread(self._modificar_sync, olt, int(pon), int(onuid), serial, payloads)
        olvidar(f"vsol:{olt.id}")
        await self._bitacora(usuario, "Modificar ONU", f"/api/olts/{olt.id}/panel/onus/{pon}/{onuid}/modificar",
                             f"{olt.nombre} GPON0/{pon}:{onuid} · {serial} · " + "; ".join(cambios))
        return {"ok": True}


# ------------------------------------------------------------------ edición de pestañas
# operación → (acción /action/, who, campos que se aceptan del formulario, nombre para la bitácora).
# Son los mismos formularios del panel VSOL; cualquier otro campo se descarta.
OPERACIONES: Dict[str, Any] = {
    "puerto_basico": ("gpononuPortBasic", 0, ("selectonuport", "en_port", "loop_detect", "selectPortSpeed", "mac_limit"), "Puerto: datos básicos"),
    "puerto_subida": ("gpononuPortBasic", 3, ("selectonuport", "us_cir", "us_pir"), "Puerto: límite de subida"),
    "puerto_bajada": ("gpononuPortBasic", 4, ("selectonuport", "ds_cir", "ds_pir"), "Puerto: límite de bajada"),
    "igmp_maximo": ("gpononuIgmp", 0, ("portnum", "porttype", "max"), "IGMP: máximo de grupos"),
    "igmp_ancho": ("gpononuIgmp", 1, ("portnum1", "porttype1", "bandwidth", "enforce"), "IGMP: ancho de banda"),
    "igmp_perfil": ("gpononuIgmp", 2, ("portnum2", "porttype2", "pname"), "IGMP: asignar perfil"),
    "igmp_perfil_quitar": ("gpononuIgmp", 3, ("portnum2", "porttype2", "pname"), "IGMP: quitar perfil"),
    "iphost_agregar": ("gpononuIphostCfg", 0, ("selectonuport", "iphost_desc", "selectipmode", "ipAddr", "mask", "gateway", "dns1", "dns2"), "Host IP: agregar"),
    "iphost_borrar": ("gpononuIphostCfg", 1, ("selectonuport", "iphostNum"), "Host IP: borrar"),
    "iphost_vlan": ("gpononuIphostCfg", 2, ("selectonuport", "vlan", "priority"), "Host IP: VLAN"),
    "tr069_gestion": ("gpononuTr069", 0, ("tr069_mng_en", "acsServerAddress", "acsServerUsername", "acsServerPassword", "inform",
                                          "informIntervalTime", "reverse_username", "reverse_password", "tr069_id", "certificate"), "TR069: gestión"),
    "tr069_stun": ("gpononuTr069", 1, ("tr069_stun_en", "stunServerAddress", "stunServerPort", "stunUsername", "stunPassword"), "TR069: STUN"),
    "mac_general": ("gpononuMiscCfg", 8, ("macAging", "ponMLimit", "lanMLimit"), "MAC: límites"),
    "mac_limpiar": ("gpononuMiscCfg", 9, (), "MAC: limpiar tabla"),
    "tcont_agregar": ("gpononuTcont", 0, ("tcontid", "tcontname", "dbaprofile"), "Línea: agregar T-CONT"),
    "tcont_borrar": ("gpononuTcont", 1, ("tcontid", "tcontname", "dbaprofile"), "Línea: borrar T-CONT"),
    "gemport_agregar": ("gpononuGemport", 0, ("gemid", "gemname", "tcontid", "us_cir", "us_pir", "ds_cir", "ds_pir", "dstraffic",
                                              "gemcos", "gemUpQueueMapId", "gemDownQueueMapId", "gemstate"), "Línea: agregar GEM port"),
    "gemport_borrar": ("gpononuGemport", 1, ("gemid", "gemname", "tcontid"), "Línea: borrar GEM port"),
    "servicio_agregar": ("gpononuService", 0, ("servicename", "gemid", "vlanmode", "vlanlist", "coslist", "portMode", "portNum"), "Línea: agregar servicio"),
    "servicio_borrar": ("gpononuService", 1, ("servicename", "gemid", "vlanmode", "vlanlist"), "Línea: borrar servicio"),
    "service_port_agregar": ("gpononuServicePort", 0, ("servicemode", "srvid", "gemid", "description", "bvid", "evid", "tvid", "tsvid",
                                                       "tcos", "tscos", "upri"), "Línea: agregar service port"),
    "vlan_puerto_agregar": ("gpononuPortVlan", 0, ("porttype", "portnum", "vlantype", "vlanid", "vlanpri", "defvlan", "defpri",
                                                   "cvlanid", "cvlanpri", "svlanid", "svlanpri"), "Línea: agregar VLAN de puerto"),
    "vlan_puerto_borrar": ("gpononuPortVlan", 1, ("name", "vlantype", "porttype"), "Línea: borrar VLAN de puerto"),
}
# Borrar un service port es un GET con who=1 en el panel.
OPERACION_BORRAR_SERVICE_PORT = "service_port_borrar"
# Campos que el panel manda con "N/A" si quedan vacíos.
VACIOS_NA = ("gemcos", "gemUpQueueMapId", "gemDownQueueMapId", "tsvid", "tcos", "tscos", "upri", "description")


def payload_operacion(operacion: str, pon: int, onuid: int, datos: Dict[str, Any]) -> Dict[str, Any]:
    if operacion not in OPERACIONES:
        raise ValueError("Operación no válida.")
    accion, who, campos, _nombre = OPERACIONES[operacion]
    payload: Dict[str, Any] = {}
    for campo in campos:
        valor = _texto((datos or {}).get(campo))
        if valor == "" and campo in VACIOS_NA:
            valor = "N/A"
        if valor != "":
            payload[campo] = valor
    payload.update({"slotid": 0, "ponid": int(pon), "onuid": int(onuid), "who": who})
    if accion == "gpononuServicePort":
        payload["Commit"] = "Commit"
    return payload


class PanelOltEdicion(PanelOltDetalle):
    def _opciones_sync(self, opener, base_url, verify_ssl, accion: str) -> List[str]:
        """Nombres de perfiles (DBA, IGMP) para los selectores."""
        try:
            datos = self.vsol._request_json_sync(opener, f"{base_url}/action/{accion}", method="GET", verify_ssl=verify_ssl).get("data") or {}
        except Exception:
            return []
        nombres = []
        for lista in datos.values():
            for item in lista if isinstance(lista, list) else []:
                nombre = next((_texto(v) for k, v in item.items() if "name" in k.lower() and _texto(v)), "") if isinstance(item, dict) else ""
                if nombre and nombre not in nombres:
                    nombres.append(nombre)
        return nombres

    def _seccion_sync(self, olt, pon, onuid, seccion):
        datos = super()._seccion_sync(olt, pon, onuid, seccion)
        if seccion in ("linea", "igmp"):
            with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
                if seccion == "linea":
                    datos["perfiles_dba"] = self._opciones_sync(opener, base_url, verify_ssl, "gpondbaprofileinfo")
                else:
                    datos["perfiles_igmp"] = self._opciones_sync(opener, base_url, verify_ssl, "gponigmpProfile")
        return datos

    def _configurar_sync(self, olt, pon, onuid, serial, operacion, datos):
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            self._confirmar_serial(opener, base_url, verify_ssl, pon, onuid, serial)
            if operacion == OPERACION_BORRAR_SERVICE_PORT:
                puerto = int(_texto(datos.get("srvid")) or 0)
                if puerto <= 0:
                    raise ValueError("Falta el service port a borrar.")
                consulta = urllib.parse.urlencode({"who": 1, "slotid": 0, "ponid": pon, "onuid": onuid, "sport": puerto})
                respuesta = self.vsol._request_json_sync(opener, f"{base_url}/action/gpononuServicePort?{consulta}",
                                                         method="GET", verify_ssl=verify_ssl)
            else:
                accion = OPERACIONES[operacion][0]
                respuesta = VsolApiService._post_form(self.vsol, opener, base_url, accion,
                                                      payload_operacion(operacion, pon, onuid, datos), verify_ssl)
            _respuesta_ok(respuesta)
            self._guardar_si_toca(opener, base_url, verify_ssl, operacion)

    async def configurar(self, olt_id: int, pon: int, onuid: int, serial: str, operacion: str,
                         datos: Dict[str, Any], usuario=None) -> Dict[str, Any]:
        if operacion != OPERACION_BORRAR_SERVICE_PORT:
            payload_operacion(operacion, pon, onuid, datos)  # valida antes de entrar a la OLT
        olt = await self._olt(olt_id)
        await asyncio.to_thread(self._configurar_sync, olt, int(pon), int(onuid), serial, operacion, datos or {})
        olvidar(f"vsol:{olt.id}")
        nombre = "Línea: borrar service port" if operacion == OPERACION_BORRAR_SERVICE_PORT else OPERACIONES[operacion][3]
        ocultos = ("Password", "password", "pw")
        detalle = ", ".join(f"{k}={'***' if any(o in k for o in ocultos) else v}" for k, v in (datos or {}).items() if _texto(v))
        await self._bitacora(usuario, nombre, f"/api/olts/{olt.id}/panel/onus/{pon}/{onuid}/config/{operacion}",
                             f"{olt.nombre} GPON0/{pon}:{onuid} · {serial} · {detalle}")
        return {"ok": True}
