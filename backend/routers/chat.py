# Real-time WebSocket Emergency Chat Router
from __future__ import annotations
import logging
import uuid
from typing import Dict, List, Literal, Set
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import asyncio

import storage
from routers.incident import ACTIVE_INCIDENTS, persistence_path

logger = logging.getLogger("geoagentic.chat")

router = APIRouter(prefix="/api/chat", tags=["Realtime Emergency Chat"])

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.chat_history: Dict[str, List[Dict]] = {}

    async def connect(self, websocket: WebSocket, incident_id: str):
        await websocket.accept()
        if incident_id not in self.active_connections:
            self.active_connections[incident_id] = set()
        await self.load(incident_id)
        self.active_connections[incident_id].add(websocket)

    def disconnect(self, websocket: WebSocket, incident_id: str):
        if incident_id in self.active_connections:
            self.active_connections[incident_id].discard(websocket)

    def history(self, incident_id: str) -> List[Dict]:
        """Cached history; on a miss, read it synchronously (tests and startup only)."""
        if incident_id not in self.chat_history:
            self.chat_history[incident_id] = storage.load_chat_migrating(incident_id, database=persistence_path())
        return self.chat_history[incident_id]

    async def load(self, incident_id: str) -> List[Dict]:
        """Cached history; on a miss, read it off the event loop."""
        if incident_id not in self.chat_history:
            messages = await asyncio.to_thread(
                storage.load_chat_migrating, incident_id, database=persistence_path()
            )
            # Another request may have filled the cache while this one waited.
            self.chat_history.setdefault(incident_id, messages)
        return self.chat_history[incident_id]

    def clear_history(self) -> None:
        database = persistence_path()
        storage.clear_chat(database=database)
        storage.delete_collections("chat:", database=database)
        self.chat_history.clear()

    def forget(self, incident_ids) -> None:
        """Drop cached history for incidents that were pruned (their rows are already gone)."""
        for incident_id in incident_ids:
            self.chat_history.pop(incident_id, None)
            self.active_connections.pop(incident_id, None)

    async def broadcast(self, incident_id: str, message_data: Dict):
        history = await self.load(incident_id)
        # One row per message, written off the event loop, before anyone sees it.
        await asyncio.to_thread(storage.append_chat, incident_id, [message_data], database=persistence_path())
        history.append(message_data)

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

CHAT_ROLES = ("citizen", "dispatcher", "driver", "hospital")
MAX_MESSAGE_CHARS = 2000
MAX_NAME_CHARS = 80


def incident_exists(incident_id: str) -> bool:
    """A channel exists only for a reported incident, so ids cannot be minted freely."""
    return any(i.get("incident_id") == incident_id for i in ACTIVE_INCIDENTS)


RESERVED_NAMES = {"route monitoring", "dispatch decision"}


def safe_sender_name(name: str) -> str:
    """User names may not imitate the system's senders."""
    return "User" if (name or "").strip().casefold() in RESERVED_NAMES else name


class ChatMessagePayload(BaseModel):
    incident_id: str = Field(max_length=100)
    sender_role: Literal["citizen", "dispatcher", "driver", "hospital"]
    sender_name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    message: str = Field(max_length=MAX_MESSAGE_CHARS)

async def post_system_message(incident_id: str, text: str, sender: str = "Route monitoring") -> None:
    """A message from the monitoring agent or a dispatch decision, into the
    incident's channel, so the crew, the hospital and the caller see it.
    Not postable by users: CHAT_ROLES excludes "system", and user names that
    imitate the system senders are refused."""
    if not incident_exists(incident_id):
        return
    try:
        await manager.broadcast(incident_id, _build_message(incident_id, "system", sender, text[:MAX_MESSAGE_CHARS]))
    except Exception:
        logger.exception("Could not post a monitoring message")


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
    if not incident_exists(incident_id):
        # Do not cache or store anything for ids that were never reported.
        return {"messages": []}
    history = await manager.load(incident_id)
    return {"messages": history}

@router.post("/{incident_id}/send")
async def send_chat_message(incident_id: str, payload: ChatMessagePayload):
    if payload.incident_id != incident_id:
        raise HTTPException(status_code=400, detail="Message incident does not match channel")
    if not incident_exists(incident_id):
        raise HTTPException(status_code=404, detail="Incident not found")
    body = (payload.message or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    msg_data = _build_message(
        incident_id,
        payload.sender_role,
        safe_sender_name(payload.sender_name),
        body,
    )
    await manager.broadcast(incident_id, msg_data)
    return {"status": "sent", "message": msg_data}
