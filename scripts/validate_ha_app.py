from __future__ import annotations
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/"kraken_trade_real"
REQUIRED={"config.yaml","Dockerfile","run.sh","README.md","DOCS.md","CHANGELOG.md","apparmor.txt"}
missing=sorted(x for x in REQUIRED if not (APP/x).exists())
if missing: raise SystemExit(f"missing Home Assistant app files: {missing}")
cfg=yaml.safe_load((APP/"config.yaml").read_text(encoding="utf-8"))
for key in ("name","version","slug","description","arch"):
    if key not in cfg: raise SystemExit(f"config.yaml missing: {key}")
if cfg["arch"]!=["amd64","aarch64"]: raise SystemExit("arch must be amd64 and aarch64")
if cfg.get("homeassistant_api") is not True: raise SystemExit("homeassistant_api must be enabled")
if not isinstance(cfg.get("apparmor", True), bool): raise SystemExit("apparmor must be a boolean in the current Supervisor schema")
for entry in cfg.get("map", []):
    if isinstance(entry, dict) and entry.get("type") == "addon_config":
        raise SystemExit("addon_config map type is legacy; use app_config")
if cfg.get("host_network",False): raise SystemExit("host_network is not permitted")
if cfg.get("full_access",False): raise SystemExit("full_access is not permitted")
bad=[p for p in ROOT.rglob("config.yaml") if p!=APP/"config.yaml"]
if bad: raise SystemExit(f"unexpected nested config.yaml: {bad}")
docker=(APP/"Dockerfile").read_text(encoding="utf-8")
if "io.hass.type=\"app\"" not in docker or "io.hass.version" not in docker: raise SystemExit("Dockerfile missing HA labels")
print("Home Assistant app structure valid")
