#!/usr/bin/env node
"use strict"
// conformance.js — Node side. Proves crypto.js and sable_crypto.py agree.

const ncrypto = require("node:crypto")
const { execFileSync } = require("node:child_process")
const fs = require("node:fs")
const path = require("node:path")
const os = require("node:os")

const crypto = require("./crypto.js")

const HERE = __dirname
const PY = path.join(HERE, "..", ".venv", "bin", "python")
const CONFORMANCE_PY = path.join(HERE, "conformance.py")

const MASTER = Buffer.from("a".repeat(64), "hex")
const FILE_PATH = "engine/foo.py"
const KEK = Buffer.from("b".repeat(64), "hex")
const NONCE = Buffer.from("00".repeat(12), "hex")
const PLAIN = Buffer.from("hello sable\n", "utf8")

function py(args) {
  return execFileSync(PY, [CONFORMANCE_PY, ...args], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  })
}

function compareDirs(a, b) {
  const diffs = []
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      const rel = path.relative(a, full)
      if (entry.isDirectory()) walk(full)
      else if (entry.isFile()) {
        const other = path.join(b, rel)
        if (!fs.existsSync(other)) diffs.push(`missing: ${rel}`)
        else if (!fs.readFileSync(full).equals(fs.readFileSync(other))) diffs.push(`differs: ${rel}`)
      }
    }
  }
  walk(a)
  return diffs
}

function makeTree(root) {
  fs.mkdirSync(path.join(root, "engine"), { recursive: true })
  fs.mkdirSync(path.join(root, "Brain"), { recursive: true })
  fs.mkdirSync(path.join(root, "nested"), { recursive: true })
  fs.writeFileSync(path.join(root, "engine", "foo.py"), 'print("hello")\n')
  fs.writeFileSync(path.join(root, "Brain", "state.json"), '{"x": 1}\n')
  fs.writeFileSync(path.join(root, "README.md"), "# test\n")
  fs.writeFileSync(path.join(root, "nested", "deep.txt"), "deep\n")
}

async function main() {
  let failures = 0
  const report = (label, ok, note = "") => {
    console.log(`${ok ? "OK  " : "FAIL"} ${label}${note ? "  " + note : ""}`)
    if (!ok) failures++
  }

  // ── Part A: primitives ──────────────────────────────────────
  console.log("=== PRIMITIVES ===")
  const p = JSON.parse(py(["primitives"]))
  const node = {
    file_key_v3: crypto.deriveFileKey(MASTER, FILE_PATH, null).toString("hex"),
    file_key_v4: crypto.deriveFileKey(MASTER, FILE_PATH, KEK).toString("hex"),
    ct: crypto.encryptBlob(crypto.deriveFileKey(MASTER, FILE_PATH, null), PLAIN, NONCE).blob.toString("hex"),
    canonical: crypto.canonicalJSON({ b: 1, a: [1, 2, { z: true, y: null }], c: "x" }),
  }
  for (const k of Object.keys(p)) {
    const ok = p[k] === node[k]
    report(k, ok, ok ? "" : `py=${String(p[k]).slice(0, 24)}… node=${String(node[k]).slice(0, 24)}…`)
  }

  // ── Part B: cross-compat round trips ────────────────────────
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "sable-conf-"))
  const tree = path.join(tmp, "tree")
  makeTree(tree)

  const DEK = ncrypto.randomBytes(32)
  const DEK_HEX = DEK.toString("hex")
  const SIGN_KEY = path.join(tmp, "test.ed25519")

  console.log("\n=== py seal -> node boot (v3) ===")
  const pyVault = path.join(tmp, "py-vault.sable")
  py(["seal", tree, pyVault, DEK_HEX, "", SIGN_KEY])
  const nodeMount = path.join(tmp, "node-mount")
  const b1 = await crypto.boot({ vaultPath: pyVault, dek: DEK, mount: nodeMount })
  const d1 = compareDirs(tree, nodeMount)
  report(`files match (v${b1.version}, ${b1.files} files)`, d1.length === 0, d1.join(", "))

  console.log("\n=== node seal -> py boot (v3) ===")
  const nodeVault = path.join(tmp, "node-vault.sable")
  await crypto.seal({ root: tree, out: nodeVault, dek: DEK, signKeyPath: SIGN_KEY })
  const pyMount = path.join(tmp, "py-mount")
  py(["boot", nodeVault, pyMount, DEK_HEX, ""])
  const d2 = compareDirs(tree, pyMount)
  report("files match", d2.length === 0, d2.join(", "))

  console.log("\n=== py seal (v4) -> node boot ===")
  const pyVault4 = path.join(tmp, "py-vault4.sable")
  py(["seal", tree, pyVault4, DEK_HEX, "test-pass", SIGN_KEY])
  const nodeMount4 = path.join(tmp, "node-mount4")
  const b3 = await crypto.boot({ vaultPath: pyVault4, dek: DEK, passphrase: "test-pass", mount: nodeMount4 })
  const d3 = compareDirs(tree, nodeMount4)
  report(`files match (v${b3.version}, ${b3.files} files)`, d3.length === 0, d3.join(", "))

  console.log("\n=== node seal (v4) -> py boot ===")
  const nodeVault4 = path.join(tmp, "node-vault4.sable")
  await crypto.seal({ root: tree, out: nodeVault4, dek: DEK, passphrase: "test-pass", signKeyPath: SIGN_KEY })
  const pyMount4 = path.join(tmp, "py-mount4")
  py(["boot", nodeVault4, pyMount4, DEK_HEX, "test-pass"])
  const d4 = compareDirs(tree, pyMount4)
  report("files match", d4.length === 0, d4.join(", "))

  console.log("\n=== wrong passphrase must fail ===")
  let rejected = false
  try {
    await crypto.boot({ vaultPath: nodeVault4, dek: DEK, passphrase: "wrong", mount: path.join(tmp, "bad") })
  } catch (e) {
    rejected = /wrong passphrase|failed/i.test(e.message)
  }
  report("wrong passphrase rejected", rejected)

  console.log(`\n=== RESULT ===\n${failures === 0 ? "ALL PASS" : failures + " FAILURE(S)"}`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((e) => {
  console.error("FATAL:", e.message)
  if (e.stderr) console.error(e.stderr.toString())
  process.exit(1)
})
