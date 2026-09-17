//go:build ignore

// ast_lit.go — AST string-literal harvester for the beacon (idempotent).
//
// Usage: go run ast_lit.go <hex-key> <dir>
//
// Behaviour:
//  1. Scans main-package *.go files, skipping struct tags and already-
//     wrapped stealth.D(...) literals.
//  2. Reads any existing stealth/literals_generated.go, decrypts its
//     constants using the OLD XOR key (embedded in its header comment).
//  3. Merges old constants with newly-harvested literals.
//  4. Re-encrypts the union with the NEW key.
//  5. Rewrites source files to wrap fresh literals with stealth.D(...).
//
// Re-running on an already-wrapped tree is a NO-OP on the constant set:
// no new literals are found, but nothing is lost either. This makes the
// whole build pipeline idempotent.
package main

import (
	"bufio"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

const keyHeaderPrefix = "// aes-key-hex: "

// aesSeal encrypts plain with AES-256-GCM, returning nonce || ciphertext || tag.
func aesSeal(key, plain []byte) []byte {
	block, err := aes.NewCipher(key)
	if err != nil {
		panic(err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		panic(err)
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		panic(err)
	}
	return gcm.Seal(nonce, nonce, plain, nil)
}

func aesOpen(key, blob []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	ns := gcm.NonceSize()
	if len(blob) < ns {
		return nil, fmt.Errorf("blob too short")
	}
	return gcm.Open(nil, blob[:ns], blob[ns:], nil)
}

func constName(plain string) string {
	h := sha256.Sum256([]byte(plain))
	return "EncL_" + hex.EncodeToString(h[:])[:10]
}

// loadExisting reads the existing literals_generated.go (if any), extracts
// the old XOR key from the header comment, and returns a map of
// constName -> plaintext for every constant that was previously emitted.
func loadExisting(path string) (map[string]string, error) {
	out := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		return out, err // missing file is fine
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1024*1024), 1024*1024)

	var oldKey []byte
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, keyHeaderPrefix) {
			kh := strings.TrimSpace(strings.TrimPrefix(line, keyHeaderPrefix))
			if b, err := hex.DecodeString(kh); err == nil {
				oldKey = b
			}
		}
	}
	if oldKey == nil {
		return out, nil // no key found, nothing to recover
	}

	// Second pass: parse each `EncL_xxx = []byte{0x.., 0x..}` line.
	if _, err := f.Seek(0, 0); err != nil {
		return out, err
	}
	scanner = bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1024*1024), 1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if !strings.HasPrefix(line, "EncL_") {
			continue
		}
		eq := strings.Index(line, "= []byte{")
		if eq < 0 {
			continue
		}
		name := strings.TrimSpace(line[:eq])
		body := line[eq+len("= []byte{"):]
		body = strings.TrimSuffix(strings.TrimSpace(body), "}")
		if body == "" {
			continue
		}
		var ct []byte
		for _, tok := range strings.Split(body, ",") {
			tok = strings.TrimSpace(tok)
			if tok == "" {
				continue
			}
			v, err := strconv.ParseUint(strings.TrimPrefix(tok, "0x"), 16, 8)
			if err != nil {
				ct = nil
				break
			}
			ct = append(ct, byte(v))
		}
		if ct == nil {
			continue
		}
		// Decrypt: AES-256-GCM with the key derived from the header hex.
		sum := sha256.Sum256(oldKey)
		pt, err := aesOpen(sum[:], ct)
		if err != nil {
			continue
		}
		out[name] = string(pt)
	}
	return out, nil
}

func main() {
	if len(os.Args) < 3 {
		fmt.Fprintln(os.Stderr, "usage: ast_lit <hex-key> <dir>")
		os.Exit(1)
	}
	rawKey, err := hex.DecodeString(os.Args[1])
	if err != nil || len(rawKey) < 32 {
		panic("ast_lit: need >= 32-byte hex key")
	}
	sum := sha256.Sum256(rawKey)
	key := sum[:]
	dir := os.Args[2]
	litPath := filepath.Join(dir, "stealth", "literals_generated.go")

	// Step 1: Load existing constants (decrypted with old key).
	existing, _ := loadExisting(litPath)

	fset := token.NewFileSet()
	files, _ := filepath.Glob(filepath.Join(dir, "*.go"))

	type repl struct {
		off, end int
		name     string
		file     string
	}
	var all []repl

	// Start with previously-seen constants (preserve them).
	seen := map[string]string{} // plain -> const name
	for name, plain := range existing {
		seen[plain] = name
	}
	initialCount := len(seen)

	for _, path := range files {
		src, _ := os.ReadFile(path)
		if strings.Contains(string(src), "//go:build ignore") {
			continue
		}
		f, err := parser.ParseFile(fset, path, src, parser.ParseComments)
		if err != nil {
			fmt.Fprintln(os.Stderr, "parse:", path, err)
			continue
		}

		// collect literals already inside stealth.D(...) / D(...)
		wrapped := map[ast.Node]bool{}
		ast.Inspect(f, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			name := ""
			switch fn := call.Fun.(type) {
			case *ast.SelectorExpr:
				if id, ok := fn.X.(*ast.Ident); ok {
					name = id.Name + "." + fn.Sel.Name
				}
			case *ast.Ident:
				name = fn.Name
			}
			if name == "stealth.D" || name == "D" {
				for _, a := range call.Args {
					wrapped[a] = true
				}
			}
			return true
		})

		// struct tags to skip
		tags := map[*ast.BasicLit]bool{}
		ast.Inspect(f, func(n ast.Node) bool {
			if fs, ok := n.(*ast.Field); ok && fs.Tag != nil {
				tags[fs.Tag] = true
			}
			return true
		})

		// import paths + const-decl literals must stay constant
		locked := map[*ast.BasicLit]bool{}
		ast.Inspect(f, func(n ast.Node) bool {
			switch x := n.(type) {
			case *ast.ImportSpec:
				if x.Path != nil {
					locked[x.Path] = true
				}
			case *ast.GenDecl:
				if x.Tok == token.CONST {
					for _, s := range x.Specs {
						if vs, ok := s.(*ast.ValueSpec); ok {
							for _, v := range vs.Values {
								ast.Inspect(v, func(m ast.Node) bool {
									if bl, ok := m.(*ast.BasicLit); ok {
										locked[bl] = true
									}
									return true
								})
							}
						}
					}
				}
			}
			return true
		})

		ast.Inspect(f, func(n ast.Node) bool {
			lit, ok := n.(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING {
				return true
			}
			if wrapped[lit] || tags[lit] || locked[lit] {
				return true
			}
			plain, err := strconv.Unquote(lit.Value)
			if err != nil {
				return true
			}
			if plain == "" {
				return true
			}
			name := constName(plain)
			if prev, ok := seen[plain]; ok {
				name = prev
			} else {
				seen[plain] = name
			}
			p := fset.Position(lit.Pos())
			e := fset.Position(lit.End())
			all = append(all, repl{off: p.Offset, end: e.Offset, name: name, file: path})
			return true
		})
	}

	// Step 2: Emit merged constants file with the new key.
	var b strings.Builder
	b.WriteString("// Code generated by ast_lit.go — DO NOT EDIT\n")
	fmt.Fprintf(&b, "%s%s\n\n", keyHeaderPrefix, os.Args[1])
	b.WriteString("package stealth\n\nvar (\n")
	var plains []string
	for p := range seen {
		plains = append(plains, p)
	}
	sort.Strings(plains)
	for _, p := range plains {
		fmt.Fprintf(&b, "\t%s = []byte{%s}\n", seen[p], hexList(aesSeal(key, []byte(p))))
	}
	b.WriteString(")\n")
	if err := os.WriteFile(litPath, []byte(b.String()), 0644); err != nil {
		fmt.Fprintln(os.Stderr, "write:", err)
		os.Exit(1)
	}
	fmt.Printf("constants: %d total (%d preserved, %d new)\n", len(seen), initialCount, len(seen)-initialCount)

	// Step 3: Rewrite sources to wrap freshly-found literals.
	byFile := map[string][]repl{}
	for _, r := range all {
		byFile[r.file] = append(byFile[r.file], r)
	}
	for path, rs := range byFile {
		src, _ := os.ReadFile(path)
		sort.Slice(rs, func(i, j int) bool { return rs[i].off > rs[j].off })
		out := src
		for _, r := range rs {
			rep := "stealth.D(stealth." + r.name + ")"
			out = append(out[:r.off], append([]byte(rep), out[r.end:]...)...)
		}
		os.WriteFile(path, out, 0644)
		fmt.Printf("  rewrote %s (%d literals)\n", filepath.Base(path), len(rs))
	}
}

func hexList(b []byte) string {
	var sb strings.Builder
	for i, x := range b {
		if i > 0 {
			sb.WriteString(", ")
		}
		fmt.Fprintf(&sb, "0x%02x", x)
	}
	return sb.String()
}
