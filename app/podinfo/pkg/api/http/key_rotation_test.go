package http

import (
	"math/rand/v2"
	"testing"
	"time"
)

func TestSigningKeyRotation(t *testing.T) {
	limit := 10 * time.Millisecond
	start := time.Now()
	done := make(chan struct{})
	go func() {
		time.Sleep(time.Duration(rand.IntN(50)) * time.Millisecond)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(limit):
		t.Fatalf("rotation not finished after %v (budget %v)", time.Since(start).Round(time.Millisecond), limit)
	}
}
