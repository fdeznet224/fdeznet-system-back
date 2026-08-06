import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.infrastructure.models import OLTModel, ClienteModel
from src.infrastructure.snmp_oids import MAPA_OIDS, procesar_potencia

class SNMPMonitorService:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _deduplicar_onus(onus):
        """Conserva un solo registro por serial, prefiriendo el más útil."""
        mejores = {}
        for onu in onus:
            serial = str(onu.get("identificador") or "").strip().upper()
            if not serial:
                continue
            potencia = None
            try:
                potencia = float(onu.get("potencia"))
            except (TypeError, ValueError):
                pass
            candidato = (
                1 if onu.get("status") == "online" else 0,
                1 if potencia is not None else 0,
                potencia if potencia is not None else -999.0,
            )
            actual = mejores.get(serial)
            if actual is None or candidato > actual[0]:
                mejores[serial] = (candidato, {**onu, "identificador": serial})
        return [item[1] for item in mejores.values()]

    async def _ejecutar_comando(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if stderr:
            err_msg = stderr.decode('utf-8').strip()
            if err_msg:
                print(
                    "❌ [SISTEMA OS] Error ejecutando SNMP "
                    f"({args[0]}): {err_msg}"
                )
                
        return stdout.decode('utf-8')

    async def _consulta_individual(self, ip: str, comm: str, oid: str) -> str:
        res = await self._ejecutar_comando(
            "/usr/bin/snmpget",
            "-v2c",
            "-c",
            comm,
            "-On",
            ip,
            oid,
        )
        if "STRING:" in res.upper(): 
            return res.split('STRING:')[1].strip().replace('"', '')
        if "INTEGER:" in res.upper(): 
            return res.split('INTEGER:')[1].strip()
        return "N/A"

    async def _escanear_olt_fisica(self, ip: str, comunidad: str, modelo_key: str):
        conf = MAPA_OIDS.get(modelo_key)
        
        if not conf:
            raise ValueError(f"No hay OIDs configurados para el modelo {modelo_key}")

        tipo_tec = conf['TIPO']
        rama_walk = conf['RAMA_POTENCIA'] if tipo_tec == 'EPON' else conf['RAMA_IDS']
        
        res_walk = await self._ejecutar_comando(
            "/usr/bin/snmpwalk",
            "-v2c",
            "-c",
            comunidad,
            "-On",
            ip,
            rama_walk,
        )
        
        reporte = []
        if not res_walk.strip(): return reporte

        for linea in res_walk.strip().split('\n'):
            if "=" in linea:
                partes = linea.split("=")
                oid_completo = partes[0].strip()
                valor_crudo = partes[1].strip()
                
                clean_oid = oid_completo.replace("iso", "").replace("authenticated", "").strip().lstrip('.')
                clean_rama = rama_walk.strip().lstrip('.')
                full_idx = clean_oid.replace(clean_rama, "").strip().lstrip('.')
                
                if not full_idx: continue

                if "STRING:" in valor_crudo.upper():
                    val_id = valor_crudo.split(':')[-1].strip().replace('"', '')
                else:
                    val_id = valor_crudo.replace('"', '')

                if tipo_tec == 'EPON':
                    # Lógica V-SOL EPON (No cambia)
                    pwr_limpia = procesar_potencia(val_id, tipo_tec)
                    sub_idx = full_idx.split('.')[-1]
                    val_id = await self._consulta_individual(ip, comunidad, f"{conf['RAMA_IDS']}.{sub_idx}")
                else:
                    # Lógica GPON (V-SOL 1 Pon y 4 Pon)
                    if modelo_key == 'V1600GS':
                        # Ajuste para la de 1 puerto (Cambiamos el "0.x" por "1.x")
                        idx_potencia = full_idx.replace("0.", "1.") if full_idx.startswith("0.") else full_idx
                    else:
                        # Ajuste para la de 4 puertos (Usa el índice directo, ej. .1.4 o .2.1)
                        idx_potencia = full_idx
                        
                    oid_buscar_potencia = f"{conf['RAMA_POTENCIA']}.{idx_potencia}"
                    raw_pwr = await self._consulta_individual(ip, comunidad, oid_buscar_potencia)
                    pwr_limpia = procesar_potencia(raw_pwr, tipo_tec)

                try:
                    p_float = float(pwr_limpia)
                    if p_float < -100:
                        p_float = p_float / 100.0
                    pwr_limpia = f"{p_float:.2f}"
                    status = "online" if -5.0 > p_float > -35.0 else "offline"
                except:
                    pwr_limpia = "0.00"
                    status = "offline"

                reporte.append({
                    "identificador": val_id.upper().strip(),
                    "potencia": pwr_limpia,
                    "status": status
                })
        return reporte

    # ==========================================
    # 2. EL CEREBRO: CRUCE BD VS HARDWARE
    # ==========================================
    async def monitorear_olt(self, olt_id: int):
        olt = await self.db.get(OLTModel, olt_id)
        if not olt: raise ValueError("OLT no encontrada")

        stmt = select(ClienteModel).options(
            selectinload(ClienteModel.onu_asignada) 
        ).where(ClienteModel.olt_id == olt_id)
        
        clientes_db = (await self.db.execute(stmt)).scalars().all()
        
        mapa_bd = {}
        for c in clientes_db:
            sn = c.onu_asignada.identificador.upper() if c.onu_asignada else None
            if sn:
                mapa_bd[sn] = c

        def datos_cliente(cliente: ClienteModel):
            """Datos de CRM incluidos en Radar para evitar cruces parciales."""
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

        onus_fisicas = self._deduplicar_onus(
            await self._escanear_olt_fisica(olt.ip, olt.comunidad, olt.modelo)
        )

        resultados = {
            "olt_nombre": olt.nombre,
            "tecnologia": olt.tecnologia,
            "resumen": {"activos": 0, "caidos": 0, "desconocidos": 0},
            "clientes_activos": [],
            "clientes_caidos": [],
            "onus_desconocidas": [] 
        }

        onus_encontradas = set() 

        for onu in onus_fisicas:
            id_fisico = onu["identificador"]
            if id_fisico == "N/A": continue
            onus_encontradas.add(id_fisico)

            if id_fisico in mapa_bd:
                cliente_real = mapa_bd[id_fisico]
                datos_cruzados = {
                    **datos_cliente(cliente_real),
                    "identificador": id_fisico,
                    "rx_power": f"{onu['potencia']} dBm",
                    "estado_fisico": onu['status'],
                }

                if onu['status'] == "online":
                    resultados["clientes_activos"].append(datos_cruzados)
                    resultados["resumen"]["activos"] += 1
                else:
                    resultados["clientes_caidos"].append(datos_cruzados)
                    resultados["resumen"]["caidos"] += 1
            else:
                resultados["onus_desconocidas"].append({
                    "identificador": id_fisico,
                    "rx_power": f"{onu['potencia']} dBm",
                    "status": onu['status']
                })
                resultados["resumen"]["desconocidos"] += 1

        for id_bd, cliente in mapa_bd.items():
            if id_bd not in onus_encontradas:
                resultados["clientes_caidos"].append({
                    **datos_cliente(cliente),
                    "identificador": id_bd,
                    "rx_power": "LOS / Sin señal",
                    "estado_fisico": "offline",
                })
                resultados["resumen"]["caidos"] += 1

        return resultados

    async def monitorear_cliente_individual(self, cliente_id: int):
        """Diagnóstico en tiempo real para un solo cliente"""
        stmt = select(ClienteModel).options(
            selectinload(ClienteModel.onu_asignada)
        ).where(ClienteModel.id == cliente_id)
        
        cliente = (await self.db.execute(stmt)).scalar_one_or_none()
        
        if not cliente:
            raise ValueError("Cliente no encontrado.")
        return await self.monitorear_objetivo(cliente)

    async def monitorear_objetivo(self, objetivo):
        """Consulta la ONU del cliente legado o de un servicio concreto."""
        onu = getattr(objetivo, "onu", None) or getattr(
            objetivo,
            "onu_asignada",
            None,
        )
        sn_cliente = onu.identificador if onu else None
        if not objetivo.olt_id or not sn_cliente:
            raise ValueError(
                "Al servicio le falta OLT o Identificador de ONU (SN)."
            )
        olt = await self.db.get(OLTModel, objetivo.olt_id)
        if not olt:
            raise ValueError("La OLT asignada no existe.")
        
        onus_fisicas = await self._escanear_olt_fisica(olt.ip, olt.comunidad, olt.modelo)

        id_buscado = sn_cliente.upper().strip()
        cliente = getattr(objetivo, "cliente", None) or objetivo
        servicio_id = (
            objetivo.id
            if getattr(objetivo, "cliente_id", None) is not None
            else None
        )
        
        for onu in onus_fisicas:
            if onu["identificador"] == id_buscado:
                try:
                    pwr = float(onu['potencia'])
                except:
                    pwr = 0.00
                
                if pwr < -27.0:
                    msg = "Señal CRÍTICA. Revisar dobleces en la fibra o conectores sucios."
                elif pwr < -25.0:
                    msg = "Señal aceptable, pero al límite. Revisar si hay margen de mejora."
                else:
                    msg = "¡Señal EXCELENTE! Instalación óptima."

                return {
                    "cliente_id": cliente.id,
                    "servicio_id": servicio_id,
                    "nombre": cliente.nombre,
                    "identificador": id_buscado,
                    "potencia": f"{onu['potencia']} dBm",
                    "estado_fisico": onu['status'],
                    "tecnologia": olt.tecnologia,
                    "recomendacion": msg 
                }

        return {
            "cliente_id": cliente.id,
            "servicio_id": servicio_id,
            "nombre": cliente.nombre,
            "identificador": id_buscado,
            "potencia": "LOS / Sin señal",
            "estado_fisico": "offline",
            "tecnologia": olt.tecnologia,
            "recomendacion": "La OLT no ve la ONU. Verificar energía del cliente o ruptura de fibra troncal."
        }
