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

    def _request(self, method, endpoint, payload=None):
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.request(
                method, url, auth=self.auth, json=payload, timeout=self.timeout, verify=False
            )
            if response.status_code in [200, 201, 204]:
                return response.json() if response.text else True
            if response.status_code == 404: 
                return None
            if response.status_code >= 400:
                print(f"⚠️ MK Error {response.status_code} en {endpoint}: {response.text}")
                return None
            return None
        except Exception as e:
            print(f"❌ Error Conexión MK ({endpoint}): {e}")
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
            return self._request("PATCH", f"/ppp/secret/{res[0]['.id']}", payload)
        else:
            return self._request("PUT", "/ppp/secret", payload)

    def eliminar_pppoe_user(self, usuario):
        self._request("DELETE", f"/ppp/secret?name={usuario}")
        self.desconectar_cliente_activo(usuario)
        return True

    def activar_desactivar_pppoe(self, usuario, disabled: bool):
        res = self._request("GET", f"/ppp/secret?name={usuario}")
        if res and len(res) > 0:
            self._request("PATCH", f"/ppp/secret/{res[0]['.id']}", {"disabled": "true" if disabled else "false"})
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
        """Fuerza la reconexión de la ONU para aplicar cambios de velocidad o IP."""
        res = self._request("GET", f"/ppp/active?name={usuario}")
        if res and isinstance(res, list):
            for item in res:
                self._request("DELETE", f"/ppp/active/{item['.id']}")
            return True
        return False

    def obtener_info_sesion(self, usuario):
        """Retorna Uptime, IP actual y MAC (Caller-ID) de la ONU."""
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
        """
        Descarga de golpe todas las sesiones activas. 
        Vital para que el Dashboard muestre el puntito verde (Online) rápido.
        """
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
        """Agrega o quita al cliente de la lista de cortes."""
        if not ip_target or ip_target == '0.0.0.0': return False
        LISTA_CORTE = "CORTE_FDEZNET"
        
        res = self._request("GET", f"/ip/firewall/address-list?address={ip_target}&list={LISTA_CORTE}")
        existe = True if res and len(res) > 0 else False

        if suspender and not existe:
            return self._request("PUT", "/ip/firewall/address-list", {"list": LISTA_CORTE, "address": ip_target, "comment": "Suspendido"})
        elif not suspender and existe:
            for item in res:
                self._request("DELETE", f"/ip/firewall/address-list/{item['.id']}")
        return True

    # ==========================================
    #  5. DIAGNÓSTICO AVANZADO
    # ==========================================
    def obtener_consumo_interfaz_pppoe(self, usuario):
        """Obtiene el consumo real directamente de la interfaz virtual PPPoE."""
        interfaz = f"<pppoe-{usuario}>"
        
        # PLAN A: Usar monitor-traffic (Requiere POST porque es una acción en REST API)
        try:
            payload = {
                "interface": interfaz,
                "once": "true" # Ejecuta el monitoreo 1 sola vez y devuelve el resultado
            }
            res = self._request("POST", "/interface/monitor-traffic", payload)
            
            if res and isinstance(res, list) and len(res) > 0:
                return {
                    "up_bps": int(res[0].get('tx-bits-per-second', 0)),
                    "down_bps": int(res[0].get('rx-bits-per-second', 0))
                }
        except Exception as e: 
            print(f"⚠️ Error Monitor Traffic en {interfaz}: {e}")

        # PLAN B (Fallback): Leer la cola dinámica que genera PPPoE
        try:
            # Las URLs con símbolos < y > a veces fallan en GET, pero el wrapper REST
            # de Python (requests) suele manejarlo. Si no, busca por nombre.
            res_q = self._request("GET", f"/queue/simple?name={interfaz}")
            if res_q and isinstance(res_q, list) and len(res_q) > 0:
                r_up, r_down = res_q[0].get('rate', '0/0').split('/')
                return {
                    "up_bps": int(r_up),
                    "down_bps": int(r_down)
                }
        except Exception as e:
            print(f"⚠️ Error Queue Simple en {interfaz}: {e}")
            
        return {"up_bps": 0, "down_bps": 0}

    def ping_desde_router(self, ip_destino, count=2):
        try:
            res = self._request("POST", "/ping", {"address": ip_destino, "count": str(count)})
            recibidos = sum(1 for p in res if "time" in p) if isinstance(res, list) else 0
            return {"status": "online" if recibidos > 0 else "offline", "loss": f"{100 - (recibidos/count*100)}%"}
        except: return {"status": "error"}

    def eliminar_item(self, path, name_identifier):
        """Utilidad genérica para borrar por nombre."""
        res = self._request("GET", f"{path}?name={name_identifier}")
        if res and isinstance(res, list):
            for item in res: self._request("DELETE", f"{path}/{item['.id']}")
            return True
        return False
    
    def obtener_recursos_sistema(self):
        res = self._request("GET", "/system/resource")
        return res[0] if res and len(res) > 0 else {}