package http

// FlowGuard addition (decision D19, amended 2026-10-26): graded fault injection for "bad
// versions". Off by default. Registered after the Prometheus middleware, at the position of
// --random-error, so http_request_duration_seconds records the injected status and latency.
// Probe and metrics paths are never touched: a faulty canary must still become ready.

import (
	"encoding/json"
	"math/rand/v2"
	"net/http"
	"time"
)

// faultExemptPaths are never delayed or failed: the kubelet probes and Prometheus' scrape path.
var faultExemptPaths = map[string]bool{
	"/healthz":        true,
	"/readyz":         true,
	"/readyz/enable":  true,
	"/readyz/disable": true,
	"/metrics":        true,
}

// FaultConfig is the injected behaviour. A request is faulty with probability Fraction; a
// faulty request is delayed by LatencyMs plus a uniform 0..JitterMs, then fails with a 500
// with probability ErrorRate. So the overall 5xx share is Fraction * ErrorRate.
type FaultConfig struct {
	ErrorRate float64
	LatencyMs int
	JitterMs  int
	Fraction  float64
	Hostname  string
}

// Enabled reports whether the middleware changes anything.
func (c FaultConfig) Enabled() bool {
	return c.Fraction > 0 && (c.ErrorRate > 0 || c.LatencyMs > 0 || c.JitterMs > 0)
}

type faultMiddleware struct {
	cfg   FaultConfig
	sleep func(time.Duration)
	draw  func() float64 // uniform in [0, 1)
}

// NewFaultMiddleware returns the middleware; values outside their range are clamped.
func NewFaultMiddleware(cfg FaultConfig) *faultMiddleware {
	cfg.ErrorRate = clamp01(cfg.ErrorRate)
	cfg.Fraction = clamp01(cfg.Fraction)
	cfg.LatencyMs = max(cfg.LatencyMs, 0)
	cfg.JitterMs = max(cfg.JitterMs, 0)
	return &faultMiddleware{cfg: cfg, sleep: time.Sleep, draw: rand.Float64}
}

func clamp01(v float64) float64 {
	return min(max(v, 0), 1)
}

func (m *faultMiddleware) Handler(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if faultExemptPaths[r.URL.Path] || m.draw() >= m.cfg.Fraction {
			next.ServeHTTP(w, r)
			return
		}
		delay := time.Duration(m.cfg.LatencyMs) * time.Millisecond
		if m.cfg.JitterMs > 0 {
			delay += time.Duration(m.draw() * float64(m.cfg.JitterMs) * float64(time.Millisecond))
		}
		if delay > 0 {
			m.sleep(delay)
		}
		if m.cfg.ErrorRate > 0 && m.draw() < m.cfg.ErrorRate {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusInternalServerError)
			// The body names the pod, so per-request logs can attribute injected errors
			_ = json.NewEncoder(w).Encode(map[string]string{
				"error":    "injected fault",
				"hostname": m.cfg.Hostname,
			})
			return
		}
		next.ServeHTTP(w, r)
	})
}
