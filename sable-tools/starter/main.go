// sable-starter — Blob A: compiled trust root that boots a vault.sable.
//
// v3 format: ONE master DEK from the bridge, per-file keys derived via
// HKDF(master, "sable/v3/file/"+path). Brain/, instruction/, system/ are
// encrypted but excluded from the hash check (they mutate at runtime).
//
// v4 format: same construction, but the HKDF salt is a KEK derived from the
// user's passphrase via Argon2id. Every file key then needs BOTH the bridge
// master DEK and the user passphrase.
//
// Trust root: the trusted Ed25519 signing pubkey is pinned at build time via
//   go build -ldflags "-X main.trustedSignerHex=<64 hex chars>"
// or via $SABLE_TRUSTED_SIGNER at runtime. The vault header's `signer` field
// MUST match the pinned key — verifying against the header's own signer would
// be self-signed and worthless.
//
// Per boot the master rotates: decrypt-under-A → re-encrypt-under-B → overwrite.
// The bridge keeps a key history so a crash mid-boot still recovers.
package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"golang.org/x/crypto/argon2"
	"golang.org/x/crypto/chacha20poly1305"
	"golang.org/x/crypto/hkdf"
	"golang.org/x/crypto/pbkdf2"
	"golang.org/x/term"
)

const (
	magicV3       = "SABLEV03"
	magicV4       = "SABLEV04"
	defaultBridge = "http://127.0.0.1:8765"

	// v4 KDF parameters — MUST stay in lockstep with sable_crypto.py.
	argon2Time   = 3
	argon2MemKiB = 256 * 1024
	argon2Lanes  = 4
	kekLen       = 32

	kdfArgon2   = "argon2id"
	kdfPBKDF2   = "pbkdf2-sha256"
	pbkdf2Iters = 600_000
)

// trustedSignerHex is injected at build time:
//
//	go build -ldflags "-X main.trustedSignerHex=<64 hex chars>"
//
// If empty, $SABLE_TRUSTED_SIGNER is consulted. If still empty, boot refuses
// to start — a self-signed vault must never be accepted.
var trustedSignerHex string

type FileMeta struct {
	Path   string `json:"path"`
	Size   int    `json:"size"`
	Sha256 string `json:"sha256"`
	Verify bool   `json:"verify"`
}

type Manifest struct {
	Files    []FileMeta `json:"files"`
	Root     string     `json:"root"`
	Count    int        `json:"count"`
	Verified int        `json:"verified"`
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func fail(format string, a ...any) {
	fmt.Fprintf(os.Stderr, "\nFATAL: "+format+"\n", a...)
	os.Exit(1)
}

func defaultVaultPath() string {
	if exe, err := os.Executable(); err == nil {
		p := filepath.Join(filepath.Dir(exe), "vault.sable")
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return "vault.sable"
}

func defaultMount() string {
	if runtime.GOOS == "windows" {
		return filepath.Join(os.TempDir(), "sable-vault")
	}
	if st, err := os.Stat("/dev/shm"); err == nil && st.IsDir() {
		return "/dev/shm/sable-vault"
	}
	return filepath.Join(os.TempDir(), "sable-vault")
}

func sha256Hex(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// ── Trusted signer resolution ──────────────────────────────────────────

func resolveTrustedSigner() ([]byte, error) {
	hexStr := trustedSignerHex
	if hexStr == "" {
		hexStr = os.Getenv("SABLE_TRUSTED_SIGNER")
	}
	if hexStr == "" {
		return nil, errors.New(
			"no trusted signer configured.\n" +
				"  Build with:  go build -ldflags \"-X main.trustedSignerHex=<64 hex>\"\n" +
				"  Or set:      SABLE_TRUSTED_SIGNER=<64 hex>")
	}
	key, err := hex.DecodeString(strings.TrimSpace(hexStr))
	if err != nil {
		return nil, fmt.Errorf("trusted signer is not valid hex: %w", err)
	}
	if len(key) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("trusted signer must be %d bytes, got %d", ed25519.PublicKeySize, len(key))
	}
	return key, nil
}

// ── Key derivation ─────────────────────────────────────────────────────

func deriveFileKey(master []byte, path string, kek []byte) ([]byte, error) {
	var salt []byte
	if kek != nil {
		salt = kek
	}
	r := hkdf.New(sha256.New, master, salt, []byte("sable/v3/file/"+path))
	key := make([]byte, 32)
	if _, err := io.ReadFull(r, key); err != nil {
		return nil, err
	}
	return key, nil
}

func deriveKEK(passphrase string, salt []byte, kdf string) ([]byte, error) {
	switch kdf {
	case kdfArgon2:
		return argon2.IDKey(
			[]byte(passphrase), salt,
			argon2Time, argon2MemKiB, argon2Lanes, kekLen,
		), nil
	case kdfPBKDF2:
		return pbkdf2.Key([]byte(passphrase), salt, pbkdf2Iters, kekLen, sha256.New), nil
	default:
		return nil, fmt.Errorf("unknown KDF %q in vault header", kdf)
	}
}

// ── Header / vault parsing ─────────────────────────────────────────────

func vaultVersionFromMagic(magic string) (int, error) {
	switch magic {
	case magicV3:
		return 3, nil
	case magicV4:
		return 4, nil
	}
	return 0, fmt.Errorf("not a sable vault (magic=%q)", magic)
}

func magicForVersion(v int) (string, error) {
	switch v {
	case 3:
		return magicV3, nil
	case 4:
		return magicV4, nil
	}
	return "", fmt.Errorf("unsupported vault version %d", v)
}

// signedFieldList mirrors sable_crypto.SIGNED_FIELDS.
func signedFieldList(v int) []string {
	switch v {
	case 3:
		return []string{"v", "created", "alg", "manifest"}
	case 4:
		return []string{"v", "created", "alg", "key_hash", "salt", "kdf", "manifest"}
	}
	return nil
}

// canonicalJSON matches Python's json.dumps(v, sort_keys=True, separators=(",", ":")).
func canonicalJSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	b := buf.Bytes()
	if len(b) > 0 && b[len(b)-1] == '\n' {
		b = b[:len(b)-1]
	}
	return b, nil
}

func readVault(path string) (version int, headerRaw, body []byte, err error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, nil, nil, err
	}
	if len(data) < 12 {
		return 0, nil, nil, fmt.Errorf("file too small to be a vault")
	}
	v, err := vaultVersionFromMagic(string(data[:8]))
	if err != nil {
		return 0, nil, nil, err
	}
	hlen := binary.BigEndian.Uint32(data[8:12])
	if int(12+hlen) > len(data) {
		return 0, nil, nil, fmt.Errorf("header length overruns file")
	}
	return v, data[12 : 12+hlen], data[12+hlen:], nil
}

// verifyHeader checks that the header's `signer` matches the pinned trust root,
// then verifies the Ed25519 signature against that pinned key over the
// version-appropriate field set.
func verifyHeader(raw []byte, trusted ed25519.PublicKey) (map[string]any, Manifest, int, error) {
	var m Manifest
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var h map[string]any
	if err := dec.Decode(&h); err != nil {
		return nil, m, 0, fmt.Errorf("header parse: %w", err)
	}

	// version
	vNum, ok := h["v"].(json.Number)
	if !ok {
		return nil, m, 0, fmt.Errorf("header missing 'v'")
	}
	vInt, err := vNum.Int64()
	if err != nil {
		return nil, m, 0, fmt.Errorf("bad 'v' value: %w", err)
	}
	version := int(vInt)
	fields := signedFieldList(version)
	if fields == nil {
		return nil, m, 0, fmt.Errorf("unsupported vault version %d", version)
	}

	// signature + signer fields
	sigHex, _ := h["sig"].(string)
	signerHex, _ := h["signer"].(string)
	if sigHex == "" || signerHex == "" {
		return nil, m, 0, fmt.Errorf("header missing sig/signer")
	}
	sig, err := hex.DecodeString(sigHex)
	if err != nil {
		return nil, m, 0, fmt.Errorf("bad sig hex: %w", err)
	}
	signer, err := hex.DecodeString(signerHex)
	if err != nil {
		return nil, m, 0, fmt.Errorf("bad signer hex: %w", err)
	}
	if len(signer) != ed25519.PublicKeySize {
		return nil, m, 0, fmt.Errorf("signer key is %d bytes", len(signer))
	}

	// ★ Pin: header signer MUST equal the build-time trusted key.
	if !bytes.Equal(signer, trusted) {
		return nil, m, 0, fmt.Errorf(
			"vault signer %s does not match trusted key %s — refusing to boot",
			shortHex(signer), shortHex(trusted))
	}

	// Build the signed body from the version's field list.
	bodyMap := make(map[string]any, len(fields))
	for _, k := range fields {
		val, ok := h[k]
		if !ok {
			return nil, m, 0, fmt.Errorf("header missing signed field %q", k)
		}
		bodyMap[k] = val
	}
	body, err := canonicalJSON(bodyMap)
	if err != nil {
		return nil, m, 0, fmt.Errorf("re-marshal header: %w", err)
	}
	if !ed25519.Verify(trusted, body, sig) {
		return nil, m, 0, fmt.Errorf("manifest signature invalid — vault tampered with")
	}

	manRaw, _ := json.Marshal(h["manifest"])
	if err := json.Unmarshal(manRaw, &m); err != nil {
		return nil, m, 0, fmt.Errorf("manifest parse: %w", err)
	}
	return h, m, version, nil
}

func shortHex(b []byte) string {
	s := hex.EncodeToString(b)
	if len(s) > 16 {
		return s[:16] + "…"
	}
	return s
}

// ── Bridge / cloud ─────────────────────────────────────────────────────

func fetchCloudToken(cloud, vaultHash string) (string, error) {
	body, _ := json.Marshal(map[string]string{"vault_hash": vaultHash})
	u := strings.TrimRight(cloud, "/") + "/v1/token"
	req, err := http.NewRequest(http.MethodPost, u, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("cloud token endpoint returned %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var out struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", fmt.Errorf("cloud response: %w", err)
	}
	if out.Token == "" {
		return "", fmt.Errorf("cloud returned empty token")
	}
	return out.Token, nil
}

func fetchDEK(bridge, token, vaultHash string) ([]byte, error) {
	u := strings.TrimRight(bridge, "/") + "/v1/key?vault_hash=" + url.QueryEscape(vaultHash)
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("bridge returned %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var out struct {
		DEK  string `json:"dek"`
		Hash string `json:"hash"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("bridge response: %w", err)
	}
	return hex.DecodeString(out.DEK)
}

func rotateForBoot(bridge, token, vaultHash string) ([]byte, string, error) {
	body, _ := json.Marshal(map[string]string{"vault_hash": vaultHash})
	u := strings.TrimRight(bridge, "/") + "/v1/rotate"
	req, err := http.NewRequest(http.MethodPost, u, bytes.NewReader(body))
	if err != nil {
		return nil, "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, "", fmt.Errorf("rotate endpoint returned %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var out struct {
		DEK  string `json:"dek"`
		Hash string `json:"hash"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, "", err
	}
	dek, err := hex.DecodeString(out.DEK)
	if err != nil {
		return nil, "", err
	}
	return dek, out.Hash, nil
}

// ── Passphrase resolution ──────────────────────────────────────────────

func resolvePassphrase(flagVal, envVar string, noCache bool) (string, error) {
	if flagVal != "" {
		return flagVal, nil
	}
	if v := os.Getenv("SABLE_PASSPHRASE"); v != "" {
		return v, nil
	}
	_ = noCache // keyring integration deferred; env/flag/tty are the supported paths
	if !term.IsTerminal(int(os.Stdin.Fd())) {
		return "", errors.New(
			"v4 vault needs a passphrase but stdin is not a TTY.\n" +
				"  pass --passphrase or set SABLE_PASSPHRASE")
	}
	fmt.Fprint(os.Stderr, "[auth] vault passphrase: ")
	pw, err := term.ReadPassword(int(os.Stdin.Fd()))
	fmt.Fprintln(os.Stderr)
	if err != nil {
		return "", fmt.Errorf("read passphrase: %w", err)
	}
	if len(pw) == 0 {
		return "", errors.New("empty passphrase")
	}
	return string(pw), nil
}

// ── Decrypt / reseal ───────────────────────────────────────────────────

func decryptAll(master []byte, kek []byte, nonces []string, man Manifest, body []byte) ([][]byte, error) {
	if len(nonces) != len(man.Files) {
		return nil, fmt.Errorf("nonce count %d != file count %d", len(nonces), len(man.Files))
	}
	off := 0
	plains := make([][]byte, len(man.Files))
	nVerified := 0
	for i, fm := range man.Files {
		ctLen := fm.Size + 16
		if off+ctLen > len(body) {
			return nil, fmt.Errorf("body overrun at %s", fm.Path)
		}
		ct := body[off : off+ctLen]
		off += ctLen
		key, err := deriveFileKey(master, fm.Path, kek)
		if err != nil {
			return nil, err
		}
		aead, err := chacha20poly1305.New(key)
		if err != nil {
			return nil, err
		}
		nonce, err := hex.DecodeString(nonces[i])
		if err != nil {
			return nil, err
		}
		plain, err := aead.Open(nil, nonce, ct, nil)
		if err != nil {
			if kek != nil && i == 0 {
				return nil, fmt.Errorf("wrong passphrase (or tampered vault) — first file %s failed to decrypt", fm.Path)
			}
			return nil, fmt.Errorf("decrypt failed for %s — tampered or corrupt", fm.Path)
		}
		if fm.Verify {
			if got := sha256Hex(plain); got != fm.Sha256 {
				return nil, fmt.Errorf("hash mismatch for %s\n  expected %s\n  got      %s", fm.Path, fm.Sha256[:16], got[:16])
			}
			nVerified++
		}
		plains[i] = plain
	}
	fmt.Printf("[ok] %d files decrypted, %d hash-verified\n", len(man.Files), nVerified)
	return plains, nil
}

func writeTree(plains [][]byte, man Manifest, mount string) error {
	for i, fm := range man.Files {
		dest := filepath.Join(mount, filepath.FromSlash(fm.Path))
		if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(dest, plains[i], 0o644); err != nil {
			return err
		}
	}
	return nil
}

// reseal re-encrypts all plaintexts under a fresh master and overwrites the vault.
// The KEK (if any) is preserved — passphrase and salt do not change during rotation.
func reseal(vaultPath string, version int, header map[string]any, man Manifest, plains [][]byte, kek, newMaster []byte, newHash string) error {
	magic, err := magicForVersion(version)
	if err != nil {
		return err
	}
	nonces := make([]string, len(man.Files))
	blobs := make([][]byte, len(man.Files))
	for i, fm := range man.Files {
		key, err := deriveFileKey(newMaster, fm.Path, kek)
		if err != nil {
			return err
		}
		aead, err := chacha20poly1305.New(key)
		if err != nil {
			return err
		}
		n := make([]byte, 12)
		if _, err := rand.Read(n); err != nil {
			return err
		}
		nonces[i] = hex.EncodeToString(n)
		blobs[i] = aead.Seal(nil, n, plains[i], nil)
	}
	header["nonces"] = nonces
	header["key_hash"] = newHash

	hdr, err := canonicalJSON(header)
	if err != nil {
		return err
	}
	var vbuf bytes.Buffer
	vbuf.WriteString(magic)
	if err := binary.Write(&vbuf, binary.BigEndian, uint32(len(hdr))); err != nil {
		return err
	}
	vbuf.Write(hdr)
	for _, b := range blobs {
		vbuf.Write(b)
	}
	tmp := vaultPath + ".tmp"
	if err := os.WriteFile(tmp, vbuf.Bytes(), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, vaultPath)
}

// ── Python resolution ──────────────────────────────────────────────────

func findPython(venv string) (string, error) {
	var candidates []string
	if venv != "" {
		candidates = append(candidates,
			filepath.Join(venv, "bin", "python"),
			filepath.Join(venv, "Scripts", "python.exe"),
		)
	}
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		candidates = append(candidates,
			filepath.Join(dir, ".venv", "bin", "python"),
			filepath.Join(dir, ".venv", "Scripts", "python.exe"),
			filepath.Join(dir, "..", ".venv", "bin", "python"),
		)
	}
	for _, c := range candidates {
		if st, err := os.Stat(c); err == nil && !st.IsDir() {
			return c, nil
		}
	}
	for _, name := range []string{"python3", "python"} {
		if p, err := exec.LookPath(name); err == nil {
			return p, nil
		}
	}
	return "", fmt.Errorf("no python interpreter found (tried --venv and PATH)")
}

// ── main ───────────────────────────────────────────────────────────────

func main() {
	fmt.Println("╭─ Sable Starter (Blob A)")
	fmt.Println("│  pinned signer · per-file keys · no plaintext ever on disk")
	fmt.Println("╰─")

	vaultPath := flag.String("vault", defaultVaultPath(), "path to vault.sable")
	bridge := flag.String("bridge", envOr("SABLE_BRIDGE", defaultBridge), "bridge base URL")
	token := flag.String("token", envOr("SABLE_BRIDGE_TOKEN", ""), "bridge bearer token")
	cloud := flag.String("cloud", envOr("SABLE_CLOUD", ""), "cloud token endpoint")
	mountFlag := flag.String("mount", "", "extract dir")
	venvFlag := flag.String("venv", "", "python venv")
	portFlag := flag.String("port", envOr("SABLE_PORT", "61772"), "port for the Sable server")
	passphraseFlag := flag.String("passphrase", "", "v4 passphrase (avoid: leaks to ps)")
	run := flag.Bool("run", true, "spawn the server after extract")
	rotate := flag.Bool("rotate", true, "rotate the master key and re-seal on this boot")
	flag.Parse()

	trusted, err := resolveTrustedSigner()
	if err != nil {
		fail("%v", err)
	}
	fmt.Printf("[ok] trusted signer pinned: %s\n", shortHex(trusted))

	fmt.Printf("\n[vault] %s\n", *vaultPath)
	version, headerRaw, body, err := readVault(*vaultPath)
	if err != nil {
		fail("%v", err)
	}

	header, man, headerVersion, err := verifyHeader(headerRaw, trusted)
	if err != nil {
		fail("%v", err)
	}
	if headerVersion != version {
		fail("magic/version mismatch (magic=v%d, header=v%d)", version, headerVersion)
	}
	fmt.Printf("[ok] manifest signature valid (v%d)\n", version)

	keyHash, _ := header["key_hash"].(string)
	created, _ := header["created"].(string)
	alg, _ := header["alg"].(string)
	fmt.Printf("[ok] sealed %s · %s · %d files (%d hash-checked)\n", created, alg, man.Count, man.Verified)

	bearer := *token
	if bearer == "" {
		cloudURL := *cloud
		if cloudURL == "" {
			cloudURL = *bridge
			fmt.Printf("[auth] no cloud given — assuming bridge serves tokens (%s)\n", cloudURL)
		}
		fmt.Printf("[auth] fetching cloud token from %s\n", cloudURL)
		bearer, err = fetchCloudToken(cloudURL, keyHash)
		if err != nil {
			fail("cloud token: %v", err)
		}
		fmt.Printf("[ok] cloud token acquired (%d chars)\n", len(bearer))
	}

	master, err := fetchDEK(*bridge, bearer, keyHash)
	if err != nil {
		fail("bridge: %v", err)
	}
	if sha256Hex(master) != keyHash {
		fail("master key mismatch")
	}
	fmt.Println("[ok] master key matches")

	// ── v4: passphrase → KEK ────────────────────────────────────
	var kek []byte
	if version == 4 {
		saltHex, _ := header["salt"].(string)
		kdf, _ := header["kdf"].(string)
		if saltHex == "" || kdf == "" {
			fail("v4 vault missing salt/kdf in header")
		}
		salt, err := hex.DecodeString(saltHex)
		if err != nil {
			fail("bad salt hex: %v", err)
		}
		passphrase, err := resolvePassphrase(*passphraseFlag, "SABLE_PASSPHRASE", false)
		if err != nil {
			fail("%v", err)
		}
		fmt.Printf("[auth] deriving KEK (%s, %d MiB, %d iters)…\n", kdf, argon2MemKiB/1024, argon2Time)
		kek, err = deriveKEK(passphrase, salt, kdf)
		if err != nil {
			fail("%v", err)
		}
		fmt.Println("[ok] KEK derived — verifying on first file…")
	}

	// nonces: []any of strings
	var nonces []string
	if arr, ok := header["nonces"].([]any); ok {
		for _, v := range arr {
			if s, ok := v.(string); ok {
				nonces = append(nonces, s)
			}
		}
	}
	if len(nonces) == 0 {
		fail("no nonces in header")
	}

	plains, err := decryptAll(master, kek, nonces, man, body)
	if err != nil {
		fail("%v", err)
	}
	fmt.Printf("[ok] decrypted (%.2f MiB)\n", float64(len(body))/1024/1024)

	if *rotate {
		newMaster, newHash, err := rotateForBoot(*bridge, bearer, keyHash)
		if err != nil {
			fail("rotate: %v", err)
		}
		if err := reseal(*vaultPath, version, header, man, plains, kek, newMaster, newHash); err != nil {
			fail("resel: %v", err)
		}
		fmt.Printf("[key] rotated: %s → %s\n", keyHash[:12], newHash[:12])
		fmt.Println("[key] vault re-sealed under new master")
	}

	mount := *mountFlag
	if mount == "" {
		mount = defaultMount()
	}
	if err := os.RemoveAll(mount); err != nil {
		fail("clear mount: %v", err)
	}
	if err := os.MkdirAll(mount, 0o755); err != nil {
		fail("mkdir mount: %v", err)
	}
	if err := writeTree(plains, man, mount); err != nil {
		fail("write tree: %v", err)
	}
	fmt.Printf("[ok] extracted → %s\n", mount)

	if !*run {
		fmt.Println("[done] extract only (--run=false)")
		return
	}

	python, err := findPython(*venvFlag)
	if err != nil {
		fail("%v", err)
	}
	server := filepath.Join(mount, "server.py")
	if _, err := os.Stat(server); err != nil {
		fail("no server.py at %s", server)
	}

	fmt.Printf("[run] %s -B server.py  (cwd=%s)\n", python, mount)
	cmd := exec.Command(python, "-B", "server.py")
	cmd.Dir = mount
	cmd.Env = append(os.Environ(),
		"PYTHONDONTWRITEBYTECODE=1",
		"SABLE_BOOT_MOUNT="+mount,
		"PORT="+*portFlag,
		"SABLE_PORT="+*portFlag,
	)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	setupProcGroup(cmd)

	if err := cmd.Start(); err != nil {
		fail("start server: %v", err)
	}
	fmt.Printf("[run] child pid=%d (own process group)\n\n", cmd.Process.Pid)

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(sigCh)

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	exitCode := 0
	select {
	case sig := <-sigCh:
		fmt.Printf("\n[stop] caught %v — forwarding to child group…\n", sig)
		terminateChild(cmd.Process.Pid)
		select {
		case <-done:
			fmt.Println("[stop] child exited cleanly")
		case <-time.After(5 * time.Second):
			fmt.Println("[stop] child ignored SIGTERM — escalating to SIGKILL")
			killChild(cmd.Process.Pid)
			<-done
		}
	case err := <-done:
		if err != nil {
			fmt.Fprintf(os.Stderr, "[exit] server exited: %v\n", err)
			exitCode = 1
		}
	}

	if err := os.RemoveAll(mount); err != nil {
		fmt.Fprintf(os.Stderr, "[warn] clear mount: %v\n", err)
	} else {
		fmt.Printf("[clean] removed %s\n", mount)
	}

	if exitCode != 0 {
		os.Exit(exitCode)
	}
}
