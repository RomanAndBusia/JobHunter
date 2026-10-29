import asyncio
from collections import defaultdict
from typing import Any, DefaultDict, Dict, Set


class EventHub:
    def __init__(self) -> None:
        self._listeners: DefaultDict[str, Set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, channel: str) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=100)
        self._listeners[channel].add(queue)
        return queue

    def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        self._listeners[channel].discard(queue)

    def publish(self, channel: str, payload: Dict[str, Any]) -> None:
        for queue in list(self._listeners[channel]):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(payload)


event_hub = EventHub()

