import ipaddress

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import ClienteModel, RedModel, ServicioModel


class IPAMService:
    """Asignación de IPs de clientes basada en las redes configuradas."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _red_ipv4(red: RedModel) -> ipaddress.IPv4Network:
        try:
            network = ipaddress.ip_network(red.cidr, strict=False)
        except ValueError as exc:
            raise ValueError("CIDR de red inválido") from exc

        if not isinstance(network, ipaddress.IPv4Network):
            raise ValueError("La red de clientes debe ser IPv4")
        return network

    @staticmethod
    def _normalizar_ip(valor: str) -> str:
        try:
            ip = ipaddress.ip_address(str(valor).strip())
        except ValueError as exc:
            raise ValueError(f"La IP {valor} no es válida") from exc

        if not isinstance(ip, ipaddress.IPv4Address):
            raise ValueError("La IP de cliente debe ser IPv4")
        return str(ip)

    async def _ips_ocupadas(
        self,
        excluir_cliente_id: int | None = None,
        excluir_servicio_id: int | None = None,
    ) -> set[str]:
        stmt_clientes = select(ClienteModel.ip_asignada).where(
            ClienteModel.ip_asignada.isnot(None),
        )
        if excluir_cliente_id is not None:
            stmt_clientes = stmt_clientes.where(
                ClienteModel.id != excluir_cliente_id
            )

        stmt_servicios = select(ServicioModel.ip_asignada).where(
            ServicioModel.ip_asignada.isnot(None),
            ServicioModel.estado != "cancelado",
        )
        if excluir_servicio_id is not None:
            stmt_servicios = stmt_servicios.where(
                ServicioModel.id != excluir_servicio_id
            )

        valores = [
            *(await self.db.execute(stmt_clientes)).scalars().all(),
            *(await self.db.execute(stmt_servicios)).scalars().all(),
        ]
        ocupadas: set[str] = set()
        for valor in valores:
            if not valor:
                continue
            try:
                ocupadas.add(self._normalizar_ip(valor))
            except ValueError:
                # Los datos históricos inválidos no deben romper el IPAM.
                ocupadas.add(str(valor).strip())
        return ocupadas

    async def listar_disponibles(
        self,
        red: RedModel,
        limite: int = 254,
        excluir_cliente_id: int | None = None,
        excluir_servicio_id: int | None = None,
    ) -> list[str]:
        network = self._red_ipv4(red)
        ocupadas = await self._ips_ocupadas(
            excluir_cliente_id,
            excluir_servicio_id,
        )

        if red.gateway:
            ocupadas.add(self._normalizar_ip(red.gateway))

        disponibles: list[str] = []
        for ip in network.hosts():
            ip_str = str(ip)
            if ip_str in ocupadas:
                continue
            disponibles.append(ip_str)
            if len(disponibles) >= limite:
                break
        return disponibles

    async def reservar_para_cliente(
        self,
        red_id: int,
        ip_solicitada: str | None = None,
        router_id: int | None = None,
        excluir_cliente_id: int | None = None,
        excluir_servicio_id: int | None = None,
    ) -> str:
        # Serializa las asignaciones concurrentes de una misma red.
        red = (
            await self.db.execute(
                select(RedModel)
                .where(RedModel.id == red_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not red:
            raise ValueError("Red no encontrada")

        if router_id and red.router_id != router_id:
            raise ValueError(
                "La red seleccionada no pertenece al router del cliente"
            )

        network = self._red_ipv4(red)
        ocupadas = await self._ips_ocupadas(
            excluir_cliente_id,
            excluir_servicio_id,
        )
        gateway = (
            self._normalizar_ip(red.gateway)
            if red.gateway
            else None
        )
        if gateway:
            ocupadas.add(gateway)

        if ip_solicitada:
            candidata = self._normalizar_ip(ip_solicitada)
            ip_obj = ipaddress.ip_address(candidata)
            no_utilizable = (
                network.prefixlen < 31
                and ip_obj in {
                    network.network_address,
                    network.broadcast_address,
                }
            )
            if ip_obj not in network or no_utilizable:
                raise ValueError(
                    f"La IP {candidata} no es utilizable en la red {red.cidr}"
                )
            if candidata in ocupadas:
                raise ValueError(f"La IP {candidata} ya está ocupada")
            return candidata

        for ip in network.hosts():
            candidata = str(ip)
            if candidata not in ocupadas:
                return candidata

        raise ValueError(f"No hay IPs disponibles en la red {red.nombre}")

    async def reservar_para_servicio(
        self,
        red_id: int,
        ip_solicitada: str | None = None,
        router_id: int | None = None,
        excluir_servicio_id: int | None = None,
    ) -> str:
        """Reserva una IP para un contrato sin confundirlo con la persona."""
        return await self.reservar_para_cliente(
            red_id=red_id,
            ip_solicitada=ip_solicitada,
            router_id=router_id,
            excluir_servicio_id=excluir_servicio_id,
        )
