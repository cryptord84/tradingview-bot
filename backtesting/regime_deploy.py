"""Bull-roster deploy/undeploy recipe printer.

When the regime detector fires BULL_CONFIRMED via Telegram, run:

    venv/bin/python -m backtesting.regime_deploy bull

That outputs the full deploy recipe — symbols, slots, inputs — which a Claude
session uses with TV MCP tools to createAlert + restartAlerts for each combo.

For BULL_LOST:

    venv/bin/python -m backtesting.regime_deploy undeploy

The script does NOT call TV directly (Python-from-cron has no TV CDP context).
It emits the manifest + step-by-step recipe; the actual API calls happen via
MCP from a Claude session.

Manifest source: config/bull_roster.yaml (edit there to add/remove combos).
Deployed alert IDs (after deploy) are recorded to:
    config/bull_roster_deployed.json

so undeploy can find them later.
"""
from __future__ import annotations

import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

MANIFEST_FILE = "config/bull_roster.yaml"
DEPLOYED_FILE = "config/bull_roster_deployed.json"


def load_manifest() -> dict:
    with open(MANIFEST_FILE) as f:
        return yaml.safe_load(f)


def expand_inputs(template: dict, secret: str) -> dict:
    """Substitute {{webhook_secret}} placeholder in template."""
    out = {}
    for k, v in template.items():
        if isinstance(v, str) and v == "{{webhook_secret}}":
            out[k] = secret
        else:
            out[k] = v
    return out


def print_deploy_recipe(manifest: dict) -> None:
    print("=" * 78)
    print("  BULL-ROSTER DEPLOY RECIPE  (run in Claude session with TV MCP available)")
    print("=" * 78)
    print()
    print("Step 1 — verify TV is open and connected:")
    print("  mcp__tradingview__tv_health_check")
    print()
    print("Step 2 — for each combo, createAlert + restartAlerts via webpack 560065")
    print("         using the proven pattern from 2026-05-06 Donchian/RENDER deploy.")
    print()
    print("Step 3 — record returned alert_ids to config/bull_roster_deployed.json")
    print("         (so undeploy can find them).")
    print()
    print("Step 4 — verify with mcp__tradingview__alert_list (expect +7 alerts).")
    print()
    print("=" * 78)
    print("  COMBOS")
    print("=" * 78)

    secret = manifest["webhook_secret"]
    pine_features = manifest["pine_features"]
    slots = manifest["slots"]

    for i, c in enumerate(manifest["combos"], 1):
        slot = slots[c["slot"]]
        inputs = expand_inputs(slot["inputs_template"], secret)
        inputs["pineFeatures"] = pine_features

        print(f"\n[{i}/{len(manifest['combos'])}] {c['label']}")
        print(f"  symbol:        {c['symbol']}")
        print(f"  pine_id:       {slot['pine_id']}")
        print(f"  pine_version:  {slot['pine_version']}")
        print(f"  resolution:    240 (4H)")
        print(f"  bull_pf:       {c['bull_pf']}  ({c['bull_window']})")
        print(f"  bot_lane:      {c['bot_lane']}")
        print(f"  inputs:        {json.dumps(inputs, indent=4)}")

    print()
    print("=" * 78)
    print("  IMPORTANT")
    print("=" * 78)
    print("""
- All alerts should be created ACTIVE (call restartAlerts immediately after
  createAlert returns alert_id, since createAlert defaults to inactive).
- Some bot lanes (EVM for UNI/ARB/FLOKI) may not be live yet. Webhook signals
  will arrive but bot may reject with "no execution lane". That's a separate
  Phase 4 problem — not a blocker for alert deployment.
- After deploy, update Indicators/DEPLOYMENT.md with the new alert IDs.
""")


def print_undeploy_recipe() -> None:
    print("=" * 78)
    print("  BULL-ROSTER UNDEPLOY RECIPE")
    print("=" * 78)
    if not os.path.exists(DEPLOYED_FILE):
        print(f"\n  No {DEPLOYED_FILE} found — nothing to undeploy.")
        print("  (Either bull-roster was never deployed, or the file was lost.)")
        return

    with open(DEPLOYED_FILE) as f:
        deployed = json.load(f)

    ids = [d["alert_id"] for d in deployed.get("alerts", [])]
    print(f"\n  Found {len(ids)} deployed alert(s):")
    for d in deployed.get("alerts", []):
        print(f"    {d['alert_id']}  {d['label']}")
    print(f"\n  Step 1 — call deleteAlerts({ids}) via webpack 560065:")
    print(f"     window.__realReq(560065).getAlertsCollection()")
    print(f"       .deleteAlerts({json.dumps(ids)})")
    print(f"\n  Step 2 — remove {DEPLOYED_FILE}")
    print(f"  Step 3 — update DEPLOYMENT.md (note the undeploy date)")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["bull", "undeploy", "manifest"],
                        help="bull = print deploy recipe, undeploy = print teardown, manifest = dump expanded JSON")
    args = parser.parse_args()

    manifest = load_manifest()

    if args.action == "bull":
        print_deploy_recipe(manifest)
    elif args.action == "undeploy":
        print_undeploy_recipe()
    elif args.action == "manifest":
        # Expand inputs and dump as JSON for programmatic use
        secret = manifest["webhook_secret"]
        pine_features = manifest["pine_features"]
        slots = manifest["slots"]
        out = []
        for c in manifest["combos"]:
            slot = slots[c["slot"]]
            inputs = expand_inputs(slot["inputs_template"], secret)
            inputs["pineFeatures"] = pine_features
            out.append({
                "label": c["label"],
                "symbol": c["symbol"],
                "pine_id": slot["pine_id"],
                "pine_version": slot["pine_version"],
                "inputs": inputs,
                "resolution": "240",
                "bull_pf": c["bull_pf"],
            })
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
