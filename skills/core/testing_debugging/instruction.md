
# Testing & Debugging Discipline

> [!IMPORTANT]
> **The Prime Directive: measure before you theorize.**
> A plausible guess is NOT a diagnosis. Never present a hypothesis as a finding
> until you have reproduced the symptom and measured the actual behavior.
> "It should be X" is forbidden until you have *shown* it is X.

This skill governs HOW you investigate bugs and verify fixes — for any code,
endpoint, script, or system behavior. It has no scripts. It is a discipline.
Load it the moment something "doesn't work" and before you claim a fix is done.

***

## The Debug Loop (non-negotiable order)

~~~mermaid
flowchart TD
    A["1. Reproduce"] --> B["2. Isolate"]
    B --> C["3. Measure"]
    C --> D["4. Hypothesize"]
    D --> E["5. Verify hypothesis"]
    E --> F{"Confirmed?"}
    F -- No --> C
    F -- Yes --> G["6. Fix"]
    G --> H["7. Re-reproduce: symptom gone?"]
    H -- No --> C
    H -- Yes --> I["Done"]
~~~
> [!EXAMPLE]
> Reproduce the error → isolate which component → measure real timing/output →
> form ONE hypothesis → test it → only then fix → confirm the original symptom is gone.

**Skipping steps is the bug.** Most failed debugging sessions jump from
"symptom" straight to "fix" with a guessed cause in between. Do not.

***

## Step 1 — Reproduce

Get the symptom to happen on command, with your own eyes (logs/output), before
touching anything.

- **Repro scripts live in `PROJECT_ROOT/test/` as `test_<thing>.py`** — never as
  throwaway `python3 -c` one-liners that vanish. If it's worth running, it's
  worth keeping as a reusable regression test.
- Run the exact failing operation. Capture the **real** error text / traceback / status code.
- If you cannot reproduce it, you cannot debug it — say so. Do not guess at a bug you have not seen.
- Note the conditions: auth state, input data, timing, which process is running.

> [!WARNING]
> Read the ACTUAL error. `[Errno 2] No such file or directory: 'SingletonLock'`
> tells you the truth. "Probably a timeout" tells you nothing.

***

## Step 2 — Isolate (the Control Experiment)

Narrow the blast radius by comparing against a known-good baseline. Change ONE
variable at a time.

| Question | Control test |
|---|---|
| Is the whole server frozen, or just this endpoint? | Hit a trivial endpoint (`/api/settings/browser`) — if it's instant, the loop is fine |
| Is it auth or the handler? | Call unauthenticated (expect fast 401) vs authenticated |
| Is it the code or the data? | Run the same operation on a tiny/empty input |
| Is it this layer or the one below? | Call the function directly in a REPL, bypassing HTTP |

The control tells you where the problem is NOT — which is half the battle.

***

## Step 3 — Measure

Quantify the real behavior. Replace adjectives ("slow", "big") with numbers.

- **Time it:** `time python3 -c "..."` or `curl -w "%{time_total}s"`.
- **Size it:** file counts, byte totals, row counts.
- **Run the operation standalone**, outside the server/app, to get a clean number
  free of framework noise.

> [!TIP]
> Standalone reproduction is the single most powerful move. Copy the suspect
> operation into a throwaway `python3 -c` snippet with the real data. If it
> crashes there, you have the bug in a bottle — no server, no auth, no guessing.

***

## Step 4–5 — Hypothesize & Verify (ONE at a time)

- Form a **single** hypothesis from your measurements — not three.
- Design a test that would **falsify** it. Run that test.
- If the test disproves it, discard it cleanly. Do not patch a wrong theory.

> [!CAUTION]
> **Never stack untested hypotheses.** "It's timing, and also the event loop,
> and also the symlinks" means you have tested none of them. One hypothesis →
> one test → one verdict. Then move on.

***

## Step 6–7 — Fix & Re-verify

A fix is a claim. Verify the claim.

- **Necessary but NOT sufficient:** `py_compile` / syntax check passing only
  proves it parses. It proves nothing about behavior.
- **Sufficient:** the *original symptom* is reproduced, then gone, on the *real*
  path (real auth, real data, real process) — not a toy version.
- **Check what is actually RUNNING.** Code on disk ≠ code in memory. If the
  service started before your edit, your fix is not live yet. Confirm process
  start time vs edit time before testing a "fix".
- **Leave a regression test behind.** The repro script from Step 1 becomes a
  permanent `test/test_<thing>.py` that asserts the bug stays fixed. A bug
  fixed without a regression test is a bug waiting to come back.

***

## Anti-Patterns (how debugging sessions die)

> [!FAILURE]
> These are the failure modes. If you catch yourself doing one, STOP and go back
> to Step 1.

- **Narrating guesses as findings.** "The walk is slow" — without timing it.
- **Fixing before confirming the root cause.** Editing code while the real bug is still unidentified.
- **Trusting "it should work".** It either does (measured) or it doesn't.
- **Testing a toy path and declaring victory.** Hitting an unauthenticated endpoint and assuming the authed path behaves the same.
- **Self-doubting a verified solution.** Once measured and confirmed, stop second-guessing. Move on.
- **Repeating the same untested theory louder.** If a guess didn't pan out, measure something new.

***

## Quick Checklist (before you say "fixed")

- [ ] I reproduced the original symptom myself
- [ ] I read the real error text, not an assumed one
- [ ] I isolated it with a control experiment
- [ ] I measured the actual behavior with a number
- [ ] I confirmed the root cause with a falsifiable test
- [ ] I tested the fix on the REAL path (auth + real data)
- [ ] The running process actually has my change (start time > edit time)
- [ ] The original symptom is now gone, demonstrated not assumed
- [ ] A regression test lives in `test/test_<thing>.py` so this can't silently return
