from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        # Guardamos todos los sockets en una lista simple y segura
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        """Envía el mensaje a todos los clientes conectados de forma segura"""
        # Iteramos sobre una copia de la lista para evitar errores si alguien se desconecta en este milisegundo
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                # Si el túnel se rompió, lo sacamos de la lista silenciosamente
                self.disconnect(connection)

manager = ConnectionManager()