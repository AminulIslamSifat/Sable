package stealth

import (
	"crypto/rand"
	"encoding/base64"
	"math/big"
	"time"
)

// ── Network Jitter ─────────────────────────────────────────
// Breaks fixed-interval heartbeat patterns that IDS/IPS systems flag.
// Instead of exactly 30s, we drift ±5-15s randomly each cycle.

// JitteredDuration returns a base duration with random jitter applied.
// minJitter and maxJitter are in seconds. Jitter can be positive or negative.
func JitteredDuration(base time.Duration, minJitterSec, maxJitterSec int) time.Duration {
	if minJitterSec >= maxJitterSec {
		return base
	}

	rangeSize := int64(maxJitterSec - minJitterSec + 1)
	n, err := rand.Int(rand.Reader, big.NewInt(rangeSize))
	if err != nil {
		return base // fail safe: no jitter rather than panic
	}

	jitterSec := int(n.Int64()) + minJitterSec

	// Randomly flip sign so jitter goes both ways
	sign, _ := rand.Int(rand.Reader, big.NewInt(2))
	if sign.Int64() == 0 {
		jitterSec = -jitterSec
	}

	result := base + time.Duration(jitterSec)*time.Second
	if result < 5*time.Second {
		return 5 * time.Second // floor: never beacon faster than 5s
	}
	return result
}

// HeartbeatInterval returns the next sleep duration for the heartbeat loop.
// Base: 30s, Jitter: ±5-15s → actual range: 15s to 45s
func HeartbeatInterval() time.Duration {
	return JitteredDuration(30*time.Second, 5, 15)
}

// ReconnectBackoff returns an exponential backoff with jitter to avoid
// thundering herd detection when bridge comes back online.
func ReconnectBackoff(attempt int) time.Duration {
	base := time.Second
	for i := 0; i < attempt && i < 6; i++ {
		base *= 2
	}
	if base > 60*time.Second {
		base = 60 * time.Second
	}
	// Add 0-10s jitter to backoff
	return JitteredDuration(base, 0, 10)
}

// ── Payload Size Padding ───────────────────────────────────
// Defeats traffic analysis that flags fixed-size periodic packets.

// RandInt returns a cryptographically random int in [0, max).
func RandInt(max int) int {
	if max <= 0 {
		return 0
	}
	n, err := rand.Int(rand.Reader, big.NewInt(int64(max)))
	if err != nil {
		return 0
	}
	return int(n.Int64())
}

// RandomB64 generates n bytes of crypto-random data and returns
// them as a base64 string. Used to pad heartbeat payloads so
// packet sizes vary wildly between beacons.
func RandomB64(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return base64.StdEncoding.EncodeToString(b)
}
