import sys
import json
import os
import sympy as sp
from sympy import latex, sympify, diff, integrate, solve, simplify, limit, Function, Eq

def run_task(params):
    task = params.get("task", "simplify")
    expr_str = params.get("expr", "0")
    wrt_str = params.get("wrt", "x")
    symbols_list = params.get("symbols", [])
    show_steps = params.get("steps", False)

    # Initialize symbols
    syms = {}
    if symbols_list:
        for s in symbols_list:
            syms[s] = sp.symbols(s)
    
    # Standard fallback symbols
    x, y, z, t = sp.symbols('x y z t')
    syms.update({'x': x, 'y': y, 'z': z, 't': t})

    try:
        # Parse expression safely
        expr = sympify(expr_str, locals=syms)
        wrt = sp.symbols(wrt_str)
        
        result = None
        description = ""

        if task == "derive":
            result = diff(expr, wrt)
            description = f"Derivative of ${latex(expr)}$ w.r.t ${latex(wrt)}$"
        elif task == "integrate":
            result = integrate(expr, wrt)
            description = f"Integral of ${latex(expr)}$ w.r.t ${latex(wrt)}$"
        elif task == "solve":
            # Assume expr = 0 if it's not an Eq
            if not isinstance(expr, Eq):
                equation = Eq(expr, 0)
            else:
                equation = expr
            result = solve(equation, wrt)
            description = f"Solutions for ${latex(equation)}$"
        elif task == "simplify":
            result = simplify(expr)
            description = f"Simplified form of ${latex(expr)}$"
        elif task == "limit":
            # For limits, we might need a destination
            at = params.get("at", 0)
            result = limit(expr, wrt, at)
            description = f"Limit of ${latex(expr)}$ as ${latex(wrt)} \\to {at}$"
        else:
            raise ValueError(f"Unknown task: {task}")

        output = {
            "status": "SUCCESS",
            "description": description,
            "result_latex": latex(result),
            "result_str": str(result)
        }
        
        if show_steps and task in ["derive", "integrate"]:
            # Basic SHM step logic if applicable
            if "sin" in expr_str or "cos" in expr_str:
                output["note"] = "Chain rule applied to trigonometric oscillation."

        return output

    except Exception as e:
        return {"status": "FAILED", "error": str(e)}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"status": "FAILED", "error": "Usage: solver.py <payload_json_path>"}))
        sys.exit(1)
    
    payload_path = sys.argv[1]
    
    try:
        # Detect if arg is a path or raw JSON
        if os.path.exists(payload_path):
            with open(payload_path, "r") as f:
                payload = json.load(f)
        else:
            payload = json.loads(payload_path)
            
        # The dispatcher wraps content in {"attrs": ..., "content": ...}
        # solver.py expects the content to be the JSON params
        content = payload.get("content", "{}")
        if isinstance(content, str):
            params = json.loads(content)
        else:
            params = content
            
        output = run_task(params)
        print(json.dumps(output, indent=2))
    except Exception as e:
        print(json.dumps({"status": "FAILED", "error": str(e)}))
        sys.exit(1)
