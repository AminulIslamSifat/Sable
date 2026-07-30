import sys
import json
import os
import matplotlib.pyplot as plt
import numpy as np

def plot_graph(config, out_path):
    plt.figure(figsize=(10, 6))
    
    plots = config.get("plots", [])
    for p in plots:
        equation = p.get("equation")
        label = p.get("label", "")
        x_range = p.get("range", [0, 10])
        params = p.get("params", {})
        
        x = np.linspace(x_range[0], x_range[1], 1000)
        
        # Prepare evaluation environment
        eval_env = {**params, "np": np, "x": x, "sin": np.sin, "cos": np.cos, "tan": np.tan, "exp": np.exp, "log": np.log, "pi": np.pi}
        
        try:
            y = eval(equation, {"__builtins__": None}, eval_env)
            plt.plot(x, y, label=label)
        except Exception as e:
            print(f"Error plotting {equation}: {e}")

    plt.title(config.get("title", "Graph"))
    plt.xlabel(config.get("xlabel", "x"))
    plt.ylabel(config.get("ylabel", "y"))
    plt.grid(True)
    if plots:
        plt.legend()
    
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    plt.savefig(out_path)
    plt.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"status": "FAILED", "message": "Usage: plotter.py <payload_json_path>"}))
        sys.exit(1)
    
    payload_path = sys.argv[1]
    
    # Add project root to sys.path for config import
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../../../../"))
    if ROOT_DIR not in sys.path:
        sys.path.insert(0, ROOT_DIR)
    
    try:
        from config import ASSETS_DIR
    except ImportError:
        ASSETS_DIR = os.path.join(ROOT_DIR, "output", "assets")
    
    try:
        with open(payload_path, "r") as f:
            payload = json.load(f)
        
        attrs = payload.get("attrs", {})
        config_str = payload.get("content", "{}")
        config = json.loads(config_str)
        
        filename = attrs.get("filename", "graph.png")
        out_path = os.path.join(ASSETS_DIR, filename)
        
        plot_graph(config, out_path)
        print(json.dumps({"status": "SUCCESS", "message": f"Graph saved to {out_path}", "path": out_path}))
    except Exception as e:
        print(json.dumps({"status": "FAILED", "message": str(e)}))
        sys.exit(1)
