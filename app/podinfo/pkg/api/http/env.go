package http

import (
	"net/http"
	"strings"

	"os"
)

// releaseSettingsPrefix marks a release's fault settings, which come from its release Secret.
// They are internal to the deployment and never shown by /env.
const releaseSettingsPrefix = "PODINFO_FAULT_"

// Env godoc
// @Summary Environment
// @Description returns the environment variables as a JSON array
// @Tags HTTP API
// @Accept json
// @Produce json
// @Router /env [get]
// @Success 200 {object} http.ArrayResponse
func (s *Server) envHandler(w http.ResponseWriter, r *http.Request) {
	_, span := s.tracer.Start(r.Context(), "envHandler")
	defer span.End()
	s.JSONResponse(w, r, visibleEnv(os.Environ()))
}

// visibleEnv drops the release settings from a KEY=value list.
func visibleEnv(env []string) []string {
	out := make([]string, 0, len(env))
	for _, kv := range env {
		if !strings.HasPrefix(kv, releaseSettingsPrefix) {
			out = append(out, kv)
		}
	}
	return out
}
