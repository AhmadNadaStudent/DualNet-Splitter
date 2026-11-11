"""Apply a metric preset to detected adapters (used by the legacy batch launchers)."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.network import generate_metric_ps_script, run_network_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preset", choices=("gaming", "download", "reset"))
    args = parser.parse_args()
    try:
        metrics = (50, 10) if args.preset == "download" else (10, 50)
        result = run_network_command(generate_metric_ps_script(*metrics, automatic=args.preset == "reset"))
        if result.returncode:
            print(result.stderr.strip() or "Metric change failed or elevation was cancelled.")
            return 1
        print("Metric preset applied:", args.preset)
        return 0
    except Exception as error:
        print("Could not apply metric preset:", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
