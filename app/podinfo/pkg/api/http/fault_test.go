package http

// Tests for the FlowGuard fault middleware (D19, amended).

import (
	"bufio"
	"encoding/json"
	"math"
	"math/rand/v2"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.uber.org/zap"
)

var okHandler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
})

// seeded makes the draws reproducible and records requested sleeps instead of sleeping.
func seeded(cfg FaultConfig) (*faultMiddleware, *[]time.Duration) {
	m := NewFaultMiddleware(cfg)
	rng := rand.New(rand.NewPCG(1, 2))
	m.draw = rng.Float64
	slept := &[]time.Duration{}
	m.sleep = func(d time.Duration) { *slept = append(*slept, d) }
	return m, slept
}

func serve(h http.Handler, path string) *httptest.ResponseRecorder {
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", path, nil))
	return rr
}

func errorShare(t *testing.T, h http.Handler, n int) float64 {
	t.Helper()
	failed := 0
	for i := 0; i < n; i++ {
		if serve(h, "/").Code == http.StatusInternalServerError {
			failed++
		}
	}
	return float64(failed) / float64(n)
}

func TestFaultErrorRateWithinTolerance(t *testing.T) {
	for _, rate := range []float64{0.02, 0.05, 0.3} {
		m, _ := seeded(FaultConfig{ErrorRate: rate, Fraction: 1})
		got := errorShare(t, m.Handler(okHandler), 20000)
		// 4 standard deviations of a binomial share over 20000 requests
		tol := 4 * math.Sqrt(rate*(1-rate)/20000)
		if math.Abs(got-rate) > tol {
			t.Errorf("rate %.2f: got %.4f, want within %.4f", rate, got, tol)
		}
	}
}

func TestFaultFractionLimitsTheAffectedRequests(t *testing.T) {
	m, slept := seeded(FaultConfig{ErrorRate: 1, LatencyMs: 10, Fraction: 0.25})
	got := errorShare(t, m.Handler(okHandler), 20000)
	if math.Abs(got-0.25) > 0.02 {
		t.Errorf("fraction 0.25 with error rate 1: got %.4f failed", got)
	}
	// Only the faulty requests were delayed
	if share := float64(len(*slept)) / 20000; math.Abs(share-0.25) > 0.02 {
		t.Errorf("delayed share %.4f, want about 0.25", share)
	}
}

func TestFaultLatencyIsAddedWithBoundedJitter(t *testing.T) {
	m, slept := seeded(FaultConfig{LatencyMs: 200, JitterMs: 50, Fraction: 1})
	h := m.Handler(okHandler)
	for i := 0; i < 500; i++ {
		if code := serve(h, "/").Code; code != http.StatusOK {
			t.Fatalf("latency-only fault returned %d", code)
		}
	}
	if len(*slept) != 500 {
		t.Fatalf("expected 500 delays, got %d", len(*slept))
	}
	lo, hi := 200*time.Millisecond, 250*time.Millisecond
	var sum time.Duration
	for _, d := range *slept {
		if d < lo || d >= hi {
			t.Fatalf("delay %v outside [%v, %v)", d, lo, hi)
		}
		sum += d
	}
	if mean := sum / 500; mean < 220*time.Millisecond || mean > 230*time.Millisecond {
		t.Errorf("mean delay %v, want about 225ms", mean)
	}
}

func TestFaultLatencyReallySleeps(t *testing.T) {
	h := NewFaultMiddleware(FaultConfig{LatencyMs: 30, Fraction: 1}).Handler(okHandler)
	start := time.Now()
	serve(h, "/")
	if took := time.Since(start); took < 30*time.Millisecond {
		t.Errorf("request took %v, want at least 30ms", took)
	}
}

func TestFaultNeverTouchesProbesOrMetrics(t *testing.T) {
	m, slept := seeded(FaultConfig{ErrorRate: 1, LatencyMs: 500, Fraction: 1})
	h := m.Handler(okHandler)
	for _, path := range []string{"/healthz", "/readyz", "/readyz/enable", "/readyz/disable", "/metrics"} {
		for i := 0; i < 50; i++ {
			if code := serve(h, path).Code; code != http.StatusOK {
				t.Fatalf("%s returned %d under full fault injection", path, code)
			}
		}
	}
	if len(*slept) != 0 {
		t.Errorf("probe or metrics requests were delayed %d times", len(*slept))
	}
	if code := serve(h, "/").Code; code != http.StatusInternalServerError {
		t.Errorf("user route returned %d, want 500", code)
	}
}

func TestInjectedErrorNamesThePod(t *testing.T) {
	m, _ := seeded(FaultConfig{ErrorRate: 1, Fraction: 1, Hostname: "podinfo-abc-1"})
	rr := serve(m.Handler(okHandler), "/")
	var body map[string]string
	if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %q", rr.Body.String())
	}
	if body["hostname"] != "podinfo-abc-1" || body["error"] != "injected fault" {
		t.Errorf("unexpected body %v", body)
	}
}

func TestFaultIsOffByDefaultAndValuesAreClamped(t *testing.T) {
	if (FaultConfig{}).Enabled() {
		t.Error("zero config must be disabled")
	}
	if (FaultConfig{ErrorRate: 0.5, Fraction: 0}).Enabled() {
		t.Error("fraction 0 must disable the fault")
	}
	m := NewFaultMiddleware(FaultConfig{ErrorRate: 7, Fraction: -1, LatencyMs: -5})
	if m.cfg.ErrorRate != 1 || m.cfg.Fraction != 0 || m.cfg.LatencyMs != 0 {
		t.Errorf("not clamped: %+v", m.cfg)
	}
}

// The end-to-end requirement: through podinfo's real handlers and middleware chain, a 5%
// error rate shows up as about 5% 5xx on path="root" in the metrics podinfo exports.
func TestInjectedErrorsAppearInPodinfoMetrics(t *testing.T) {
	srv := NewMockServer()
	srv.logger = zap.NewNop()
	// /healthz needs the flag ListenAndServe sets; restore it for the upstream health tests
	defer atomic.StoreInt32(&healthy, atomic.SwapInt32(&healthy, 1))
	srv.config.FaultErrorRate = 0.05
	srv.config.FaultFraction = 1
	srv.registerHandlers()
	srv.registerMiddlewares()

	const n = 20000
	for i := 0; i < n; i++ {
		req := httptest.NewRequest("GET", "/", nil)
		req.Header.Set("User-Agent", "loadgen")
		srv.router.ServeHTTP(httptest.NewRecorder(), req)
	}
	// Probes keep passing while the fault is active
	if code := serve(srv.router, "/healthz").Code; code != http.StatusOK {
		t.Fatalf("/healthz returned %d", code)
	}

	metrics := serve(srv.router, "/metrics").Body.String()
	counts := map[string]float64{}
	scanner := bufio.NewScanner(strings.NewReader(metrics))
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "http_request_duration_seconds_count{") ||
			!strings.Contains(line, `path="root"`) {
			continue
		}
		status := strings.SplitN(strings.SplitN(line, `status="`, 2)[1], `"`, 2)[0]
		value, _ := strconv.ParseFloat(line[strings.LastIndex(line, " ")+1:], 64)
		counts[status] += value
	}
	total := counts["200"] + counts["500"]
	if total != n {
		t.Fatalf("metrics count %v requests on path=root, want %d (counts %v)", total, n, counts)
	}
	share := counts["500"] / total
	if math.Abs(share-0.05) > 4*math.Sqrt(0.05*0.95/n) {
		t.Errorf("5xx share on path=root is %.4f, want about 0.05", share)
	}
	t.Logf("path=root: %v requests, %.2f%% 5xx", total, 100*share)
}
