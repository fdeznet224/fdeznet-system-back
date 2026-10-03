import re
from urllib.parse import quote

import requests
import urllib3

from src.utils.mikrotik import (
    KBPS_CORTE_WHATSAPP_DEFAULT,
    RANGOS_META_WHATSAPP,
    normalizar_mac,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LISTA_CORTE = "CORTE_FDEZNET"
LISTA_WHATSAPP = "WHATSAPP_FDEZNET"
MARCA_WA_SUBIDA = "fdeznet-corte-wa-subida"
MARCA_WA_BAJADA = "fdeznet-corte-wa-bajada"
COLA_TIPO_WA_SUBIDA = "fdeznet-corte-wa-subida"
COLA_TIPO_WA_BAJADA = "fdeznet-corte-wa-bajada"
COLA_ARBOL_WA_SUBIDA = "FDEZNET-CORTE-WA-SUBIDA"
COLA_ARBOL_WA_BAJADA = "FDEZNET-CORTE-WA-BAJADA"

class MikroTikService:
    def __init__(self, ip, user, password, port=80):
        try:
            self.port = int(port)
        except:
            self.port = 80
            
        if self.port in [8291, 8728]: 
            self.port = 80 
            
        self.base_url = f"http://{ip}:{self.port}/rest" 
        self.auth = (user, password)
        self.timeout = 10 

    def _request(self, method, endpoint, payload=None, raise_on_error=False):
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.request(
                method, url, auth=self.auth, json=payload, timeout=self.timeout, verify=False
            )
            if response.status_code in [200, 201, 204]:
                return response.json() if response.text else True
            if response.status_code == 404:
                if raise_on_error:
                    raise RuntimeError(
                        f"MikroTik respondió 404 en {endpoint}"
                    )
                return None
            if response.status_code >= 400:
                mensaje = (
                    f"MK Error {response.status_code} en {endpoint}: "
                    f"{response.text}"
                )
                print(f"⚠️ {mensaje}")
                if raise_on_error:
                    raise RuntimeError(mensaje)
                return None
            return None
        except Exception as e:
            print(f"❌ Error Conexión MK ({endpoint}): {e}")
            if raise_on_error:
                raise
            return None

    def probar_conexion(self):
        try:
            res = self._request("GET", "/system/identity")
            if res:
                nombre = res.get('name') if isinstance(res, dict) else res[0].get('name')
                return True, f"Conectado a OLT/Router: {nombre}"
            return False, "Falló la autenticación."
        except Exception as e:
            return False, str(e)

    # ==========================================
    #  1. GESTIÓN DE PERFILES FTTH (PLANES)
    # ==========================================
    def crear_actualizar_perfil_pppoe(self, nombre_plan: str, velocidad: str, local_addr="10.0.0.1"):
        payload = {
            "name": nombre_plan,
            "rate-limit": velocidad,
            "only-one": "default", 
            "dns-server": "8.8.8.8,1.1.1.1", 
            "comment": "ISP-FTTH"
        }
        res = self._request("GET", f"/ppp/profile?name={nombre_plan}")
        
        if res and isinstance(res, list) and len(res) > 0:
            return self._request("PATCH", f"/ppp/profile/{res[0]['.id']}", {"rate-limit": velocidad})
        else:
            payload["local-address"] = local_addr
            return self._request("PUT", "/ppp/profile", payload)

    # 🔥 NUEVA FUNCIÓN DE RENOMBRADO (Adaptada a REST API) 🔥
    def renombrar_perfil_ppp(self, nombre_viejo: str, nombre_nuevo: str):
        """Busca el perfil por su nombre viejo y lo actualiza al nuevo nombre"""
        res = self._request("GET", f"/ppp/profile?name={nombre_viejo}")
        if res and isinstance(res, list) and len(res) > 0:
            return self._request("PATCH", f"/ppp/profile/{res[0]['.id']}", {"name": nombre_nuevo})
        else:
            print(f"⚠️ MikroTik: No se encontró el perfil '{nombre_viejo}' para renombrar.")
            return None

    def eliminar_perfil_pppoe(self, nombre_plan: str):
        """Elimina un profile de PPP del MikroTik"""
        res = self._request("GET", f"/ppp/profile?name={nombre_plan}")
        if res and isinstance(res, list) and len(res) > 0:
            self._request("DELETE", f"/ppp/profile/{res[0]['.id']}")
            return True
        return False

    # ==========================================
    #  2. GESTIÓN DE ONUs / CLIENTES (SECRETS)
    # ==========================================
    def crear_actualizar_pppoe(self, user, password, profile, remote_address=None, comment="ISP-Manager"):
        payload = {
            "name": user,
            "password": password,
            "profile": profile,
            "service": "pppoe",
            "comment": comment
        }
        if remote_address and remote_address != '0.0.0.0':
            payload["remote-address"] = remote_address

        res = self._request("GET", f"/ppp/secret?name={user}")
        if res and isinstance(res, list) and len(res) > 0:
            resultado = self._request(
                "PATCH",
                f"/ppp/secret/{res[0]['.id']}",
                payload,
                raise_on_error=True,
            )
        else:
            resultado = self._request(
                "PUT",
                "/ppp/secret",
                payload,
                raise_on_error=True,
            )

        if resultado is None or resultado is False:
            raise RuntimeError(
                f"MikroTik no confirmó la creación o actualización de PPPoE {user}"
            )
        return resultado

    def eliminar_pppoe_user(self, usuario):
        try:
            respuesta = self._request("GET", f"/ppp/secret?name={usuario}")
            if isinstance(respuesta, list) and len(respuesta) > 0:
                id_interno = respuesta[0].get(".id")
                self._request(
                    "DELETE",
                    f"/ppp/secret/{id_interno}",
                    raise_on_error=True,
                )
                print(f"✅ MikroTik: Usuario {usuario} eliminado correctamente.")
            else:
                print(f"⚠️ MikroTik: El usuario {usuario} ya no existía.")
                
            self.desconectar_cliente_activo(usuario)
            return True
            
        except Exception as e:
            print(f"❌ Error al eliminar usuario {usuario} en MikroTik: {e}")
            raise Exception(f"Fallo en MikroTik: {str(e)}")

    def activar_desactivar_pppoe(self, usuario, disabled: bool):
        res = self._request("GET", f"/ppp/secret?name={usuario}")
        if res and len(res) > 0:
            resultado = self._request(
                "PATCH",
                f"/ppp/secret/{res[0]['.id']}",
                {"disabled": "true" if disabled else "false"},
                raise_on_error=True,
            )
            if resultado is None:
                return False
            if disabled:
                self.desconectar_cliente_activo(usuario)
            return True
        return False

    def obtener_todos_pppoe(self):
        res = self._request("GET", "/ppp/secret")
        return res if isinstance(res, list) else []

    def obtener_todos_pppoe_estricto(self):
        """Lista secrets diferenciando un router vacío de un fallo de conexión."""
        res = self._request(
            "GET",
            "/ppp/secret",
            raise_on_error=True,
        )
        if not isinstance(res, list):
            raise RuntimeError(
                "MikroTik devolvió una respuesta inválida al listar PPPoE"
            )
        return res

    def obtener_pppoe_estricto(self, usuario):
        res = self._request(
            "GET",
            f"/ppp/secret?name={usuario}",
            raise_on_error=True,
        )
        if not isinstance(res, list):
            raise RuntimeError(
                f"MikroTik no permitió verificar el PPPoE {usuario}"
            )
        return res[0] if res else None

    # ==========================================
    #  3. SESIONES ACTIVAS PPPoE
    # ==========================================
    def desconectar_cliente_activo(self, usuario):
        try:
            respuesta = self._request("GET", f"/ppp/active?name={usuario}")
            if isinstance(respuesta, list) and len(respuesta) > 0:
                id_activo = respuesta[0].get(".id")
                self._request("DELETE", f"/ppp/active/{id_activo}")
                print(f"✅ MikroTik: Sesión de internet de {usuario} cortada de golpe.")
        except Exception as e:
            print(f"⚠️ MikroTik: No se pudo desconectar sesión activa: {e}")

    def obtener_info_sesion(self, usuario):
        res = self._request("GET", f"/ppp/active?name={usuario}")
        if res and isinstance(res, list) and len(res) > 0:
            return {
                "online": True,
                "ip": res[0].get("address", ""),
                "uptime": res[0].get("uptime", ""),
                "mac_onu": res[0].get("caller-id", "")
            }
        return {"online": False}

    def obtener_todos_active_pppoe(self):
        res = self._request("GET", "/ppp/active")
        return res if isinstance(res, list) else []

    # ==========================================
    #  3.5 ACCESO DHCP ESTÁTICO POR IP/MAC
    # ==========================================
    def obtener_todos_dhcp_estricto(self):
        leases = self._request(
            "GET",
            "/ip/dhcp-server/lease",
            raise_on_error=True,
        )
        if not isinstance(leases, list):
            raise RuntimeError("MikroTik devolvió una lista DHCP inválida")
        return leases

    def obtener_lease_dhcp_estricto(self, mac_address):
        mac = normalizar_mac(mac_address)
        matches = [
            item
            for item in self.obtener_todos_dhcp_estricto()
            if str(item.get("mac-address", "")).upper() == mac
        ]
        if len(matches) > 1:
            raise RuntimeError(f"La MAC {mac} está repetida en DHCP")
        return matches[0] if matches else None

    def crear_actualizar_lease_dhcp(
        self,
        mac_address,
        address,
        rate_limit,
        comment="FdezNet DHCP",
    ):
        mac = normalizar_mac(mac_address)
        ip = str(address or "").strip()
        if not ip or ip == "0.0.0.0":
            raise ValueError("El lease DHCP necesita una IP fija")
        leases = self.obtener_todos_dhcp_estricto()
        by_mac = [
            item
            for item in leases
            if str(item.get("mac-address", "")).upper() == mac
        ]
        if len(by_mac) > 1:
            raise RuntimeError(f"La MAC {mac} está repetida en DHCP")
        conflict = next(
            (
                item
                for item in leases
                if str(item.get("address", "")).strip() == ip
                and str(item.get("mac-address", "")).upper() != mac
            ),
            None,
        )
        if conflict:
            raise RuntimeError(
                f"La IP {ip} ya pertenece a otra MAC en DHCP"
            )

        payload = {
            "address": ip,
            "mac-address": mac,
            "rate-limit": str(rate_limit).strip(),
            "comment": comment,
            "block-access": "false",
        }
        lease = by_mac[0] if by_mac else None
        if lease and str(lease.get("dynamic", "false")).lower() in {
            "true", "yes", "1"
        }:
            self._request(
                "POST",
                "/ip/dhcp-server/lease/make-static",
                {"numbers": lease[".id"]},
                raise_on_error=True,
            )
            lease = self.obtener_lease_dhcp_estricto(mac)

        if lease:
            result = self._request(
                "PATCH",
                f"/ip/dhcp-server/lease/{lease['.id']}",
                payload,
                raise_on_error=True,
            )
        else:
            result = self._request(
                "PUT",
                "/ip/dhcp-server/lease",
                payload,
                raise_on_error=True,
            )
        if result is None or result is False:
            raise RuntimeError("MikroTik no confirmó el lease DHCP")
        verified = self.obtener_lease_dhcp_estricto(mac)
        if not verified:
            raise RuntimeError("El lease DHCP no apareció después de guardarlo")
        if str(verified.get("address", "")).strip() != ip:
            raise RuntimeError("MikroTik no confirmó la IP del lease DHCP")
        return verified

    def activar_desactivar_dhcp(self, mac_address, blocked: bool):
        lease = self.obtener_lease_dhcp_estricto(mac_address)
        if not lease:
            return False
        result = self._request(
            "PATCH",
            f"/ip/dhcp-server/lease/{lease['.id']}",
            {"block-access": "true" if blocked else "false"},
            raise_on_error=True,
        )
        return result is not None and result is not False

    def eliminar_lease_dhcp(self, mac_address):
        lease = self.obtener_lease_dhcp_estricto(mac_address)
        if not lease:
            return False
        self._request(
            "DELETE",
            f"/ip/dhcp-server/lease/{lease['.id']}",
            raise_on_error=True,
        )
        return True

    # ==========================================
    #  4. FIREWALL DE CORTES (MOROSOS)
    # ==========================================
    def inicializar_firewall_corte(
        self,
        ip_servidor_portal: str = None,
        solo_whatsapp: bool = False,
        kbps: int = KBPS_CORTE_WHATSAPP_DEFAULT,
    ):
        LISTA_CORTE = "CORTE_FDEZNET"
        try:
            if ip_servidor_portal:
                payload_nat = {
                    "chain": "dstnat", "protocol": "tcp", "dst-port": "80",
                    "src-address-list": LISTA_CORTE, "action": "dst-nat",
                    "to-addresses": ip_servidor_portal, "to-ports": "80",
                    "comment": "=== PORTAL COBRANZA ==="
                }
                self._asegurar_regla_unica("/ip/firewall/nat", payload_nat)

            payload_filter = {
                "chain": "forward", "src-address-list": LISTA_CORTE,
                "action": "drop", "comment": "=== BLOQUEO MOROSOS ==="
            }
            self._asegurar_regla_unica("/ip/firewall/filter", payload_filter)

            if solo_whatsapp:
                self._activar_corte_whatsapp(int(kbps))
                return True, (
                    "Firewall de corte listo: suspendidos solo con "
                    f"WhatsApp a {int(kbps)} kbps"
                )
            self._desactivar_corte_whatsapp()
            return True, "Firewall de corte FTTH listo"
        except Exception as e:
            return False, str(e)

    # --- Modo suspendido "solo WhatsApp" ---
    # Las reglas accept van al inicio de la cadena forward: así quedan antes
    # de fasttrack y del drop de morosos, y el tráfico permitido nunca se
    # fasttrackea (si lo hiciera, se saltaría la cola de velocidad).

    @staticmethod
    def _reglas_filtro_whatsapp():
        return [
            {
                "chain": "forward", "action": "accept",
                "src-address-list": LISTA_CORTE,
                "dst-address-list": LISTA_WHATSAPP,
                "protocol": "tcp", "dst-port": "443,5222",
                "comment": "=== CORTE WHATSAPP SALIDA ===",
            },
            {
                "chain": "forward", "action": "accept",
                "src-address-list": LISTA_WHATSAPP,
                "dst-address-list": LISTA_CORTE,
                "protocol": "tcp", "src-port": "443,5222",
                "comment": "=== CORTE WHATSAPP ENTRADA ===",
            },
            {
                "chain": "forward", "action": "accept",
                "src-address-list": LISTA_CORTE,
                "protocol": "udp", "dst-port": "53",
                "comment": "=== CORTE DNS UDP ===",
            },
            {
                "chain": "forward", "action": "accept",
                "src-address-list": LISTA_CORTE,
                "protocol": "tcp", "dst-port": "53",
                "comment": "=== CORTE DNS TCP ===",
            },
        ]

    @staticmethod
    def _reglas_mangle_whatsapp():
        return [
            {
                "chain": "forward", "action": "mark-packet",
                "src-address-list": LISTA_CORTE,
                "dst-address-list": LISTA_WHATSAPP,
                "new-packet-mark": MARCA_WA_SUBIDA,
                "passthrough": "false",
                "comment": "=== CORTE WHATSAPP MARCA SUBIDA ===",
            },
            {
                "chain": "forward", "action": "mark-packet",
                "src-address-list": LISTA_WHATSAPP,
                "dst-address-list": LISTA_CORTE,
                "new-packet-mark": MARCA_WA_BAJADA,
                "passthrough": "false",
                "comment": "=== CORTE WHATSAPP MARCA BAJADA ===",
            },
        ]

    @staticmethod
    def _colas_whatsapp(kbps: int):
        tipos = [
            {
                "name": COLA_TIPO_WA_SUBIDA, "kind": "pcq",
                "pcq-rate": f"{kbps}k", "pcq-classifier": "src-address",
            },
            {
                "name": COLA_TIPO_WA_BAJADA, "kind": "pcq",
                "pcq-rate": f"{kbps}k", "pcq-classifier": "dst-address",
            },
        ]
        arboles = [
            {
                "name": COLA_ARBOL_WA_SUBIDA, "parent": "global",
                "packet-mark": MARCA_WA_SUBIDA, "queue": COLA_TIPO_WA_SUBIDA,
                "comment": "FdezNet: suspendidos solo WhatsApp (subida)",
            },
            {
                "name": COLA_ARBOL_WA_BAJADA, "parent": "global",
                "packet-mark": MARCA_WA_BAJADA, "queue": COLA_TIPO_WA_BAJADA,
                "comment": "FdezNet: suspendidos solo WhatsApp (bajada)",
            },
        ]
        return tipos, arboles

    def _buscar(self, ruta: str, campo: str, valor: str):
        res = self._request(
            "GET",
            f"{ruta}?{campo}={quote(valor)}",
            raise_on_error=True,
        )
        return res if isinstance(res, list) else []

    def _asegurar_item(self, ruta: str, campo: str, payload: dict) -> str:
        """Crea o corrige el item identificado por `campo`; devuelve su id."""
        existentes = self._buscar(ruta, campo, payload[campo])
        if existentes:
            actual = existentes[0]
            cambios = {
                clave: valor
                for clave, valor in payload.items()
                if str(actual.get(clave, "")) != str(valor)
            }
            if str(actual.get("disabled", "false")) == "true":
                cambios["disabled"] = "false"
            if cambios:
                self._request(
                    "PATCH", f"{ruta}/{actual['.id']}", cambios,
                    raise_on_error=True,
                )
            return actual[".id"]
        creado = self._request("PUT", ruta, payload, raise_on_error=True)
        if not isinstance(creado, dict) or not creado.get(".id"):
            creados = self._buscar(ruta, campo, payload[campo])
            if not creados:
                raise RuntimeError(
                    f"MikroTik no confirmó la creación en {ruta}"
                )
            return creados[0][".id"]
        return creado[".id"]

    def _asegurar_regla_unica(self, ruta: str, payload: dict):
        """Una sola regla con ese comentario: la crea o borra las copias.

        Antes, si la consulta fallaba (VPN lenta, tiempo de espera) se tomaba
        como "no existe" y se creaba otra copia; ahora un error no crea nada.
        """
        existentes = self._buscar(ruta, "comment", payload["comment"])
        if not existentes:
            self._request("PUT", ruta, payload, raise_on_error=True)
            return
        for sobrante in existentes[1:]:
            self._request(
                "DELETE", f"{ruta}/{sobrante['.id']}", raise_on_error=True
            )

    def _eliminar_items(self, ruta: str, campo: str, valores):
        for valor in valores:
            for item in self._buscar(ruta, campo, valor):
                self._request(
                    "DELETE", f"{ruta}/{item['.id']}", raise_on_error=True
                )

    def _mover_al_inicio(self, ruta: str, cadena: str, ids: list[str]):
        """Deja `ids` como primeras reglas (no dinámicas) de la cadena."""
        reglas = self._request(
            "GET", f"{ruta}?chain={cadena}", raise_on_error=True
        )
        orden = [
            regla[".id"]
            for regla in (reglas if isinstance(reglas, list) else [])
            if str(regla.get("dynamic", "false")) != "true"
        ]
        if orden[:len(ids)] == ids:
            return
        for regla_id in reversed(ids):
            if orden and orden[0] == regla_id:
                continue
            self._request(
                "POST",
                f"{ruta}/move",
                {"numbers": regla_id, "destination": orden[0]},
                raise_on_error=True,
            )
            if regla_id in orden:
                orden.remove(regla_id)
            orden.insert(0, regla_id)

    def _sincronizar_lista_whatsapp(self):
        ruta = "/ip/firewall/address-list"
        actuales = self._buscar(ruta, "list", LISTA_WHATSAPP)
        registradas = {
            str(item.get("address", "")).strip(): item for item in actuales
        }
        for rango in RANGOS_META_WHATSAPP:
            if rango not in registradas:
                self._request(
                    "PUT",
                    ruta,
                    {
                        "list": LISTA_WHATSAPP,
                        "address": rango,
                        "comment": "Meta/WhatsApp (FdezNet)",
                    },
                    raise_on_error=True,
                )
        for direccion, item in registradas.items():
            if direccion not in RANGOS_META_WHATSAPP:
                self._request(
                    "DELETE", f"{ruta}/{item['.id']}", raise_on_error=True
                )

    def _activar_corte_whatsapp(self, kbps: int):
        if kbps < 16:
            raise ValueError("La velocidad del modo WhatsApp es muy baja")
        self._sincronizar_lista_whatsapp()

        tipos, arboles = self._colas_whatsapp(kbps)
        for tipo in tipos:
            self._asegurar_item("/queue/type", "name", tipo)
        for arbol in arboles:
            self._asegurar_item("/queue/tree", "name", arbol)

        for ruta, reglas in (
            ("/ip/firewall/mangle", self._reglas_mangle_whatsapp()),
            ("/ip/firewall/filter", self._reglas_filtro_whatsapp()),
        ):
            ids = [
                self._asegurar_item(ruta, "comment", regla)
                for regla in reglas
            ]
            self._mover_al_inicio(ruta, "forward", ids)

    def _desactivar_corte_whatsapp(self):
        self._eliminar_items(
            "/ip/firewall/filter",
            "comment",
            [r["comment"] for r in self._reglas_filtro_whatsapp()],
        )
        self._eliminar_items(
            "/ip/firewall/mangle",
            "comment",
            [r["comment"] for r in self._reglas_mangle_whatsapp()],
        )
        self._eliminar_items(
            "/queue/tree", "name", [COLA_ARBOL_WA_SUBIDA, COLA_ARBOL_WA_BAJADA]
        )
        self._eliminar_items(
            "/queue/type", "name", [COLA_TIPO_WA_SUBIDA, COLA_TIPO_WA_BAJADA]
        )
        self._eliminar_items(
            "/ip/firewall/address-list", "list", [LISTA_WHATSAPP]
        )

    def limpiar_conexiones_cliente(self, ip_target) -> bool:
        """Cierra conexiones abiertas (incluidas las fasttrack) de una IP.

        Sin esto, una descarga o video iniciado antes del corte puede seguir
        fluyendo porque FastTrack salta el firewall.
        """
        ip = str(ip_target or "").strip()
        if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip):
            return False
        script = (
            "/ip firewall connection remove [find where "
            f'src-address~"^{ip}:" or dst-address~"^{ip}:"]'
        )
        return self._request("POST", "/execute", {"script": script}) is not None

    def gestionar_corte_cliente(self, ip_target, suspender: bool):
        if not ip_target or ip_target == '0.0.0.0':
            return False

        ip_target = str(ip_target).strip()
        LISTA_CORTE = "CORTE_FDEZNET"
        
        endpoint_consulta = (
            f"/ip/firewall/address-list?address={ip_target}"
            f"&list={LISTA_CORTE}"
        )
        res = self._request(
            "GET",
            endpoint_consulta,
            raise_on_error=True,
        )
        if not isinstance(res, list):
            raise RuntimeError(
                "MikroTik devolvió una respuesta inválida al consultar "
                "el address-list"
            )
        existe = len(res) > 0

        if suspender and not existe:
            self._request(
                "PUT",
                "/ip/firewall/address-list",
                {
                    "list": LISTA_CORTE,
                    "address": ip_target,
                    "comment": "Suspendido",
                },
                raise_on_error=True,
            )
            try:
                self.limpiar_conexiones_cliente(ip_target)
            except Exception as exc:
                print(f"⚠️ No se limpiaron conexiones de {ip_target}: {exc}")
        elif not suspender and existe:
            for item in res:
                self._request(
                    "DELETE",
                    f"/ip/firewall/address-list/{item['.id']}",
                    raise_on_error=True,
                )

        verificacion = self._request(
            "GET",
            endpoint_consulta,
            raise_on_error=True,
        )
        if not isinstance(verificacion, list):
            raise RuntimeError(
                "MikroTik no permitió verificar el address-list"
            )

        sigue_en_lista = len(verificacion) > 0
        if suspender != sigue_en_lista:
            accion = "agregar" if suspender else "retirar"
            raise RuntimeError(
                f"MikroTik no confirmó que se pudiera {accion} "
                f"la IP {ip_target} en {LISTA_CORTE}"
            )
        return True

    def obtener_ips_cortadas(self):
        """Obtiene una fotografía verificable de la lista de suspensión."""
        res = self._request(
            "GET",
            "/ip/firewall/address-list?list=CORTE_FDEZNET",
            raise_on_error=True,
        )
        if not isinstance(res, list):
            raise RuntimeError(
                "MikroTik devolvió una respuesta inválida para "
                "CORTE_FDEZNET"
            )
        return {
            str(item.get("address", "")).strip()
            for item in res
            if item.get("address")
        }

    def reactivar_cliente(self, ip_target, usuario_pppoe=None):
        """Retira el corte y rehabilita el secret PPPoE si existe."""
        if self.gestionar_corte_cliente(ip_target, suspender=False) is not True:
            return False

        if usuario_pppoe:
            if not self.activar_desactivar_pppoe(
                usuario_pppoe,
                disabled=False,
            ):
                raise RuntimeError(
                    f"No se encontró o no se pudo habilitar el PPPoE "
                    f"{usuario_pppoe}"
                )

        return True

    # ==========================================
    #  5. DIAGNÓSTICO AVANZADO
    # ==========================================
    def obtener_consumo_interfaz_pppoe(self, usuario):
        interfaz = f"<pppoe-{usuario}>"
        try:
            payload = {"interface": interfaz, "once": "true"}
            res = self._request("POST", "/interface/monitor-traffic", payload)
            if res and isinstance(res, list) and len(res) > 0:
                return {
                    "up_bps": int(res[0].get('tx-bits-per-second', 0)),
                    "down_bps": int(res[0].get('rx-bits-per-second', 0))
                }
        except Exception as e: pass

        try:
            res_q = self._request("GET", f"/queue/simple?name={interfaz}")
            if res_q and isinstance(res_q, list) and len(res_q) > 0:
                r_up, r_down = res_q[0].get('rate', '0/0').split('/')
                return {"up_bps": int(r_up), "down_bps": int(r_down)}
        except Exception as e: pass
            
        return {"up_bps": 0, "down_bps": 0}

    def ping_desde_router(self, ip_destino, count=2):
        try:
            res = self._request("POST", "/ping", {"address": ip_destino, "count": str(count)})
            recibidos = sum(1 for p in res if "time" in p) if isinstance(res, list) else 0
            return {"status": "online" if recibidos > 0 else "offline", "loss": f"{100 - (recibidos/count*100)}%"}
        except: return {"status": "error"}

    def eliminar_item(self, path, name_identifier):
        res = self._request("GET", f"{path}?name={name_identifier}")
        if res and isinstance(res, list):
            for item in res: self._request("DELETE", f"{path}/{item['.id']}")
            return True
        return False
    
    def obtener_recursos_sistema(self):
        res = self._request("GET", "/system/resource")
        return res[0] if res and len(res) > 0 else {}
