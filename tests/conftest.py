import os
import sys

# Plugins and the refresh task import their siblings as top level modules
# (`from utils.http_client import ...`), matching how src/inkypi.py runs the app.
SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
