from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1] / "app"


def test_no_web_gui_or_legacy_version_chain():
    source = "\n".join(
        p.read_text(encoding="utf-8") for p in ROOT.rglob("*.py")
    ).lower()
    assert not any(
        x in source for x in ("flask", "jinja", "render_template", "v101", "v102", "v103")
    )


def test_trading_authority_is_only_order_sender():
    senders = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"submit_spot_order", "submit_futures_order"}
            ):
                senders.append(path.relative_to(ROOT).as_posix())
    assert senders
    assert set(senders) == {"trading/authority.py"}
