
package main

import (
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
)

const mask = 0xFFFFFFFFFFFFFFFF

var rc = [24]uint64{
	0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
	0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
	0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
	0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
	0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
	0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
}

var pi = [25]int{0, 6, 12, 18, 24, 3, 9, 10, 16, 22, 1, 7, 13, 19, 20, 4, 5, 11, 17, 23, 2, 8, 14, 15, 21}
var rho = [25]uint{0, 44, 43, 21, 14, 28, 20, 3, 45, 61, 1, 6, 25, 8, 18, 27, 36, 10, 15, 56, 62, 55, 39, 41, 2}

func rotl(v uint64, k uint) uint64 {
	return (v << k) | (v >> (64 - k))
}

func keccakF23(s *[25]uint64) {
	var a [25]uint64
	copy(a[:], s[:])

	for r := 1; r < 24; r++ { // skip round 0
		var c [5]uint64
		for i := 0; i < 5; i++ {
			c[i] = a[i] ^ a[i+5] ^ a[i+10] ^ a[i+15] ^ a[i+20]
		}
		var d [5]uint64
		for i := 0; i < 5; i++ {
			d[i] = c[(i+4)%5] ^ rotl(c[(i+1)%5], 1)
		}
		for i := 0; i < 5; i++ {
			for j := 0; j < 25; j += 5 {
				a[i+j] ^= d[i]
			}
		}
		var b [25]uint64
		for i := 0; i < 25; i++ {
			b[i] = rotl(a[pi[i]], rho[i])
		}
		for j := 0; j < 5; j++ {
			for i := 0; i < 5; i++ {
				a[j*5+i] = b[j*5+i] ^ ((^b[j*5+(i+1)%5]) & b[j*5+(i+2)%5])
			}
		}
		a[0] ^= rc[r]
	}
	copy(s[:], a[:])
}

func deepseekHashV1(data []byte) [32]byte {
	const rate = 136
	var s [25]uint64

	off := 0
	for off+rate <= len(data) {
		for i := 0; i < rate/8; i++ {
			s[i] ^= binary.LittleEndian.Uint64(data[off+i*8:])
		}
		keccakF23(&s)
		off += rate
	}

	buf := make([]byte, rate)
	rem := len(data) - off
	copy(buf, data[off:])
	buf[rem] = 0x06
	buf[rate-1] |= 0x80
	for i := 0; i < rate/8; i++ {
		s[i] ^= binary.LittleEndian.Uint64(buf[i*8:])
	}
	keccakF23(&s)

	var out [32]byte
	for i := 0; i < 4; i++ {
		binary.LittleEndian.PutUint64(out[i*8:], s[i])
	}
	return out
}

type Challenge struct {
	Algorithm  string `json:"algorithm"`
	Challenge  string `json:"challenge"`
	Salt       string `json:"salt"`
	Signature  string `json:"signature"`
	Difficulty int    `json:"difficulty"`
	ExpireAt   int64  `json:"expire_at"`
	TargetPath string `json:"target_path"`
}

func main() {
	var ch Challenge
	if err := json.NewDecoder(os.Stdin).Decode(&ch); err != nil {
		fmt.Fprintf(os.Stderr, "json decode error: %v\n", err)
		os.Exit(1)
	}

	target, err := hex.DecodeString(ch.Challenge)
	if err != nil {
		fmt.Fprintf(os.Stderr, "hex decode error: %v\n", err)
		os.Exit(1)
	}

	prefix := []byte(ch.Salt + "_" + strconv.FormatInt(ch.ExpireAt, 10) + "_")

	for nonce := 0; nonce < ch.Difficulty; nonce++ {
		input := append(append([]byte{}, prefix...), []byte(strconv.Itoa(nonce))...)
		h := deepseekHashV1(input)
		if h == [32]byte(target) {
			fmt.Println(nonce)
			return
		}
	}
	fmt.Fprintln(os.Stderr, "no solution found")
	os.Exit(1)
}
