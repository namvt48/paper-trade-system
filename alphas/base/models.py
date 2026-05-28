from dataclasses import dataclass, field


@dataclass
class SymbolData:
    price_list: list[float] = field(default_factory=list)
    volume_list: list[float] = field(default_factory=list)
    high_list: list[float] = field(default_factory=list)
    low_list: list[float] = field(default_factory=list)
    open_list: list[float] = field(default_factory=list)
    time_list: list[int] = field(default_factory=list)
