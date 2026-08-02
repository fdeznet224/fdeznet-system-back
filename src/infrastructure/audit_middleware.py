import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from src.infrastructure.database import SessionLocal
from src.infrastructure.models import LogActividadModel


logger = logging.getLogger(__name__)
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
EXCLUDED_PATHS = {"/auth/login", "/whatsapp/webhook/recibir", "/whatsapp/webhook/ack"}


class AuditMiddleware(BaseHTTPMiddleware):
    """Registra acciones exitosas sin almacenar cuerpos ni credenciales."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if (
            request.method not in MUTATING_METHODS
            or request.url.path in EXCLUDED_PATHS
            or response.status_code >= 400
        ):
            return response

        user = getattr(request.state, "current_user", None)
        if user is None:
            return response

        route = request.scope.get("route")
        route_template = getattr(route, "path", request.url.path)
        client_ip = request.client.host if request.client else None

        try:
            async with SessionLocal() as db:
                db.add(
                    LogActividadModel(
                        usuario_id=user.id,
                        usuario_nombre=user.usuario,
                        accion=f"{request.method} {route_template}",
                        metodo=request.method,
                        ruta=request.url.path[:255],
                        estado_http=response.status_code,
                        detalle="Acción completada",
                        ip_cliente=client_ip,
                    )
                )
                await db.commit()
        except Exception:
            logger.exception("No se pudo guardar el evento de auditoría")

        return response
