from __future__ import annotations
import ast
from pathlib import Path
ROOT=Path(__file__).parents[1]/"app"
def test_no_web_gui_or_legacy_version_chain():
    source="\n".join(p.read_text(encoding="utf-8") for p in ROOT.rglob("*.py")).lower()
    assert not any(x in source for x in ("flask","jinja","render_template","v101","v102","v103"))
def test_trading_authority_is_only_order_sender():
    hits=[]
    for p in ROOT.rglob("*.py"):
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in {"submit_spot_order","submit_futures_order"}:
                hits.append(p.relative_to(ROOT).as_posix())
    assert hits==["trading/authority.py"]
