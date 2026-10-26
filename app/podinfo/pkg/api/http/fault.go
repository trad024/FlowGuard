package http

// FlowGuard addition (decision D19, amended 2026-10-26 and 2026-10-27): graded fault
// injection. Off by default. Registered after the Prometheus middleware, at the position of
// --random-error, so http_request_duration_seconds records the injected status and latency.
// Probe and metrics paths are never touched: a faulty pod must still become ready.
//
// The settings come from flags at start (a "bad version" is a pod template change) and, when
// --fault-admin is set, can be changed at run time through /fault on the internal metrics
// port (an incident in a running version, with no rollout). The run-time settings can also
// leak memory at a fixed rate, until the container's memory limit stops it.

import (
	"encoding/json"
	"math/rand/v2"
	"net/http"
	"sync"
	"sync/atomic"
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
// with probability ErrorRate. So the overall 5xx share is Fraction * ErrorRate. LeakKBPerSec
// keeps that much memory allocated every second while it is above zero.
type FaultConfig struct {
	ErrorRate    float64 `json:"error_rate"`
	LatencyMs    int     `json:"latency_ms"`
	JitterMs     int     `json:"jitter_ms"`
	Fraction     float64 `json:"fraction"`
	LeakKBPerSec int     `json:"leak_kb_per_s"`
	Hostname     string  `json:"-"`
}

// Enabled reports whether the request path changes anything.
func (c FaultConfig) Enabled() bool {
	return c.Fraction > 0 && (c.ErrorRate > 0 || c.LatencyMs > 0 || c.JitterMs > 0)
}

func (c FaultConfig) clamped() FaultConfig {
	c.ErrorRate = clamp01(c.ErrorRate)
	c.Fraction = clamp01(c.Fraction)
	c.LatencyMs = max(c.LatencyMs, 0)
	c.JitterMs = max(c.JitterMs, 0)
	c.LeakKBPerSec = max(c.LeakKBPerSec, 0)
	return c
}

type faultMiddleware struct {
	cfg   atomic.Pointer[FaultConfig]
	sleep func(time.Duration)
	draw  func() float64 // uniform in [0, 1)

	leakMu   sync.Mutex
	leaked   [][]byte
	leakStop chan struct{}
}

// NewFaultMiddleware returns the middleware; values outside their range are clamped.
func NewFaultMiddleware(cfg FaultConfig) *faultMiddleware {
	m := &faultMiddleware{sleep: time.Sleep, draw: rand.Float64}
	m.Set(cfg)
	return m
}

// Config returns the settings in force.
func (m *faultMiddleware) Config() FaultConfig {
	return *m.cfg.Load()
}

// Set replaces the settings at once for every following request and restarts the leak.
func (m *faultMiddleware) Set(cfg FaultConfig) {
	cfg = cfg.clamped()
	if old := m.cfg.Load(); old != nil && cfg.Hostname == "" {
		cfg.Hostname = old.Hostname
	}
	m.cfg.Store(&cfg)
	m.restartLeak(cfg.LeakKBPerSec)
}

func clamp01(v float64) float64 {
	return min(max(v, 0), 1)
}

func (m *faultMiddleware) restartLeak(kbPerSec int) {
	m.leakMu.Lock()
	defer m.leakMu.Unlock()
	if m.leakStop != nil {
		close(m.leakStop)
		m.leakStop = nil
	}
	if kbPerSec <= 0 {
		m.leaked = nil // released: the garbage collector takes it back
		return
	}
	stop := make(chan struct{})
	m.leakStop = stop
	go func() {
		tick := time.NewTicker(time.Second)
		defer tick.Stop()
		for {
			select {
			case <-stop:
				return
			case <-tick.C:
				m.leakOnce(kbPerSec)
			}
		}
	}()
}

// leakOnce keeps kb more kilobytes reachable, touching every page so it counts as resident.
func (m *faultMiddleware) leakOnce(kb int) {
	block := make([]byte, kb*1024)
	for i := 0; i < len(block); i += 4096 {
		block[i] = 1
	}
	m.leakMu.Lock()
	m.leaked = append(m.leaked, block)
	m.leakMu.Unlock()
}

// LeakedBytes is the memory currently held by the leak.
func (m *faultMiddleware) LeakedBytes() int {
	m.leakMu.Lock()
	defer m.leakMu.Unlock()
	n := 0
	for _, b := range m.leaked {
		n += len(b)
	}
	return n
}

func (m *faultMiddleware) Handler(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cfg := m.cfg.Load()
		if faultExemptPaths[r.URL.Path] || !cfg.Enabled() || m.draw() >= cfg.Fraction {
			next.ServeHTTP(w, r)
			return
		}
		delay := time.Duration(cfg.LatencyMs) * time.Millisecond
		if cfg.JitterMs > 0 {
			delay += time.Duration(m.draw() * float64(cfg.JitterMs) * float64(time.Millisecond))
		}
		if delay > 0 {
			m.sleep(delay)
		}
		if cfg.ErrorRate > 0 && m.draw() < cfg.ErrorRate {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusInternalServerError)
			// The body names the pod, so per-request logs can attribute the error
			_ = json.NewEncoder(w).Encode(map[string]string{
				"error":    "internal error",
				"hostname": cfg.Hostname,
			})
			return
		}
		next.ServeHTTP(w, r)
	})
}

// AdminHandler serves /fault on the metrics port: GET shows the settings, PUT or POST
// replaces them with a JSON body, DELETE clears them.
func (m *faultMiddleware) AdminHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
		case http.MethodPut, http.MethodPost:
			var cfg FaultConfig
			if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&cfg); err != nil {
				http.Error(w, "invalid JSON body", http.StatusBadRequest)
				return
			}
			m.Set(cfg)
		case http.MethodDelete:
			m.Set(FaultConfig{})
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		cfg := m.Config()
		_ = json.NewEncoder(w).Encode(map[string]any{"settings": cfg, "leaked_bytes": m.LeakedBytes()})
	})
}
