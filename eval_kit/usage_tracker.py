"""Thread-safe LLM cost tracker with periodic user warnings."""

import logging
import os
import sys
import threading
from decimal import Decimal

logger = logging.getLogger(__name__)


class CostLimitAborted(BaseException):
    """Raised when the user declines to continue after a cost warning, or when
    running non-interactively and the cost threshold is reached."""


class UsageTracker:
    """Singleton that accumulates LLM spend and prompts the user every
    COST_WARNING_THRESHOLD dollars (default $5)."""

    def __init__(self) -> None:
        interval_str = os.environ.get("COST_WARNING_THRESHOLD", "5")
        try:
            self._threshold_interval = Decimal(interval_str)
        except Exception:
            logger.warning(
                "Invalid COST_WARNING_THRESHOLD=%r — defaulting to $5.", interval_str
            )
            self._threshold_interval = Decimal("5")

        self._cost_lock = threading.Lock()
        self._prompt_lock = threading.Lock()
        self._total_cost: Decimal = Decimal(0)
        self._next_threshold: Decimal = self._threshold_interval
        self._rubric_accepted: int = 0
        self._abort: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def total_cost(self) -> Decimal:
        with self._cost_lock:
            return self._total_cost

    @property
    def is_aborted(self) -> bool:
        with self._cost_lock:
            return self._abort

    def add_cost(self, amount: Decimal) -> None:
        """Add *amount* (in USD) to the running total.

        Raises CostLimitAborted if the user declines to continue or if
        running non-interactively when the threshold is crossed.
        """
        with self._cost_lock:
            if self._abort:
                raise CostLimitAborted()
            self._total_cost += amount
            should_check = self._total_cost >= self._next_threshold

        if should_check:
            self._handle_threshold()

    def set_rubric_accepted(self, count: int) -> None:
        """Update the live count of rubric-accepted PRs (shown in warnings)."""
        with self._cost_lock:
            self._rubric_accepted = count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_threshold(self) -> None:
        with self._prompt_lock:
            # Another thread may have already bumped the threshold — re-check.
            with self._cost_lock:
                if self._total_cost < self._next_threshold:
                    return
                if self._abort:
                    raise CostLimitAborted()
                total = float(self._total_cost)
                rubric_count = self._rubric_accepted

            if not sys.stdin.isatty():
                logger.warning(
                    "LLM cost reached $%.2f — non-interactive mode, aborting.", total
                )
                self._abort = True
                raise CostLimitAborted()

            print(f"\n{'=' * 60}")
            print(f"WARNING: ${total:.2f} has been spent on LLM usage.")
            if rubric_count > 0:
                print(
                    f"You have {rubric_count} rubric-accepted PR(s) so far. "
                    "If you stop now, this repo will probably be rejected. "
                    "It is recommended to continue."
                )
            response = input("Do you want to continue? [y/N]: ").strip().lower()
            print("=" * 60)

            if response not in ("y", "yes"):
                self._abort = True
                raise CostLimitAborted()

            # User said yes — advance threshold past current spend.
            with self._cost_lock:
                while self._next_threshold <= self._total_cost:
                    self._next_threshold += self._threshold_interval

        with self._cost_lock:
            if self._abort:
                raise CostLimitAborted()


_tracker: UsageTracker | None = None
_tracker_lock = threading.Lock()


def get_tracker() -> UsageTracker:
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = UsageTracker()
    return _tracker
