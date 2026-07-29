import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
            "comment": "FdezNet-FTTH"
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
    def crear_actualizar_pppoe(self, user, password, profile, remote_address=None, comment="FdezNet"):
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
    #  4. FIREWALL DE CORTES (MOROSOS)
    # ==========================================
    def inicializar_firewall_corte(self, ip_servidor_portal: str = None):
        LISTA_CORTE = "CORTE_FDEZNET"
        try:
            if ip_servidor_portal:
                payload_nat = {
                    "chain": "dstnat", "protocol": "tcp", "dst-port": "80",
                    "src-address-list": LISTA_CORTE, "action": "dst-nat",
                    "to-addresses": ip_servidor_portal, "to-ports": "80",
                    "comment": "=== PORTAL COBRANZA ==="
                }
                if not self._request("GET", f"/ip/firewall/nat?comment==== PORTAL COBRANZA ==="):
                    self._request("PUT", "/ip/firewall/nat", payload_nat)

            payload_filter = {
                "chain": "forward", "src-address-list": LISTA_CORTE,
                "action": "drop", "comment": "=== BLOQUEO MOROSOS ==="
            }
            if not self._request("GET", f"/ip/firewall/filter?comment==== BLOQUEO MOROSOS ==="):
                self._request("PUT", "/ip/firewall/filter", payload_filter)
            return True, "Firewall de corte FTTH listo"
        except Exception as e:
            return False, str(e)

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
