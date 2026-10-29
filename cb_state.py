"""Tracks which callback queries were already answered (Telegram accepts only one answer)."""
from collections import OrderedDict

_ANSWERED: "OrderedDict[str, bool]" = OrderedDict()
_MAX = 5000


def is_answered(cb_id) -> bool:
    return str(cb_id) in _ANSWERED


def mark_answered(cb_id, had_text: bool = False) -> None:
    _ANSWERED[str(cb_id)] = bool(had_text)
    while len(_ANSWERED) > _MAX:
        _ANSWERED.popitem(last=False)
