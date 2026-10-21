"""Plain data containers shared across layers."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Order:
    symbol: str
    direction: int            # +1 long, -1 short
    lots: float
    entry_ref: float          # reference price at decision time (last close)
    stop: float
    tp: float
    risk_usd: float           # loss if stopped out, spread included, commissions excluded


@dataclass
class Position:
    symbol: str
    direction: int
    lots: float
    entry: float              # actual fill price
    stop: float
    tp: float
    entry_ts: int
    risk_usd: float
    meta: dict = field(default_factory=dict)


@dataclass
class AccountState:
    equity: float
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    day_id: int = -1
    trades_today: int = 0
    positions: dict = field(default_factory=dict)   # symbol -> Position

    def __post_init__(self):
        if self.peak_equity <= 0:
            self.peak_equity = self.equity
        if self.day_start_equity <= 0:
            self.day_start_equity = self.equity

    def mark(self, equity: float, now_ts: int):
        """Update equity, peak and the UTC day roll. Call once per bar, before decisions."""
        day = now_ts // 86400
        if self.day_id == -1:
            self.day_id = day
        elif day != self.day_id:
            self.day_id = day
            self.day_start_equity = self.equity     # last mark of the previous day
            self.trades_today = 0
        self.equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity

    @property
    def dd_pct(self) -> float:
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity * 100.0)

    @property
    def day_loss_pct(self) -> float:
        return max(0.0, (self.day_start_equity - self.equity) / self.day_start_equity * 100.0)

    @property
    def open_risk_usd(self) -> float:
        return sum(p.risk_usd for p in self.positions.values())
