# Math Solver: Symbolic Mathematics Engine

This skill performs precise symbolic mathematical operations using a SymPy-backed solver.
It is invoked in two contexts: **standalone** (Sifat asks for a derivation or computation
directly) and **inline** (called mid-response by another skill, such as Proof Verifier
during a logical audit). The protocol differs between these two contexts — read both.

---

## The `<solve_math>` Tag

```xml
<solve_math>
{
  "task":    "{task_type}",
  "expr":    "{expression}",
  "wrt":     "{variable}",
  "symbols": ["{sym1}", "{sym2}", "..."],
  "steps":   true | false,
  "eq":      "{rhs expression}"
}
</solve_math>
```

### Parameter Reference

| Parameter | Type | Required | Description |
|---|---|---|---|
| `task` | string | Always | Operation to perform. See Task Type Reference below. |
| `expr` | string | Always | The mathematical expression in SymPy-compatible syntax. |
| `wrt` | string | Conditionally | Variable to differentiate or integrate with respect to. Required for `derive`, `integrate`, `limit`. Omit for `simplify`. |
| `symbols` | array of strings | When non-standard | Declare all symbols that are not `x`, `y`, `z`, `t`, `n`. Always declare physics constants and Greek-letter variables. |
| `steps` | boolean | Optional | `true` requests intermediate working. Behavior is task-specific — see Task Type Reference. Default: `false`. |
| `eq` | string | For `solve` and `diff_eq` only | The right-hand side of the equation. The solver treats the problem as `expr = eq`. Omit for all other tasks. |

### SymPy Expression Syntax Rules

Always write expressions in SymPy-compatible Python syntax:

| Math notation | SymPy syntax |
|---|---|
| $A\sin(\omega t + \phi)$ | `A * sin(omega * t + phi)` |
| $e^{-kt}$ | `exp(-k * t)` |
| $\frac{x^2 + 1}{x - 3}$ | `(x**2 + 1) / (x - 3)` |
| $\sqrt{x^2 + y^2}$ | `sqrt(x**2 + y**2)` |
| $\ln x$ | `log(x)` |
| $\frac{d}{dx}f(x)$ | handled by `task: derive`, `wrt: x` |

Never use `^` for exponentiation — SymPy will misparse it. Always use `**`.

---

## Task Type Reference

### `derive` — Differentiation

Computes $\frac{d}{d(\text{wrt})}(\text{expr})$.

**Required:** `expr`, `wrt`
**Optional:** `symbols`, `steps`
**`steps: true` behavior:** Returns the result plus the differentiation rule applied
(chain rule, product rule, quotient rule) identified by name.
**Do not use for:** Partial derivatives of multivariate expressions where the
variable of differentiation is ambiguous — always supply `wrt` explicitly.

```xml
<solve_math>
{
  "task":    "derive",
  "expr":    "A * sin(omega * t + phi)",
  "wrt":     "t",
  "symbols": ["A", "omega", "t", "phi"],
  "steps":   true
}
</solve_math>
```

---

### `integrate` — Integration

Computes $\int \text{expr} \, d(\text{wrt})$ (indefinite) or a definite integral
if bounds are embedded in the expression string.

**Required:** `expr`, `wrt`
**Optional:** `symbols`, `steps`
**`steps: true` behavior:** Returns the technique used (u-substitution, integration
by parts, partial fractions, trigonometric substitution) identified by name, plus
the intermediate substitution or split if applicable.
**Definite integrals:** Express bounds in the `expr` field using SymPy's
`Integral(expr, (var, lower, upper))` syntax, and set `task: integrate` with no `wrt`
(the bounds carry the variable).

```xml
<solve_math>
{
  "task":    "integrate",
  "expr":    "x * exp(-x**2)",
  "wrt":     "x",
  "steps":   true
}
</solve_math>
```

---

### `simplify` — Algebraic Simplification

Reduces `expr` to its simplest form using SymPy's `simplify()` pipeline (trigonometric
identities, rational simplification, cancellation).

**Required:** `expr`
**Do not supply:** `wrt`, `eq`
**Optional:** `symbols`, `steps`
**`steps: true` behavior:** Returns the simplification class applied
(trigsimp, ratsimp, cancel, expand) and the intermediate form before final reduction.
**When to use:** After a multi-step derivation produces a complex intermediate expression
that needs reduction before the next step. Do not use as a substitute for showing working.

```xml
<solve_math>
{
  "task":    "simplify",
  "expr":    "sin(x)**2 + cos(x)**2",
  "symbols": ["x"]
}
</solve_math>
```

---

### `solve` — Equation Solving

Solves `expr = eq` for `wrt`. Returns all solutions including complex roots.

**Required:** `expr`, `eq`, `wrt`
**Optional:** `symbols`, `steps`
**`steps: true` behavior:** Returns the method used (factoring, quadratic formula,
substitution) and intermediate factored or rearranged form.
**Multiple solutions:** The engine returns all solutions as an array. In the formatted
response, present each solution on its own line with a case label.

```xml
<solve_math>
{
  "task":    "solve",
  "expr":    "x**2 - 5*x",
  "eq":      "6",
  "wrt":     "x",
  "steps":   true
}
</solve_math>
```

---

### `limit` — Limit Evaluation

Computes $\lim_{\text{wrt} \to \text{point}} \text{expr}$.

**Required:** `expr`, `wrt`
**The limit point:** Embed it in the `expr` string using SymPy's
`Limit(expr, var, point)` syntax, or append the point as a comment in `symbols`:
supply `"wrt": "x"` and include `"point": "0"` as an additional key (engine extracts it).
**Optional:** `symbols`, `steps`
**`steps: true` behavior:** Returns L'Hôpital applications (if used) and the
indeterminate form encountered before resolution.

```xml
<solve_math>
{
  "task":    "limit",
  "expr":    "sin(x) / x",
  "wrt":     "x",
  "point":   "0",
  "symbols": ["x"]
}
</solve_math>
```

---

### `diff_eq` — Differential Equation Solving

Solves `expr = eq` as an ODE where `expr` contains derivative terms.
Uses SymPy's `dsolve()`.

**Required:** `expr`, `eq`, `wrt`
**Optional:** `symbols`, `steps`
**`steps: true` behavior:** Returns the ODE classification (separable, linear first-order,
second-order homogeneous, etc.) and the homogeneous and particular solutions separately
before combining.
**Boundary conditions:** If known, append them to `symbols` as string entries in the
format `"y(0)=1"` — the engine parses and applies them.

```xml
<solve_math>
{
  "task":    "diff_eq",
  "expr":    "f(t).diff(t, 2) + omega**2 * f(t)",
  "eq":      "0",
  "wrt":     "t",
  "symbols": ["omega", "t"],
  "steps":   true
}
</solve_math>
```

---

## Invocation Protocols

### Protocol A — Standalone Invocation

Sifat asks directly: "derive X", "integrate Y", "solve Z", "simplify this expression."

**Flow:**
1. Identify the correct task type from the Task Type Reference.
2. Construct the `<solve_math>` tag.
3. **Fire the tag. Stop. Do not write the result.**
4. Wait for the engine to return the SymPy output.
5. On success: format the result as a `$$ ... $$` LaTeX block with a one-sentence
   plain-English interpretation below it.
6. On failure: see Engine Failure Handling below.

**Response structure after engine success:**

```markdown
**Result:**

$$
{LaTeX result from engine}
$$

**Interpretation:** {One sentence explaining what the result means in context —
e.g., "This is the velocity, showing the system oscillates with amplitude $A\omega$
and is $90°$ ahead of the displacement in phase."}
```

If `steps: true` was used, prepend the steps block before the result:

```markdown
**Method:** {Rule or technique name}

**Intermediate form:**
$$
{intermediate LaTeX}
$$

**Result:**
$$
{final LaTeX}
$$
```

---

### Protocol B — Inline Invocation (called by another skill)

Triggered mid-response by Proof Verifier (Phase 3) or Study Suite (problem solution
verification). The tag fires as part of a larger response — the calling skill does not
pause for a separate engine-return message.

**Inline output format** — compact, no full interpretation paragraph:

```markdown
**Symbolic check (Step N → N+1):**
$$
{LaTeX result}
$$
{One clause: "Matches Sifat's Step N+1" or "Does not match — engine gives [result]
vs Sifat's [what he wrote]."}
```

**Rules for inline use:**
- Always use `steps: false` in inline calls — intermediate steps clutter the calling
  skill's output.
- The calling skill owns the error verdict. Math Solver reports the symbolic
  result only — it does not issue PASS or FLAG judgments inline.
- If the engine fails inline, report: `[Symbolic check unavailable: {error}]` and
  allow the calling skill to continue its audit without the programmatic result.

---

## Engine Failure Handling

The engine can fail in four ways. Handle each explicitly:

| Failure type | Symptom | Action |
|---|---|---|
| **Parse error** | Engine returns `SympifyError` or `TokenError` | Report the exact error. Re-examine the `expr` string for syntax issues (missing `*`, `^` instead of `**`, undefined symbol). Fix and retry once. If it fails again, report to Sifat and do not retry blindly. |
| **Timeout** | Engine hangs beyond 15 seconds | Treat as failure. Report: *"SymPy timed out on this expression — it may be too complex for automated solving. Proceeding with manual derivation."* Then perform the operation manually, showing all steps. |
| **No solution found** | Engine returns empty solution set for `solve` or `diff_eq` | Report: *"SymPy returned no closed-form solution. The equation may require numerical methods."* Do not fabricate a solution. |
| **Unexpected output** | Engine returns a value that cannot be parsed as LaTeX | Report the raw output to Sifat verbatim. Do not attempt to interpret or reformat it. |

---

## Symbol Declaration Reference

Always declare these explicitly in `symbols` — never assume SymPy will infer them:

| Domain | Symbols to declare |
|---|---|
| Classical mechanics | `m`, `g`, `k`, `omega`, `phi`, `A`, `F`, `v`, `a` |
| Electrostatics | `epsilon_0`, `q`, `Q`, `r`, `E`, `V`, `k_e` |
| Thermodynamics | `T`, `P`, `V`, `n`, `R`, `k_B`, `S`, `U` |
| Wave mechanics | `omega`, `k`, `lambda_`, `phi`, `A`, `c` |
| Pure mathematics | Only declare if not in `{x, y, z, t, n, i}` |

Note: `lambda` is a reserved Python keyword. Always use `lambda_` in SymPy expressions.

---

## Global Rules

1. **Never write the result before the engine returns it** in standalone invocations.
   The engine output is the result — do not pre-compute or anticipate it.
2. **Never fabricate a SymPy result.** If the engine fails, say so and fall back to
   manual derivation with explicit working shown.
3. **Always use `steps: true` for standalone derivations** unless Sifat asks for just
   the answer. The method name and intermediate form are as important as the result.
4. **Always use `steps: false` for inline invocations.** Conciseness is required when
   embedded in another skill's output.
5. **One `<solve_math>` tag per response.** If a derivation requires multiple operations
   (e.g., differentiate, then simplify the result), chain them across separate responses
   or perform the second step manually using the engine's output from the first.
6. **Declare all non-standard symbols.** An undeclared symbol causes SymPy to either
   throw a parse error or treat it as a free variable, silently producing wrong results.