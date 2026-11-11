"""Launch a discovered Steam installation with the optional local proxy flag."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.apps import find_steam

steam = find_steam()
if not steam:
    raise SystemExit("Steam was not found. Install Steam or add it to PATH.")
subprocess.Popen([steam, "-proxy-server=http://127.0.0.1:8080"])
print("Steam opened. Proxy flag support depends on the Steam version; prefer TUN routing.")
