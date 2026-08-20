"""Единый формат карточки из любого источника скаута (пункт 15.2).

Источники разные (Overpass, вставка из Ads Transparency, позже Places),
а дальше по конвейеру — site_probe, скоринг, ingest — едет одно и то же.
"""
from dataclasses import dataclass, field


@dataclass
class RawBiz:
    name: str
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    city: str | None = None
    source: str = "overpass"          # overpass | ads | places
    source_url: str = ""
    has_ads: bool = False
    # результат site_probe и скоринга дописываются конвейером
    probe: dict | None = None
    score: int = 0
    verdict: str = ""                 # candidate | review | reject
    reasons: list = field(default_factory=list)
