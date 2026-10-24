# `sable-tools/` — The Sealed Vault System

> [!IMPORTANT] What this is
> A **self-contained cryptographic packaging system** that turns the entire Sable source tree into a single signed, encrypted `vault.sable` blob and boots it back into **tmpfs** with no plaintext ever touching disk. It's the anti-tamper / anti-piracy layer. Nothing in `engine/` or `server/` imports it at runtime — it's a launcher-adjacent subsystem.

***

## Layout

| File | Lines | Role |
|:--|--:|:--|
| `sable_crypto.py` | 199 | **Shared primitives.** Both sides import this so key derivation can never drift |
| `seal.py` | 250 | Encrypt a tree → `vault.sable` |
| `boot.py` | 225 | Decrypt `vault.sable` → tmpfs → optionally spawn server |
| `encrypt_tree.py` | 65 | Per-file `.enc` blobs for the import-hook path |
| `vimport.py` | 68 | Decrypt-on-import module finder (plaintext never hits disk) |
| `sable-seal` / `sable-boot` | 15 / 17 | Bash shims that `uv run --with httpx --with cryptography --with pynacl` |
| `starter/main.go` | 795 | **Blob A** — compiled trust root. The production boot path |
| `starter/proc_{linux,windows,unix_other}.go` | 27/28/21 | Platform process-group handling |

> [!TIP] Why Go for the starter
> The Python `boot.py` is a readable reference implementation. The Go `sable-starter` is what actually ships — it's a single static binary with the trusted signing pubkey **pinned at build time**, so an attacker can't just edit `boot.py` and bypass verification.

***

## The two vault versions

```
v3 : file_key = HKDF-SHA256(master, salt=None, info="sable/v3/file/" + path)
v4 : file_key = HKDF-SHA256(master, salt=KEK,   info="sable/v3/file/" + path)
     KEK      = Argon2id(passphrase, vault_salt)
```

| | v3 | v4 |
|:--|:--|:--|
| Secrets needed | Bridge master DEK | Bridge DEK **+** user passphrase |
| Magic | `SABLEV03` | `SABLEV04` |
| `alg` | `chacha20poly1305-hkdf-perfile` | `…-argon2` |
| Signed fields | `v, created, alg, manifest` | `v, created, alg, **key_hash, salt, kdf**, manifest` |
| Passphrase | none | Argon2id / PBKDF2 fallback |

> [!WARNING] The v4 signing trick
> `key_hash`, `salt` and `kdf` are inside the Ed25519 signature in v4 **specifically so a downgrade attack is loud**. Strip the salt to force v3 and the signature fails immediately.

***

## `sable_crypto.py` — the shared contract

Everything both `seal.py` and `boot.py` must agree on lives here. If these two sides diverge, nothing decrypts.

| Constant | Value | Note |
|:--|:--|:--|
| `ARGON2_TIME` | 3 | |
| `ARGON2_MEM_KIB` | 256 × 1024 | 256 MiB — ~0.5–1 s/guess |
| `ARGON2_LANES` | 4 | |
| `PBKDF2_ITERS` | 600 000 | fallback when no Argon2 backend |
| `SALT_LEN` / `KEK_LEN` | 16 / 32 | |
| `KEYRING_SERVICE` | `sable-vault` | secret-tool / macOS `security` |

**Functions:** `new_salt()`, `pick_kdf()`, `derive_kek(passphrase, salt, kdf)`, `derive_file_key(master, path, kek=None)`, `keyring_lookup(account)`, `keyring_store(account, secret)`.

**Argon2 backend ladder** — `cryptography`'s `Argon2id` → `argon2-cffi`'s `hash_secret_raw` → hard error. Both use the *exact* same params so the derived KEK is byte-identical regardless of which backend ran.

> [!CAUTION] `derive_kek` never auto-detects on the boot side
> It uses the `kdf` string recorded in the header verbatim. Auto-detecting "what this machine supports" would silently derive a **different key** and produce an unexplainable decryption failure.

***

## `seal.py` — packing

```
main()
  ├─ collect(root)          → file list
  ├─ resolve_passphrase()   → v3 or v4 decision
  ├─ fetch_dek(bridge)      → GET /v1/key, Bearer token, hex DEK
  ├─ per file: derive key → ChaCha20-Poly1305(nonce, data)
  ├─ manifest { files[], root, count, verified }
  └─ Ed25519-sign → header + blobs → vault.sable
```

### File selection — `.gitignore` is the authority

`git_visible()` runs `git ls-files --cached --others --exclude-standard -z` and treats that output as the allowlist. Nested `.gitignore`s, negation patterns and `core.excludesFile` all count for free — no reimplementation of gitignore semantics.

Hard skips regardless of git state: `.venv .git __pycache__ .pytest_cache .sable_backups .editor_tools_backups node_modules .mypy_cache .ruff_cache .idea .vscode fastembed_cache models sable-tools test tests output dist`, plus prefixes `browser-data-`, `browser-scraper-data`, `browser-automation-data`, plus files `.sable_server.pid vault.sable .DS_Store`.

### Hash verification is selective

```python
NO_VERIFY_PREFIXES = ("Brain/", "instruction/", "system/")
```

Those three directories are **encrypted but not hash-checked** because they mutate at runtime (memory writes, persona edits, session tokens). Every other file gets a SHA-256 in the manifest and is verified on boot.

### Passphrase resolution (seal side)

`--no-passphrase` → `--passphrase` → `--passphrase-file` → `$SABLE_PASSPHRASE` → interactive `getpass` with confirmation → **blank means v3**.

### Vault file format

```
[8 bytes magic][4 bytes BE header length][header JSON][blob_0][blob_1]…
```

Each blob is `ChaCha20-Poly1305(ct)` of length `size + 16` (poly1305 tag). Nonces live in the header as hex, indexed parallel to `manifest.files` — so boot can seek without a per-file length table.

### Manifest root

```python
joined = "".join(f"{f['path']}:{f['sha256']}" for f in files_meta).encode()
root = sha256(joined)
```
A single Merkle-ish digest over path+hash pairs. Cheap to compare, catches reordering.

***

## `boot.py` — unpacking (Python reference path)

```
read_vault() → verify(header) → fetch DEK → check sha256(DEK)==key_hash
  → [v4] resolve passphrase → derive KEK
  → per file: decrypt → verify sha256 if fm.verify
  → write to /dev/shm/sable-vault (fallback: tempfile.mkdtemp)
  → [--run] spawn python -B server.py with SABLE_BOOT_MOUNT + PYTHONDONTWRITEBYTECODE
```

**Passphrase order (v4):** `--passphrase` → `$SABLE_PASSPHRASE` → **OS keyring** → TTY prompt → cache to keyring unless `--no-cache`.

> [!NOTE] The wrong-passphrase trap
> A bad passphrase doesn't fail at KEK derivation — Argon2 always returns 32 bytes. It fails on the **first file's Poly1305 tag check**, which is why the code special-cases `version == 4 and i == 0` to print `FATAL: wrong passphrase (or tampered vault)` instead of a generic decrypt error.

**Mount selection:** `/dev/shm/sable-vault` if `/dev/shm` exists (RAM-backed), else a temp dir. The `rmtree` at start guarantees no stale plaintext from a previous crashed run.

***

## `starter/main.go` — Blob A (the real boot path)

795 lines of Go. Same crypto, but with two properties `boot.py` can't have:

### 1. Pinned trust root

```go
//go build -ldflags "-X main.trustedSignerHex=<64 hex>"
var trustedSignerHex string
```

`resolveTrustedSigner()` resolves `-ldflags` → `$SABLE_TRUSTED_SIGNER` → **refuses to boot**.

`verifyHeader()` then does the critical check:

```go
if !bytes.Equal(signer, trusted) {
    return …, fmt.Errorf("vault signer %s does not match trusted key %s — refusing to boot")
}
if !ed25519.Verify(trusted, body, sig) { … }
```

> [!CRITICAL] Why this matters
> Verifying against the header's **own** `signer` field is worthless — an attacker signs their own vault with their own key and it validates. The pinned key makes the check meaningful. This is the single most important line in the subsystem.

### 2. Master key rotation on every boot

```
decrypt-under-A → POST /v1/rotate → get (newDEK, newHash)
                → re-encrypt all plaintexts under B → overwrite vault.sable
                → print "[key] rotated: aaaa… → bbbb…"
```

`rotateForBoot()` hits `POST {bridge}/v1/rotate` with `{vault_hash}`. `reseal()` re-encrypts with **fresh nonces** and preserves the KEK (passphrase + salt don't change). The bridge keeps a key history so a crash mid-rotation is still recoverable.

Disable with `--rotate=false`.

### Canonical JSON — the cross-language trap

```go
func canonicalJSON(v any) ([]byte, error)   // json.Marshal + sorted keys + {",", ":"}
```

Go must reproduce **byte-exact** what Python's `json.dumps(v, sort_keys=True, separators=(",", ":"))` produces, or the Ed25519 signature computed in Python won't verify in Go. This is the most fragile seam in the whole system.

### Process management

`main()` spawns `python -B server.py` with `SABLE_BOOT_MOUNT`, `PORT`, `PYTHONDONTWRITEBYTECODE=1`, then:

| Signal | Action |
|:--|:--|
| SIGINT / SIGTERM | `terminateChild` (SIGTERM to **process group**), wait 5 s |
| still alive | `killChild` (SIGKILL to group) |
| child exits | `os.RemoveAll(mount)` — **tmpfs wiped on exit** |

`proc_linux.go` uses `Setpgid: true` **+ `Pdeathsig: SIGTERM`** — the kernel kills the child if the starter dies, even by SIGKILL. `proc_unix_other.go` drops `Pdeathsig` (not available on Darwin/BSD). `proc_windows.go` uses `CREATE_NEW_PROCESS_GROUP` and falls back to `Process.Kill()`.

### Flag defaults (note the port!)

```go
portFlag := flag.String("port", envOr("SABLE_PORT", "61772"), …)
```

> [!WARNING] The starter defaults to **61772**, not 61770
> `start` passes `SABLE_PORT` through, so in practice they agree — but running `sable-starter` bare puts the server on a different port than everything else expects. Worth knowing before you debug "why is nothing on 61770".

***

## `encrypt_tree.py` + `vimport.py` — the import-hook path

A **second, lighter** packaging strategy (labelled "C2" in the docstring) for people who don't want a full vault:

```
vault-encrypt core_vault/ --dek-hex <64 hex>
  path/foo.py → path/foo.py.enc   (nonce(12) || ChaCha20-Poly1305)
  plaintext deleted unless --keep
```

`vimport.install(Path("vault/"), dek)` inserts a `VaultFinder` at `sys.meta_path[0]`. On `import engine`, it finds `vault/engine.py.enc`, decrypts **in RAM**, `compile()`s with filename `<vault:engine>`, `exec()`s into the module dict, then `del src`.

> [!CAUTION] Documented limitations
> - **Top-level modules only** — `if "." in fullname: return None`. No packages.
> - Must run with `-B` / `PYTHONDONTWRITEBYTECODE=1` or `__pycache__` leaks plaintext bytecode.
> - `del src` is best-effort GC, not a real memory wipe.

***

## Threat model summary

| Attack | Defence |
|:--|:--|
| Edit vault files | Per-file ChaCha20-Poly1305 AEAD + manifest SHA-256 |
| Re-sign with own key | **Pinned** Ed25519 pubkey in the compiled starter |
| Downgrade v4 → v3 | `key_hash`/`salt`/`kdf` are inside the v4 signature |
| Swap file contents | Path is in the HKDF `info`, so keys are per-path |
| Steal the vault file | Needs bridge DEK (+ passphrase for v4) |
| Read plaintext off disk | tmpfs only, wiped on exit, `-B` prevents `__pycache__` |
| Replay an old vault | Master rotates per boot, bridge keeps key history |
| Brute-force the passphrase | Argon2id 256 MiB × 3 iters |

> [!NOTE] Not defended against
> A compromised bridge (`:8765`) hands out DEKs to anyone with a valid bearer token. Root on the machine at boot time can read tmpfs. The design protects the **vault at rest**, not a live rooted host.

***

## Current state on this machine

- `vault.sable` = **33 MB**, present at repo root.
- **No starter binary is committed** (`sable-tools/starter/sable-starter-bin` exists but the launcher path resolution falls through) → `sable-launch.sh` drops to `server.py`, i.e. the **plaintext dev tree**.
- `sable-tools/starter/sable-starter.exe` (Windows) is committed; the Linux binary may need `go build`.
- The bridge service (`127.0.0.1:8765`) is **not** part of this repo — it's external infrastructure.
