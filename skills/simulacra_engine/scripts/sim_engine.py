import sys
import os
import json

# Add project root to sys.path for config import
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../../../"))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

try:
    from config import ASSETS_DIR
except ImportError:
    ASSETS_DIR = os.path.join(ROOT_DIR, "output", "assets")

def run_simulation(out_path, code):
    try:
        # We execute the provided code. The code is expected to generate 
        # some content and we save it to out_path.
        namespace = {"OUT_PATH": out_path}
        exec(code, namespace)
        
        if os.path.exists(out_path):
            print(json.dumps({"status": "SUCCESS", "message": f"Simulation saved to {out_path}"}))
        else:
            print(json.dumps({"status": "FAILED", "message": "Code executed but no file was created at OUT_PATH"}))
            
    except Exception as e:
        print(json.dumps({"status": "FAILED", "message": str(e)}))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"status": "FAILED", "message": "Usage: sim_engine.py <payload_json_path>"}))
        sys.exit(1)
    
    payload_path = sys.argv[1]
    try:
        with open(payload_path, "r") as f:
            payload = json.load(f)
        
        attrs = payload.get("attrs", {})
        code = payload.get("content", "")
        filename = attrs.get("filename", "simulation.html")
        
        out_path = os.path.join(ASSETS_DIR, filename)
        os.makedirs(ASSETS_DIR, exist_ok=True)
        
        run_simulation(out_path, code)
    except Exception as e:
        print(json.dumps({"status": "FAILED", "message": f"Payload error: {e}"}))
#
