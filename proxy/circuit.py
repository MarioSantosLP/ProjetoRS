import logging
import time

log = logging.getLogger("proxy.circuit")

FAILURE_THRESHOLD = 3   # failures within the window to open the circuit
FAILURE_WINDOW    = 10  # seconds to look back when counting failures
RECOVERY_TIMEOUT  = 30  # seconds to wait in OPEN before trying HALF_OPEN


class CircuitBreaker:
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"

    def __init__(self) -> None:
        self.state          = self.CLOSED
        self.failure_count  = 0
        self.last_failure   = 0.0  # timestamp of the most recent failure
        self.opened_at      = 0.0  # timestamp of when the circuit opened

    def is_open(self) -> bool:
        if self.state == self.CLOSED:
            return False

        if self.state == self.OPEN:
            if time.time() - self.opened_at >= RECOVERY_TIMEOUT:
                self.state = self.HALF_OPEN  # let one request through as a probe
                return False
            return True

        # HALF_OPEN — let the request through
        return False

    def record_failure(self) -> None:
        now = time.time()

        # reset counter if the last failure was outside the window
        if now - self.last_failure > FAILURE_WINDOW:
            self.failure_count = 0

        self.failure_count += 1
        self.last_failure   = now

        if self.state == self.HALF_OPEN or self.failure_count >= FAILURE_THRESHOLD:
            if self.state != self.OPEN:  # don't reset recovery timer if already open
                self.opened_at = now
            self.state = self.OPEN
            self.failure_count = 0
            log.warning(f"Circuit OPEN (will retry in {RECOVERY_TIMEOUT}s)")

    def record_success(self) -> None:
        # only relevant when recovering — reset everything
        if self.state != self.CLOSED:
            log.info("Circuit CLOSED (container recovered)")
        self.state         = self.CLOSED
        self.failure_count = 0
        self.last_failure  = 0.0
        self.opened_at     = 0.0

    @property
    def current_state(self) -> str:
        self.is_open()  # trigger OPEN → HALF_OPEN transition if timeout expired
        return self.state    