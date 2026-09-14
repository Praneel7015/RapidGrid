# Real-time WebSocket Emergency Chat Router
from __future__ import annotations
import uuid
from typing import Dict, List, Set
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone

router = APIRouter(prefix="/api/chat", tags=["Realtime Emergency Chat"])

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.chat_history: Dict[str, List[Dict]] = {}

    async def connect(self, websocket: WebSocket, incident_id: str):
        await websocket.accept()
        if incident_id not in self.active_connections:
            self.active_connections[incident_id] = set()
            self.chat_history[incident_id] = []
        self.active_connections[incident_id].add(websocket)

    def disconnect(self, websocket: WebSocket, incident_id: str):
        if incident_id in self.active_connections:
            self.active_connections[incident_id].discard(websocket)

    async def broadcast(self, incident_id: str, message_data: Dict):
        if incident_id not in self.chat_history:
            self.chat_history[incident_id] = []
        self.chat_history[incident_id].append(message_data)

        if incident_id in self.active_connections:
            dead_sockets = set()
            for ws in list(self.active_connections[incident_id]):
                try:
                    await ws.send_json(message_data)
                except Exception:
                    dead_sockets.add(ws)
            for ws in dead_sockets:
                self.active_connections[incident_id].discard(ws)

manager = ConnectionManager()

class ChatMessagePayload(BaseModel):
    incident_id: str
    sender_role: str
    sender_name: str
    message: str

def _build_message(incident_id: str, sender_role: str, sender_name: str, message: str) -> Dict:
    return {
        "id": str(uuid.uuid4()),
        "incident_id": incident_id,
        "sender_role": sender_role,
        "sender_name": sender_name,
        "message": message,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "timestamp_iso": datetime.now(timezone.utc).isoformat(),
    }

@router.get("/{incident_id}/messages")
async def get_chat_history(incident_id: str):
    history = manager.chat_history.get(incident_id, [])
    return {"messages": history}

@router.post("/{incident_id}/send")
async def send_chat_message(incident_id: str, payload: ChatMessagePayload):
    body = (payload.message or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    msg_data = _build_message(
        incident_id,
        payload.sender_role,
        payload.sender_name,
        body,
    )
    await manager.broadcast(incident_id, msg_data)
    return {"status": "sent", "message": msg_data}
