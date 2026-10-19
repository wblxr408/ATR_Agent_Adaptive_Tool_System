from __future__ import annotations

import threading
import time

from atr.storage import Store


class Cancellation(threading.Event):
    """Local cancellation plus bounded polling of durable ancestor cancellation."""

    def __init__(self, store: Store, run_id: str) -> None:
        super().__init__()
        self.store, self.run_id = store, run_id
        self.next_poll = 0.0

    def is_set(self) -> bool:
        if super().is_set():
            return True
        if time.monotonic() >= self.next_poll:
            self.next_poll = time.monotonic() + .05
            row = self.store.one("""WITH RECURSIVE ancestors(id,parent,cancel_requested) AS (
                SELECT id,parent,cancel_requested FROM runs WHERE id=? UNION ALL
                SELECT r.id,r.parent,r.cancel_requested FROM runs r
                JOIN ancestors a ON a.parent=r.id)
                SELECT MAX(cancel_requested) AS cancelled FROM ancestors""", (self.run_id,))
            if row and row["cancelled"]:
                self.set()
        return super().is_set()
