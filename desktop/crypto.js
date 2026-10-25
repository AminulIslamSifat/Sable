"use strict"
// crypto.js — Node port of sable-tools/sable_crypto.py + seal/boot.
// Byte-compatible with the Python side. If these disagree, nothing decrypts.

const crypto = require("node:crypto")
const fs = require("node:fs")
const path = require("node:path")
const os = require("node:os")
const { execFileSync } = require("node:child_process")

// ── format ──────────────────────────────────────────────────────
const MAGIC_V3 = Buffer.from("SABLEV03", "latin1")
const MAGIC_V4 = Buffer.from("SABLEV04", "latin1")
const MAGIC_BY_VERSION = { 3: MAGIC_V3, 4: MAGIC_V4 }
const SIGNED_FIELDS = {
  3: ["v", "created", "alg", "manifest"],
  4: ["v", "created", "alg", "key_hash", "salt", "kdf", "manifest"],
}
const ALG_V3 = "chacha20poly1305-hkdf-perfile"
const ALG_V4 = "chacha20poly1305-hkdf-perfile-argon2"

// ── KDF params (must match sable_crypto.py exactly) ─────────────
const KDF_ARGON2 = "argon2id"
const KDF_PBKDF2 = "pbkdf2-sha256"
const ARGON2_TIME = 3
const ARGON2_MEM_KIB = 256 * 1024
const ARGON2_LANES = 4
const SALT_LEN = 16
const KEK_LEN = 32
const PBKDF2_ITERS = 600_000

// ── seal skip rules (mirror seal.py) ────────────────────────────
const SKIP_DIRS = new Set([
  ".venv", ".git", "__pycache__", ".pytest_cache", ".sable_backups",
  ".editor_tools_backups", "node_modules", ".mypy_cache", ".ruff_cache",
  ".idea", ".vscode", "fastembed_cache", "models",
  "sable-tools", "test", "tests", "output", "dist",
])
const SKIP_DIR_PREFIXES = ["browser-data-", "browser-scraper-data", "browser-automation-data"]
const SKIP_FILES = new Set([".sable_server.pid", "vault.sable", ".DS_Store"])
const NO_VERIFY_PREFIXES = ["Brain/", "instruction/", "system/"]

// ── helpers ─────────────────────────────────────────────────────
function sha256hex(data) {
  return crypto.createHash("sha256").update(data).digest("hex")
}

// Python json.dumps(obj, sort_keys=True, separators=(",", ":"))
function canonicalJSON(value) {
  if (Array.isArray(value)) return "[" + value.map(canonicalJSON).join(",") + "]"
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort()
    return "{" + keys.map((k) => JSON.stringify(k) + ":" + canonicalJSON(value[k])).join(",") + "}"
  }
  return JSON.stringify(value)
}

function needsVerify(relPath) {
  return !NO_VERIFY_PREFIXES.some((p) => relPath.startsWith(p))
}

// ── HKDF (sable/v3/file/<path>) ─────────────────────────────────
function deriveFileKey(master, filePath, kek = null) {
  const salt = kek || Buffer.alloc(0) // Python HKDF(salt=None) == zero salt
  const info = Buffer.from("sable/v3/file/" + filePath, "utf8")
  const out = crypto.hkdfSync("sha256", master, salt, info, 32)
  return Buffer.from(out)
}

// ── Argon2id / PBKDF2 KEK ───────────────────────────────────────
let _argon2 = null
async function _loadArgon2() {
  if (_argon2) return _argon2
  try {
    _argon2 = require("@node-rs/argon2")
    _argon2.__impl = "node-rs"
    return _argon2
  } catch {}
  try {
    _argon2 = require("hash-wasm")
    _argon2.__impl = "hash-wasm"
    return _argon2
  } catch {}
  throw new Error("no Argon2 backend: npm i @node-rs/argon2 (or hash-wasm)")
}

async function pickKdf() {
  try {
    await _loadArgon2()
    return KDF_ARGON2
  } catch {
    return KDF_PBKDF2
  }
}

async function deriveKek(passphrase, salt, kdf) {
  const pw = Buffer.from(passphrase, "utf8")
  if (kdf === KDF_ARGON2) {
    const a = await _loadArgon2()
    if (a.__impl === "node-rs") {
      const out = await a.hash(pw, {
        algorithm: a.Algorithm.Argon2id,
        memoryCost: ARGON2_MEM_KIB,
        timeCost: ARGON2_TIME,
        parallelism: ARGON2_LANES,
        outputLen: KEK_LEN,
        salt,
      })
      return Buffer.from(out)
    }
    const out = await a.argon2id({
      password: pw,
      salt,
      parallelism: ARGON2_LANES,
      iterations: ARGON2_TIME,
      memorySize: ARGON2_MEM_KIB,
      hashLength: KEK_LEN,
      outputType: "binary",
    })
    return Buffer.from(out)
  }
  if (kdf === KDF_PBKDF2) {
    return crypto.pbkdf2Sync(pw, salt, PBKDF2_ITERS, KEK_LEN, "sha256")
  }
  throw new Error(`unknown kdf: ${kdf}`)
}

// ── ChaCha20-Poly1305 (blob = ciphertext || tag, nonce separate) ─
function encryptBlob(key, plaintext, nonce) {
  nonce = nonce || crypto.randomBytes(12)
  const cipher = crypto.createCipheriv("chacha20-poly1305", key, nonce, { authTagLength: 16 })
  const ct = Buffer.concat([cipher.update(plaintext), cipher.final()])
  return { nonce, blob: Buffer.concat([ct, cipher.getAuthTag()]) }
}

function decryptBlob(key, nonce, blob) {
  const ct = blob.subarray(0, blob.length - 16)
  const tag = blob.subarray(blob.length - 16)
  const decipher = crypto.createDecipheriv("chacha20-poly1305", key, nonce, { authTagLength: 16 })
  decipher.setAuthTag(tag)
  return Buffer.concat([decipher.update(ct), decipher.final()])
}

// ── Ed25519 (PyNaCl raw 32-byte seed) ───────────────────────────
const PKCS8_ED25519_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex")

function loadSigner(keyPath) {
  if (!fs.existsSync(keyPath)) {
    const seed = crypto.randomBytes(32)
    fs.mkdirSync(path.dirname(keyPath), { recursive: true })
    fs.writeFileSync(keyPath, seed, { mode: 0o600 })
  }
  const seed = fs.readFileSync(keyPath)
  const der = Buffer.concat([PKCS8_ED25519_PREFIX, seed])
  return crypto.createPrivateKey({ key: der, format: "der", type: "pkcs8" })
}

function signerPublicHex(privateKey) {
  const der = crypto.createPublicKey(privateKey).export({ format: "der", type: "spki" })
  return der.subarray(der.length - 32).toString("hex")
}

function signCanonical(obj, privateKey) {
  return crypto.sign(null, Buffer.from(canonicalJSON(obj), "utf8"), privateKey)
}

function verifyHeader(header) {
  const fields = SIGNED_FIELDS[header.v]
  if (!fields) throw new Error(`unsupported vault version ${header.v}`)
  const body = {}
  for (const k of fields) {
    if (!(k in header)) throw new Error(`vault header missing signed field ${k}`)
    body[k] = header[k]
  }
  const pubDer = Buffer.concat([
    Buffer.from("302a300506032b6570032100", "hex"),
    Buffer.from(header.signer, "hex"),
  ])
  const pub = crypto.createPublicKey({ key: pubDer, format: "der", type: "spki" })
  const ok = crypto.verify(
    null,
    Buffer.from(canonicalJSON(body), "utf8"),
    pub,
    Buffer.from(header.sig, "hex"),
  )
  if (!ok) throw new Error("manifest signature invalid — vault tampered with")
  return true
}

// ── file collection (gitignore is authority) ────────────────────
function gitVisible(root) {
  try {
    const out = execFileSync(
      "git",
      ["-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
      { encoding: "buffer", timeout: 60000, stdio: ["ignore", "pipe", "ignore"] },
    )
    return new Set(out.toString("utf8").split("\0").filter(Boolean))
  } catch {
    return null
  }
}

function collect(root) {
  const visible = gitVisible(root)
  const rels = []
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      const rel = path.relative(root, full).split(path.sep).join("/")
      if (entry.isDirectory()) {
        if (SKIP_DIRS.has(entry.name)) continue
        if (SKIP_DIR_PREFIXES.some((p) => entry.name.startsWith(p))) continue
        walk(full)
      } else if (entry.isFile()) {
        if (SKIP_FILES.has(entry.name)) continue
        if (visible !== null && !visible.has(rel)) continue
        rels.push(rel)
      }
    }
  }
  walk(root)
  return rels.sort()
}

// ── bridge ──────────────────────────────────────────────────────
async function fetchDek(bridgeUrl, token, vaultHash) {
  const url = new URL("/v1/key", bridgeUrl)
  if (vaultHash) url.searchParams.set("vault_hash", vaultHash)
  const r = await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
  if (!r.ok) throw new Error(`bridge /v1/key ${r.status}`)
  const j = await r.json()
  return Buffer.from(j.dek, "hex")
}

// ── seal ────────────────────────────────────────────────────────
async function seal({ root, out, dek, passphrase = null, signKeyPath, bridgeUrl, token }) {
  root = path.resolve(root)
  if (!dek && bridgeUrl && token) dek = await fetchDek(bridgeUrl, token)
  if (!dek) throw new Error("seal: need a DEK (or bridgeUrl+token)")
  if (dek.length !== 32) throw new Error("DEK must be 32 bytes")

  const rels = collect(root)
  let salt = null, kdf = null, kek = null, version, alg
  if (passphrase) {
    salt = crypto.randomBytes(SALT_LEN)
    kdf = await pickKdf()
    kek = await deriveKek(passphrase, salt, kdf)
    version = 4
    alg = ALG_V4
  } else {
    version = 3
    alg = ALG_V3
  }

  const filesMeta = []
  const nonces = []
  const blobs = []
  for (const rel of rels) {
    const data = fs.readFileSync(path.join(root, rel))
    const verify = needsVerify(rel)
    const digest = verify ? sha256hex(data) : ""
    const key = deriveFileKey(dek, rel, kek)
    const { nonce, blob } = encryptBlob(key, data)
    filesMeta.push({ path: rel, size: data.length, sha256: digest, verify })
    nonces.push(nonce.toString("hex"))
    blobs.push(blob)
  }

  const joined = filesMeta.map((f) => `${f.path}:${f.sha256}`).join("")
  const manifest = {
    files: filesMeta,
    root: sha256hex(Buffer.from(joined, "utf8")),
    count: filesMeta.length,
    verified: filesMeta.filter((f) => f.verify).length,
  }

  const sk = loadSigner(signKeyPath || path.join(os.homedir(), ".config", "sable", "dev.ed25519"))
  const header = {
    v: version,
    created: new Date().toISOString(),
    alg,
    key_hash: sha256hex(dek),
    salt: salt ? salt.toString("hex") : null,
    kdf,
    nonces,
    manifest,
  }
  const signBody = {}
  for (const k of SIGNED_FIELDS[version]) signBody[k] = header[k]
  header.sig = signCanonical(signBody, sk).toString("hex")
  header.signer = signerPublicHex(sk)

  const headerJson = Buffer.from(canonicalJSON(header), "utf8")
  const lenBuf = Buffer.alloc(4)
  lenBuf.writeUInt32BE(headerJson.length, 0)
  const blob = Buffer.concat([MAGIC_BY_VERSION[version], lenBuf, headerJson, ...blobs])
  fs.mkdirSync(path.dirname(path.resolve(out)), { recursive: true })
  fs.writeFileSync(out, blob)
  return { out, version, files: filesMeta.length, verified: manifest.verified, bytes: blob.length }
}

// ── boot ────────────────────────────────────────────────────────
async function boot({ vaultPath, dek, passphrase = null, mount, bridgeUrl, token, verifySig = true }) {
  const data = fs.readFileSync(vaultPath)
  if (data.length < 12) throw new Error("file too small to be a vault")
  const magic = data.subarray(0, 8)
  const version = MAGIC_V4.equals(magic) ? 4 : MAGIC_V3.equals(magic) ? 3 : null
  if (!version) throw new Error(`not a sable vault (magic=${magic.toString("latin1")})`)
  const hlen = data.readUInt32BE(8)
  const header = JSON.parse(data.subarray(12, 12 + hlen).toString("utf8"))
  const body = data.subarray(12 + hlen)

  if (verifySig) verifyHeader(header)
  if (!dek && bridgeUrl && token) dek = await fetchDek(bridgeUrl, token, header.key_hash)
  if (!dek) throw new Error("boot: need a DEK (or bridgeUrl+token)")
  if (sha256hex(dek) !== header.key_hash) throw new Error("master key mismatch")

  let kek = null
  if (version === 4) {
    if (!passphrase) throw new Error("v4 vault needs a passphrase")
    const salt = Buffer.from(header.salt, "hex")
    kek = await deriveKek(passphrase, salt, header.kdf)
  }

  mount = mount || fs.mkdtempSync(path.join(os.tmpdir(), "sable-vault-"))
  fs.mkdirSync(mount, { recursive: true })

  let off = 0
  for (let i = 0; i < header.manifest.files.length; i++) {
    const fm = header.manifest.files[i]
    const ctLen = fm.size + 16
    const blob = body.subarray(off, off + ctLen)
    off += ctLen
    const key = deriveFileKey(dek, fm.path, kek)
    const nonce = Buffer.from(header.nonces[i], "hex")
    let plain
    try {
      plain = decryptBlob(key, nonce, blob)
    } catch {
      if (version === 4 && i === 0) throw new Error(`wrong passphrase (or tampered vault) — first file ${fm.path} failed`)
      throw new Error(`decrypt failed for ${fm.path}`)
    }
    if (fm.verify && sha256hex(plain) !== fm.sha256) {
      throw new Error(`hash mismatch for ${fm.path}`)
    }
    const dest = path.join(mount, fm.path)
    fs.mkdirSync(path.dirname(dest), { recursive: true })
    fs.writeFileSync(dest, plain)
  }
  return { mount, version, files: header.manifest.files.length }
}

module.exports = {
  // constants
  MAGIC_V3, MAGIC_V4, SIGNED_FIELDS, ALG_V3, ALG_V4, KDF_ARGON2, KDF_PBKDF2,
  ARGON2_TIME, ARGON2_MEM_KIB, ARGON2_LANES, SALT_LEN, KEK_LEN, PBKDF2_ITERS,
  // primitives
  sha256hex, canonicalJSON, deriveFileKey, deriveKek, pickKdf,
  encryptBlob, decryptBlob, verifyHeader, loadSigner, signerPublicHex,
  // io
  collect, fetchDek, seal, boot,
}
