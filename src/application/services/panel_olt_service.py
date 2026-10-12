"""Panel de la OLT VSOL dentro del sistema: la misma lista de ONU y las mismas acciones.

Cada acción manda a /action/ lo mismo que el panel web de la VSOL (sacado de
su código). Nunca se le da "Save" a la OLT: tras un apagón debe arrancar
limpia (ver guardar_en_olt). Antes de tocar una ONU se confirma que en ese
PON y número sigue el serial que el usuario ve en pantalla.
"""

import asyncio
import urllib.parse
from typing import Any, Dict, List, Optional

from src.application.services.cache_olt import olvidar
from src.application.services.descripcion_onu_service import DescripcionOnuService, LARGO_MAXIMO
from src.application.services.vsol_api_service import VsolApiService, guardar_en_olt
from src.infrastructure.models import LogActividadModel, OLTModel

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

    def _lista_sync(self, olt: OLTModel, pon: Optional[int], buscar: Optional[str]) -> List[Dict[str, Any]]:
        with self.vsol._sesion(olt) as (opener, base_url, verify_ssl):
            todos = [dict(urllib.parse.parse_qsl(p.decode("utf-8")))
                     for p in self.vsol._descubrir_payloads_pon_sync(opener, base_url, verify_ssl)]
            leer = lambda accion, datos, lista: (
                (VsolApiService._post_form(self.vsol, opener, base_url, accion, datos, verify_ssl).get("data") or {})
                .get(lista) or []
            )
            if buscar:
                # "Buscar en TODAS las PON" del panel: who=10 con el texto.
                auth = leer("gpononuauthinfo", {**todos[0], "who": "10", "searchONU": buscar}, "onuAuth_list") if todos else []
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
        filas = await asyncio.to_thread(self._lista_sync, olt, pon, (buscar or "").strip() or None)
        clientes = await DescripcionOnuService(self.db, self.vsol)._clientes_por_onu()
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
                    "online": _texto(auth.get("state")) == "1", "detalle": detalle, "optica": optica}

    async def detalle(self, olt_id: int, pon: int, onuid: int) -> Dict[str, Any]:
        return await asyncio.to_thread(self._detalle_sync, await self._olt(olt_id), int(pon), int(onuid))

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
