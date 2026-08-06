import asyncio
import json
import ssl
import urllib.parse
import urllib.request
import http.cookiejar
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.infrastructure.models import OLTModel, ClienteModel


class VsolApiService:
    """Cliente de lectura para la API web JSON de OLT VSOL."""

    def __init__(self, db: AsyncSession):
        self.db = db

    def _base_url(self, olt: OLTModel) -> str:
        protocol = getattr(olt, "api_protocol", None) or "https"
        port = getattr(olt, "api_port", None) or (443 if protocol == "https" else 80)
        default_port = (protocol == "https" and int(port) == 443) or (protocol == "http" and int(port) == 80)
        host = str(olt.ip).strip()
        return f"{protocol}://{host}" if default_port else f"{protocol}://{host}:{port}"

    def _crear_opener_sync(self, verify_ssl: bool = False) -> urllib.request.OpenerDirector:
        jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(jar)]

        if not verify_ssl:
            handlers.append(
                urllib.request.HTTPSHandler(
                    context=ssl._create_unverified_context()
                )
            )

        return urllib.request.build_opener(*handlers)

    def _request_json_sync(
        self,
        opener: urllib.request.OpenerDirector,
        url: str,
        method: str = "GET",
        data: Optional[bytes] = None,
        verify_ssl: bool = False,
    ) -> Dict[str, Any]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "FdezNet-VSOL-API/1.0",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        context = ssl._create_unverified_context() if url.startswith("https://") and not verify_ssl else None

        if context is not None:
            response = opener.open(req, timeout=15)
        else:
            response = opener.open(req, timeout=15)

        raw = response.read().decode("utf-8", errors="replace")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"La OLT no devolvió JSON válido: {raw[:180]}") from exc

    def _descubrir_payloads_pon_sync(
        self,
        opener: urllib.request.OpenerDirector,
        base_url: str,
        verify_ssl: bool,
    ) -> List[bytes]:
        # Descubre los puertos PON desde /action/portState.
        #
        # VSOL no siempre devuelve todas las ONUs con POST vacío. En OLTs
        # multi-PON hay que consultar cada PON con:
        #   slotid=0&portid=4&onuid=1&port_id=8
        #
        # Donde:
        # - portid es el número lógico del PON: GPON0/4 -> 4
        # - port_id es el id interno que devuelve portState: GPON0/4 -> 8
        payloads: List[bytes] = []

        try:
            port_state = self._request_json_sync(
                opener,
                f"{base_url}/action/portState",
                method="GET",
                verify_ssl=verify_ssl,
            )
        except Exception:
            port_state = {}

        slot_list = port_state.get("data", {}).get("slot_list", []) or []

        for slot in slot_list:
            slotid = str(slot.get("slotid", "0"))
            pon_cfg_list = slot.get("pon_cfg_list", []) or []

            for pon in pon_cfg_list:
                intf_name = str(pon.get("intfName", "")).strip()
                internal_port_id = str(pon.get("portid", "")).strip()

                if not internal_port_id:
                    continue

                logical_port = None
                if "/" in intf_name:
                    logical_port = intf_name.split("/")[-1].strip()

                if not logical_port:
                    try:
                        # En equipos VSOL GPON 4 puertos suele ser:
                        # GPON0/1 => port_id 5, GPON0/4 => port_id 8
                        logical_port = str(int(internal_port_id) - 4)
                    except Exception:
                        logical_port = internal_port_id

                if not logical_port or logical_port == "0":
                    continue

                payload = urllib.parse.urlencode({
                    "slotid": slotid,
                    "portid": logical_port,
                    "onuid": "1",
                    "port_id": internal_port_id,
                }).encode("utf-8")

                payloads.append(payload)

        # Fallback para OLTs de 1 PON o firmwares que sí responden todo con POST vacío.
        if not payloads:
            payloads.append(b"")

        return payloads

    def _login_y_consultar_sync(self, olt: OLTModel) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        usuario = getattr(olt, "api_user", None)
        password = getattr(olt, "api_password", None)

        if not usuario or not password:
            raise ValueError("La OLT no tiene configurado api_user/api_password.")

        base_url = self._base_url(olt)
        verify_ssl = bool(getattr(olt, "api_verify_ssl", False))

        opener = self._crear_opener_sync(verify_ssl=verify_ssl)

        login_payload = urllib.parse.urlencode({
            "user": usuario,
            "pass": password,
            "verification_code": "",
            "button": "Login",
            "who": "100",
        }).encode("utf-8")

        login_resp = self._request_json_sync(
            opener,
            f"{base_url}/action/main",
            method="POST",
            data=login_payload,
            verify_ssl=verify_ssl,
        )

        if str(login_resp.get("retcode", "0")) not in ("0", "None"):
            raise ValueError(f"Login VSOL rechazado: {login_resp}")

        user_resp = self._request_json_sync(
            opener,
            f"{base_url}/action/user",
            method="GET",
            verify_ssl=verify_ssl,
        )

        if str(user_resp.get("retcode")) != "0":
            raise ValueError(f"No se pudo validar sesión VSOL: {user_resp}")

        payloads_pon = self._descubrir_payloads_pon_sync(opener, base_url, verify_ssl)

        auth_all: List[Dict[str, Any]] = []
        optical_all: List[Dict[str, Any]] = []
        status_all: List[Dict[str, Any]] = []

        for payload in payloads_pon:
            auth = self._request_json_sync(
                opener,
                f"{base_url}/action/gpononuauthinfo",
                method="POST",
                data=payload,
                verify_ssl=verify_ssl,
            )

            optical = self._request_json_sync(
                opener,
                f"{base_url}/action/gpononuopticalinfo",
                method="POST",
                data=payload,
                verify_ssl=verify_ssl,
            )

            status = self._request_json_sync(
                opener,
                f"{base_url}/action/gpononustatusinfo",
                method="POST",
                data=payload,
                verify_ssl=verify_ssl,
            )

            auth_all.extend(auth.get("data", {}).get("onuAuth_list", []) or [])
            optical_all.extend(optical.get("data", {}).get("onuOpticalInfo_list", []) or [])
            status_all.extend(status.get("data", {}).get("onuStatus_list", []) or [])

        return (
            {"retcode": "0", "data": {"onuAuth_list": auth_all}},
            {"retcode": "0", "data": {"onuOpticalInfo_list": optical_all}},
            {"retcode": "0", "data": {"onuStatus_list": status_all}},
        )

    async def _consultar_api(self, olt: OLTModel) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        return await asyncio.to_thread(self._login_y_consultar_sync, olt)

    @staticmethod
    def _normalizar_sn(valor: Any) -> str:
        return str(valor or "").strip().upper()

    @staticmethod
    def _parse_float(valor: Any) -> Optional[float]:
        try:
            texto = str(valor).strip()
            if texto.upper() in ("", "N/A", "NULL", "NONE", "--"):
                return None
            return float(texto)
        except Exception:
            return None

    @staticmethod
    def _estado_por_datos(item: Dict[str, Any]) -> str:
        state = str(item.get("state", "")).strip()
        phase = str(item.get("phase_state", "")).strip().lower()
        rx = VsolApiService._parse_float(item.get("rx_power"))
        if state == "1" and phase == "working" and rx is not None:
            return "online"
        if state == "1" and phase == "working":
            return "online"
        return "offline"

    @staticmethod
    def _recomendacion(rx_power: Any, estado: str) -> str:
        pwr = VsolApiService._parse_float(rx_power)
        if estado != "online" or pwr is None:
            return "La OLT no ve la ONU o no reporta potencia. Verificar energía del cliente, patchcord o ruptura de fibra."
        if pwr < -27.0:
            return "Señal CRÍTICA. Revisar conectores, dobleces, empalmes o divisor óptico."
        if pwr < -25.0:
            return "Señal aceptable, pero al límite. Conviene revisar margen óptico."
        if pwr > -8.0:
            return "Señal muy alta. Verificar saturación o distancia muy corta."
        return "¡Señal EXCELENTE! Instalación óptima."


    def _ordenar_onus_por_mejor_estado(self, onus: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # En algunas VSOL la misma ONU puede aparecer como registro viejo/offline
        # en un PON y como registro actual/online en otro PON.
        # Para diagnóstico se prefiere:
        # online > working > rx_power numérico > mejor potencia válida.
        def score(onu: Dict[str, Any]) -> tuple:
            estado = str(onu.get("estado_fisico") or onu.get("status") or "").lower()
            phase = str(onu.get("phase_state") or "").lower()
            rx = self._parse_float(onu.get("rx_power"))

            return (
                1 if estado == "online" else 0,
                1 if phase == "working" else 0,
                1 if rx is not None else 0,
                rx if rx is not None else -999.0,
            )

        return sorted(onus, key=score, reverse=True)

    def _deduplicar_onus_por_serial(self, onus: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        mejor_por_serial: Dict[str, Dict[str, Any]] = {}

        for onu in onus:
            serial = self._normalizar_sn(onu.get("identificador") or onu.get("serial"))
            if not serial:
                continue

            actual = mejor_por_serial.get(serial)
            if not actual:
                mejor_por_serial[serial] = onu
                continue

            mejor = self._ordenar_onus_por_mejor_estado([actual, onu])[0]
            mejor_por_serial[serial] = mejor

        return list(mejor_por_serial.values())


    def _unificar_onus(self, auth: Dict[str, Any], optical: Dict[str, Any], status: Dict[str, Any]) -> List[Dict[str, Any]]:
        auth_list = auth.get("data", {}).get("onuAuth_list", []) or []
        optical_list = optical.get("data", {}).get("onuOpticalInfo_list", []) or []
        status_list = status.get("data", {}).get("onuStatus_list", []) or []

        optical_by_onu = {str(item.get("onu_id", "")).strip(): item for item in optical_list if item.get("onu_id")}
        status_by_onu = {str(item.get("onu_id", "")).strip(): item for item in status_list if item.get("onu_id")}
        status_by_sn = {self._normalizar_sn(item.get("Info")): item for item in status_list if item.get("Info")}

        resultado = []
        for auth_item in auth_list:
            onu_id = str(auth_item.get("onu_id", "")).strip()
            serial = self._normalizar_sn(auth_item.get("info") or auth_item.get("Info"))
            optical_item = optical_by_onu.get(onu_id, {})
            status_item = status_by_onu.get(onu_id) or status_by_sn.get(serial, {})

            unificada = {
                "onu_id": onu_id,
                "slot_id": auth_item.get("slot_id"),
                "pon_id": auth_item.get("pon_id"),
                "onuid": auth_item.get("onuid"),
                "identificador": serial,
                "serial": serial,
                "modelo": auth_item.get("model"),
                "profile": auth_item.get("profile"),
                "mode": auth_item.get("mode") or status_item.get("Mode"),
                "state": auth_item.get("state"),
                "active_action_state": auth_item.get("active_action_state"),
                "admin_state": status_item.get("admin_state"),
                "omcc_state": status_item.get("omcc_state"),
                "phase_state": status_item.get("phase_state"),
                "alive_time": status_item.get("alive_time"),
                "last_register_time": status_item.get("last_register_time"),
                "last_deregister_time": status_item.get("last_deregister_time"),
                "last_deregister_reason": status_item.get("last_deregister_reason"),
                "rx_power": optical_item.get("rx_power"),
                "tx_power": optical_item.get("tx_power"),
                "rx_state": optical_item.get("rx_state"),
                "tx_state": optical_item.get("tx_state"),
                "description": auth_item.get("description") or status_item.get("description") or optical_item.get("description"),
            }
            estado = self._estado_por_datos(unificada)
            unificada["estado_fisico"] = estado
            unificada["status"] = estado
            unificada["recomendacion"] = self._recomendacion(unificada.get("rx_power"), estado)
            resultado.append(unificada)
        return resultado

    async def listar_onus_unificadas(self, olt_id: int) -> Dict[str, Any]:
        olt = await self.db.get(OLTModel, olt_id)
        if not olt:
            raise ValueError("OLT no encontrada.")
        auth, optical, status = await self._consultar_api(olt)
        onus = self._deduplicar_onus_por_serial(
            self._unificar_onus(auth, optical, status)
        )
        return {
            "olt_id": olt.id,
            "olt_nombre": olt.nombre,
            "tecnologia": olt.tecnologia,
            "origen": "vsol_api",
            "total_onus": len(onus),
            "onus": onus,
        }

    async def monitorear_olt_api(self, olt_id: int) -> Dict[str, Any]:
        olt = await self.db.get(OLTModel, olt_id)
        if not olt:
            raise ValueError("OLT no encontrada.")

        stmt = select(ClienteModel).options(selectinload(ClienteModel.onu_asignada)).where(ClienteModel.olt_id == olt_id)
        clientes_db = (await self.db.execute(stmt)).scalars().all()
        mapa_bd = {}
        for cliente in clientes_db:
            sn = self._normalizar_sn(cliente.onu_asignada.identificador if cliente.onu_asignada else None)
            if sn:
                mapa_bd[sn] = cliente

        def datos_cliente(cliente: ClienteModel) -> Dict[str, Any]:
            """Datos de CRM que Radar necesita sin depender del listado paginado."""
            return {
                "id_cliente": cliente.id,
                "nombre": cliente.nombre,
                "cedula": cliente.cedula,
                "telefono": cliente.telefono,
                "direccion": cliente.direccion,
                "correo": cliente.correo,
                "ip_asignada": cliente.ip_asignada,
                "user_pppoe": cliente.user_pppoe,
                "mac_address": cliente.mac_address,
                "olt_id": cliente.olt_id,
                "onu_id_inventario": cliente.onu_id,
                "caja_nap_id": cliente.caja_nap_id,
                "puerto_nap": cliente.puerto_nap,
                "estado_fdeznet": cliente.estado,
            }

        api_data = await self.listar_onus_unificadas(olt_id)
        onus_api = api_data["onus"]
        resultados = {
            "olt_nombre": olt.nombre,
            "tecnologia": olt.tecnologia,
            "origen": "vsol_api",
            "resumen": {"activos": 0, "caidos": 0, "desconocidos": 0},
            "clientes_activos": [],
            "clientes_caidos": [],
            "onus_desconocidas": [],
            "onus_api": onus_api,
        }
        onus_encontradas = set()
        for onu in onus_api:
            sn = self._normalizar_sn(onu.get("identificador"))
            if not sn:
                continue
            onus_encontradas.add(sn)
            rx_power = onu.get("rx_power")
            rx_text = f"{rx_power} dBm" if rx_power not in (None, "", "N/A") else "LOS / Sin señal"
            if sn in mapa_bd:
                cliente = mapa_bd[sn]
                dato = {
                    **datos_cliente(cliente),
                    "identificador": sn,
                    "onu_id": onu.get("onu_id"),
                    "modelo": onu.get("modelo"),
                    "profile": onu.get("profile"),
                    "rx_power": rx_text,
                    "tx_power": onu.get("tx_power"),
                    "phase_state": onu.get("phase_state"),
                    "alive_time": onu.get("alive_time"),
                    "estado_fisico": onu.get("estado_fisico"),
                    "recomendacion": onu.get("recomendacion"),
                }
                if onu.get("estado_fisico") == "online":
                    resultados["clientes_activos"].append(dato)
                    resultados["resumen"]["activos"] += 1
                else:
                    resultados["clientes_caidos"].append(dato)
                    resultados["resumen"]["caidos"] += 1
            else:
                resultados["onus_desconocidas"].append({
                    "identificador": sn,
                    "onu_id": onu.get("onu_id"),
                    "modelo": onu.get("modelo"),
                    "rx_power": rx_text,
                    "status": onu.get("estado_fisico"),
                    "profile": onu.get("profile"),
                })
                resultados["resumen"]["desconocidos"] += 1

        for sn, cliente in mapa_bd.items():
            if sn not in onus_encontradas:
                resultados["clientes_caidos"].append({
                    **datos_cliente(cliente),
                    "identificador": sn,
                    "rx_power": "LOS / Sin señal",
                    "estado_fisico": "offline",
                    "recomendacion": "La OLT no reportó esta ONU por API. Verificar energía, serial o puerto PON.",
                })
                resultados["resumen"]["caidos"] += 1
        return resultados

    async def monitorear_cliente_individual_api(self, cliente_id: int) -> Dict[str, Any]:
        stmt = select(ClienteModel).options(
            selectinload(ClienteModel.onu_asignada)
        ).where(ClienteModel.id == cliente_id)

        cliente = (await self.db.execute(stmt)).scalar_one_or_none()

        if not cliente:
            raise ValueError("Cliente no encontrado.")
        return await self.monitorear_objetivo(cliente)

    async def monitorear_objetivo(self, objetivo) -> Dict[str, Any]:
        """Consulta la ONU del cliente legado o de un servicio concreto."""
        onu_asignada = getattr(objetivo, "onu", None) or getattr(
            objetivo,
            "onu_asignada",
            None,
        )
        sn_cliente = self._normalizar_sn(
            onu_asignada.identificador if onu_asignada else None
        )

        if not objetivo.olt_id or not sn_cliente:
            raise ValueError(
                "Al servicio le falta OLT o Identificador de ONU (SN)."
            )

        api_data = await self.listar_onus_unificadas(objetivo.olt_id)
        cliente = getattr(objetivo, "cliente", None) or objetivo
        servicio_id = (
            objetivo.id
            if getattr(objetivo, "cliente_id", None) is not None
            else None
        )

        candidatos = [
            onu
            for onu in api_data["onus"]
            if self._normalizar_sn(onu.get("identificador") or onu.get("serial")) == sn_cliente
        ]

        if candidatos:
            onu = self._ordenar_onus_por_mejor_estado(candidatos)[0]
            rx = onu.get("rx_power")
            return {
                "cliente_id": cliente.id,
                "servicio_id": servicio_id,
                "nombre": cliente.nombre,
                "identificador": sn_cliente,
                "onu_id": onu.get("onu_id"),
                "modelo": onu.get("modelo"),
                "profile": onu.get("profile"),
                "potencia": f"{rx} dBm" if rx not in (None, "", "N/A") else "LOS / Sin señal",
                "rx_power": onu.get("rx_power"),
                "tx_power": onu.get("tx_power"),
                "estado_fisico": onu.get("estado_fisico"),
                "phase_state": onu.get("phase_state"),
                "admin_state": onu.get("admin_state"),
                "omcc_state": onu.get("omcc_state"),
                "alive_time": onu.get("alive_time"),
                "last_register_time": onu.get("last_register_time"),
                "last_deregister_time": onu.get("last_deregister_time"),
                "last_deregister_reason": onu.get("last_deregister_reason"),
                "tecnologia": api_data.get("tecnologia"),
                "origen": "vsol_api",
                "coincidencias_serial": len(candidatos),
                "recomendacion": onu.get("recomendacion"),
            }

        return {
            "cliente_id": cliente.id,
            "servicio_id": servicio_id,
            "nombre": cliente.nombre,
            "identificador": sn_cliente,
            "potencia": "LOS / Sin señal",
            "estado_fisico": "offline",
            "tecnologia": api_data.get("tecnologia"),
            "origen": "vsol_api",
            "coincidencias_serial": 0,
            "recomendacion": "La OLT no reportó esta ONU por API. Verificar energía, serial o ruptura de fibra.",
        }
