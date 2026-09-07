"""sweep/hub/api/analysis.py -- read-only analysis. `check` in M1; rank, categorise, invert, render, derive-ladders, calibrate are M4's."""
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from sweep import model_check as mc
from sweep.hub.api.common import OK, get_store
from sweep.hub.refusals import check_refusal

router = APIRouter(prefix="/analysis", tags=["analysis"])


def firing_text(store, firing):
    """The first firing check in the refusal's form, then one line per other firing check."""
    lines = []
    for i, (name, rows) in enumerate(firing.items()):
        text = store.checks[name] if name in store.checks else dict(zip(("check", "fix"), mc.SCRIPT_CHECKS[name]))
        r = check_refusal(name, text["check"], rows, text["fix"], [])
        lines.append(str(r) if i == 0 else f"{r.what} -- {r.fix}")
    return "\n".join(lines)


@router.get("/check")
def check(store=Depends(get_store)):
    """Every check over the whole store: ok, or the firing ones with their fixes. Nothing is written."""
    firing = store.check()
    if firing:
        return PlainTextResponse(firing_text(store, firing), status_code=422)
    return OK
