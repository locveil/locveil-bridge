import asyncio
import json
import logging
from typing import Dict, Set, Any, Optional
from datetime import datetime
from enum import Enum
from fastapi import Request
from fastapi.responses import StreamingResponse

from locveil_bridge.domain.ports import EventPublisherPort

logger = logging.getLogger(__name__)

class SSEChannel(str, Enum):
    """Available SSE channels"""
    DEVICES = "devices"
    SCENARIOS = "scenarios" 
    SYSTEM = "system"

class SSEEvent:
    """Represents a Server-Sent Event"""
    
    def __init__(self, event_type: str, data: Any, channel: SSEChannel, id: Optional[str] = None):
        self.event_type = event_type
        self.data = data
        self.channel = channel
        self.id = id or str(int(datetime.now().timestamp() * 1000))
        self.timestamp = datetime.now()
    
    def format(self) -> str:
        """Format the event for SSE transmission with embedded event type"""
        lines = []
        
        if self.id:
            lines.append(f"id: {self.id}")
        
        # Embed event type in the data payload instead of separate event field
        if isinstance(self.data, str):
            # If data is already a string, try to parse it as JSON and add eventType
            try:
                parsed_data = json.loads(self.data)
                payload = {"eventType": self.event_type, **parsed_data}
            except json.JSONDecodeError:
                # If not valid JSON, wrap the string in an object
                payload = {"eventType": self.event_type, "message": self.data}
        elif isinstance(self.data, dict):
            # If data is a dict, embed eventType at the top level
            payload = {"eventType": self.event_type, **self.data}
        else:
            # For other data types, wrap in an object
            payload = {"eventType": self.event_type, "data": self.data}
        
        data_str = json.dumps(payload, separators=(",", ":"))
        
        # Handle multi-line data (future-proof against pretty-printed JSON)
        for line in data_str.splitlines():
            lines.append(f"data: {line}")
        
        # SSE events must end with CRLF double newline for proxy compatibility
        return "\r\n".join(lines) + "\r\n\r\n"

class SSEManager(EventPublisherPort):
    """Manages Server-Sent Event connections and broadcasting"""
    
    def __init__(self):
        # Active connections per channel
        self._connections: Dict[SSEChannel, Set[asyncio.Queue]] = {
            SSEChannel.DEVICES: set(),
            SSEChannel.SCENARIOS: set(),
            SSEChannel.SYSTEM: set()
        }
        self._connection_lock = asyncio.Lock()
        # Connections dropped for not draining (SCN-19 §6.7); their generators end.
        self._dropped: Set[asyncio.Queue] = set()
        self._shutdown_event = asyncio.Event()
        self._is_shutting_down = False
        # Track active event generator tasks for proper cleanup
        self._active_tasks: Set[asyncio.Task] = set()
        # Live uvicorn Server reference (set at startup by app.main). Lets the
        # SSE event generators poll `server.should_exit` — uvicorn flips that
        # flag on SIGINT BEFORE waiting for connections to drain, which is the
        # one signal that arrives early enough to let the generators return
        # cleanly so uvicorn's drain wait completes (instead of hanging forever
        # on long-lived SSE connections). See action_plan.md §5.1 #8 for the
        # full causal chain. Optional — None in tests / when launched some other
        # way; falls back to the in-process `_is_shutting_down` flag only.
        self._uvicorn_server: Optional[Any] = None

    def set_uvicorn_server(self, server: Any) -> None:
        """Wire the live uvicorn Server so event generators can poll
        `server.should_exit`. Called once from app.main at startup."""
        self._uvicorn_server = server

    def _shutdown_signaled(self) -> bool:
        """True if EITHER the in-process shutdown ran OR uvicorn was asked to
        exit. The uvicorn check is what makes 1st-Ctrl-C work — `should_exit`
        flips before uvicorn waits for the drain. Defensive `getattr` so a
        future uvicorn rename doesn't crash the generators (they'd just fall
        back to the in-process flag, behaving as pre-fix)."""
        if self._is_shutting_down:
            return True
        server = self._uvicorn_server
        if server is not None and getattr(server, "should_exit", False):
            return True
        return False
    
    async def add_connection(self, channel: SSEChannel, queue: asyncio.Queue) -> None:
        """Add a new SSE connection to a channel"""
        async with self._connection_lock:
            self._connections[channel].add(queue)
            logger.info(f"New SSE connection added to {channel.value} channel. Total: {len(self._connections[channel])}")
    
    async def remove_connection(self, channel: SSEChannel, queue: asyncio.Queue) -> None:
        """Remove an SSE connection from a channel"""
        async with self._connection_lock:
            self._connections[channel].discard(queue)
            self._dropped.discard(queue)
            logger.info(f"SSE connection removed from {channel.value} channel. Total: {len(self._connections[channel])}")
    
    async def publish_device_event(
        self, event_type: str, data: Any, event_id: Optional[str] = None
    ) -> None:
        """EventPublisherPort impl: fan a device event out on the DEVICES channel."""
        await self.broadcast(SSEChannel.DEVICES, event_type, data, event_id)

    async def broadcast(self, channel: SSEChannel, event_type: str, data: Any, event_id: Optional[str] = None) -> None:
        """Broadcast an event to all connections on a channel"""
        if channel not in self._connections:
            logger.warning(f"Unknown SSE channel: {channel}")
            return
        
        event = SSEEvent(event_type, data, channel, event_id)
        formatted_event = event.format()
        
        async with self._connection_lock:
            connections = self._connections[channel].copy()
        
        if not connections:
            logger.debug(f"No active connections for {channel.value} channel")
            return
        
        logger.debug(f"Broadcasting {event_type} event to {len(connections)} connections on {channel.value} channel")
        
        # Send to all active connections — NON-blocking (SCN-19 §6.7): a subscriber whose
        # queue is full is dropped, never waited for. The old `await queue.put` would
        # have stalled the producer — the scenario executor among them — on one slow
        # consumer, i.e. a stalled voice box could pace the house. The dropped
        # subscriber's generator sees the poison marker, ends its stream, and the
        # consumer reconnects + re-reads its truth (GET /scenario/jobs/{id}).
        dead_connections = set()
        for queue in connections:
            try:
                queue.put_nowait(formatted_event)
            except asyncio.QueueFull:
                logger.warning(
                    f"SSE subscriber on {channel.value} is not draining "
                    f"(queue of {queue.maxsize} full) — dropping the connection"
                )
                dead_connections.add(queue)
                self._poison(queue)
            except Exception as e:
                logger.warning(f"Failed to send event to connection: {e}")
                dead_connections.add(queue)
        
        # Clean up dead connections
        if dead_connections:
            async with self._connection_lock:
                for dead_queue in dead_connections:
                    self._connections[channel].discard(dead_queue)
            logger.info(f"Removed {len(dead_connections)} dead connections from {channel.value} channel")

    def _poison(self, queue: asyncio.Queue) -> None:
        """Mark a connection dropped (§6.7); its generator ends the stream on its next
        turn instead of serving the undrained backlog."""
        self._dropped.add(queue)

    def is_dropped(self, queue: asyncio.Queue) -> bool:
        return queue in self._dropped
    
    async def get_channel_stats(self) -> Dict[str, int]:
        """Get connection statistics for all channels"""
        async with self._connection_lock:
            return {
                channel.value: len(connections) 
                for channel, connections in self._connections.items()
            }
    
    async def shutdown(self) -> None:
        """Signal shutdown to all SSE connections"""
        logger.info("Initiating SSE manager shutdown...")
        self._is_shutting_down = True
        self._shutdown_event.set()
        
        # Cancel all active tasks immediately
        if self._active_tasks:
            logger.info(f"Cancelling {len(self._active_tasks)} active SSE tasks...")
            for task in self._active_tasks:
                if not task.done():
                    task.cancel()
            
            # Wait for tasks to be cancelled (with timeout)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._active_tasks, return_exceptions=True),
                    timeout=2.0
                )
            except asyncio.TimeoutError:
                logger.warning("Some SSE tasks did not cancel within timeout")
            
            self._active_tasks.clear()
        
        # Send shutdown event to any remaining active connections
        try:
            shutdown_data = {
                "message": "Server is shutting down",
                "timestamp": datetime.now().isoformat()
            }
            
            for channel in SSEChannel:
                await asyncio.wait_for(
                    self.broadcast(channel, "shutdown", shutdown_data),
                    timeout=0.5
                )
        except asyncio.TimeoutError:
            logger.warning("Broadcast of shutdown events timed out")
        except Exception as e:
            logger.warning(f"Error broadcasting shutdown events: {e}")
        
        # Get total connections before cleanup
        stats = await self.get_channel_stats()
        total_connections = sum(stats.values())
        
        if total_connections > 0:
            logger.info(f"Forcefully closing {total_connections} remaining SSE connections...")
        
        # Clear all connections to force cleanup
        async with self._connection_lock:
            for channel in self._connections:
                self._connections[channel].clear()
        
        logger.info("SSE manager shutdown complete")
    
    def is_shutting_down(self) -> bool:
        """Check if the SSE manager is shutting down"""
        return self._is_shutting_down
    
    async def create_event_stream(self, channel: SSEChannel, request: Request):
        """Create an SSE event stream for a specific channel"""
        queue = asyncio.Queue(maxsize=100)  # Limit queue size to prevent memory issues
        
        await self.add_connection(channel, queue)
        
        async def event_generator():
            try:
                # Send initial connection event
                welcome_event = SSEEvent(
                    event_type="connected",
                    data={"message": f"Connected to {channel.value} channel", "timestamp": datetime.now().isoformat()},
                    channel=channel
                )
                yield welcome_event.format()
                
                while True:
                    # Quick shutdown check first. _shutdown_signaled() checks BOTH the in-process
                    # shutdown flag AND uvicorn's `server.should_exit` — the latter is what makes
                    # the 1st Ctrl-C work (it flips before uvicorn waits for the connection drain).
                    if self._shutdown_signaled():
                        logger.info(f"Server shutdown detected, closing {channel.value} SSE connection")
                        break

                    # Check if client disconnected (but don't await it as it might block)
                    try:
                        if await asyncio.wait_for(request.is_disconnected(), timeout=0.1):
                            logger.info(f"Client disconnected from {channel.value} channel")
                            break
                    except asyncio.TimeoutError:
                        # Client still connected, continue
                        pass

                    try:
                        # Try to get an event from the queue with a short timeout
                        if queue in self._dropped:
                            logger.info(f"Closing a dropped (slow) {channel.value} SSE connection")
                            break
                        try:
                            event_data = await asyncio.wait_for(queue.get(), timeout=1.0)
                            yield event_data
                        except asyncio.TimeoutError:
                            # No events in queue, send keepalive and check shutdown again
                            if self._shutdown_signaled():
                                break

                            keepalive_event = SSEEvent(
                                event_type="keepalive",
                                data={"timestamp": datetime.now().isoformat()},
                                channel=channel
                            )
                            yield keepalive_event.format()

                    except Exception as e:
                        logger.error(f"Error processing SSE event for {channel.value}: {e}")
                        break
                        
            except asyncio.CancelledError:
                logger.info(f"SSE event generator for {channel.value} was cancelled")
                raise  # Re-raise to ensure proper cleanup
            except Exception as e:
                logger.error(f"Error in SSE event stream for {channel.value}: {e}")
            finally:
                await self.remove_connection(channel, queue)
        
        # Create a wrapper generator that tracks the task
        async def tracked_event_generator():
            # Get the current task (the one running this generator)
            current_task = asyncio.current_task()
            if current_task:
                self._active_tasks.add(current_task)
                
            try:
                async for event in event_generator():
                    yield event
            finally:
                # Clean up task tracking
                if current_task:
                    self._active_tasks.discard(current_task)
        
        return StreamingResponse(
            tracked_event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET",
                "Access-Control-Allow-Headers": "Cache-Control",
                "Access-Control-Expose-Headers": "Cache-Control, Content-Type"
            }
        )

# Global SSE manager instance
sse_manager = SSEManager() 