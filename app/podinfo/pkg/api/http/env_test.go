package http

import (
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"
)

func TestEnvHandler(t *testing.T) {
	req, err := http.NewRequest("GET", "/api/env", nil)
	if err != nil {
		t.Fatal(err)
	}

	rr := httptest.NewRecorder()
	srv := NewMockServer()
	handler := http.HandlerFunc(srv.infoHandler)

	handler.ServeHTTP(rr, req)

	// Check the status code is what we expect.
	if status := rr.Code; status != http.StatusOK {
		t.Errorf("handler returned wrong status code: got %v want %v",
			status, http.StatusOK)
	}

	// Check the response body is what we expect.
	expected := ".*hostname.*"
	r := regexp.MustCompile(expected)
	if !r.MatchString(rr.Body.String()) {
		t.Fatalf("handler returned unexpected body:\ngot \n%v \nwant \n%s",
			rr.Body.String(), expected)
	}
}

func TestEnvHandlerHidesReleaseSettings(t *testing.T) {
	t.Setenv("PODINFO_FAULT_ERROR_RATE", "0.05")
	t.Setenv("PODINFO_FAULT_LATENCY_MS", "200")
	t.Setenv("PODINFO_UI_COLOR", "#34577c")

	req, err := http.NewRequest("GET", "/env", nil)
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	srv := NewMockServer()
	http.HandlerFunc(srv.envHandler).ServeHTTP(rr, req)

	if status := rr.Code; status != http.StatusOK {
		t.Fatalf("handler returned wrong status code: got %v want %v", status, http.StatusOK)
	}
	body := rr.Body.String()
	if strings.Contains(body, "PODINFO_FAULT_") || strings.Contains(body, "0.05") {
		t.Fatalf("release settings visible in /env:\n%s", body)
	}
	if !strings.Contains(body, "PODINFO_UI_COLOR") {
		t.Fatalf("other variables must stay visible:\n%s", body)
	}
}
