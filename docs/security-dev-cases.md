# Expanded development security cases

Eight real app-ci cases: 0/8 decisions changed due to EPSS and 2/8 due to reachability. **EPSS effect not tested (masked by critical findings).** Every high-EPSS case also had a CRITICAL finding that blocks before scoring.

Each decision uses every real scan finding in the image. EPSS effects compare actual L1 with the same case at zero EPSS; reachability effects compare actual L1 with measured reachability replaced by unknown. Missing EPSS remains missing; hard rules remain active.

| Case / CI run | CVE | Module@version | CVSS | EPSS | Reachable | Fix available | L0 | L1 | EPSS changed L1 | Reachability changed L1 |
| --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- | --- |
| [dev-sec-text-called](https://github.com/trad024/FlowGuard/actions/runs/40927548134) | CVE-2026-56851 | `golang.org/x/text@v0.40.0` | 7.5 | 0.00466 | yes | yes | block | review | no | no |
| [dev-sec-text-uncalled](https://github.com/trad024/FlowGuard/actions/runs/40927335241) | CVE-2026-56851 | `golang.org/x/text@v0.40.0` | 7.5 | 0.00466 | no | yes | block | allow with findings | no | yes |
| [dev-sec-http2-022](https://github.com/trad024/FlowGuard/actions/runs/40927321818) | CVE-2023-45288 | `golang.org/x/net@v0.22.0` | 7.5 | 0.91969 | yes | yes | block | block | no | no |
| [dev-sec-http2-016](https://github.com/trad024/FlowGuard/actions/runs/40927540025) | CVE-2023-45288 | `golang.org/x/net@v0.16.0` | 7.5 | 0.91969 | yes | yes | block | block | no | no |
| [dev-sec-jwt4-called](https://github.com/trad024/FlowGuard/actions/runs/40927168160) | CVE-2025-30204 | `github.com/golang-jwt/jwt/v4@v4.5.1` | 7.5 | 0.00811 | yes | yes | block | review | no | no |
| [dev-sec-jwt5-uncalled](https://github.com/trad024/FlowGuard/actions/runs/40927311190) | CVE-2025-30204 | `github.com/golang-jwt/jwt/v5@v5.2.1` | 7.5 | 0.00811 | no | yes | block | allow with findings | no | yes |
| [dev-sec-legacy-jwt-320](https://github.com/trad024/FlowGuard/actions/runs/40927521742) | CVE-2025-30204 | `github.com/golang-jwt/jwt@v3.2.0+incompatible` | 7.5 | 0.00811 | yes | no | block | review | no | no |
| [dev-sec-legacy-jwt-322](https://github.com/trad024/FlowGuard/actions/runs/40927530355) | CVE-2025-30204 | `github.com/golang-jwt/jwt@v3.2.2+incompatible` | 7.5 | 0.00811 | yes | no | block | review | no | no |

Observed mix: reachable=6, unreachable=2, fixed=6, no_fix=2, low_epss=6, high_epss=2. Low EPSS means <=0.01; high means >=0.10, declared before the scenario pushes. All six required coverage minima must pass.

Allow with findings still requires a human through the existing gate. Unreachable means the complete pinned govulncheck source/symbol run produced only module/package findings for that CVE, not an absence of scan output. Reachable is govulncheck's conservative call-graph result, not proof that the service can be exploited. Legacy JWT's advisory has no symbol-specific metadata, so its reachable result is coarse.

The legacy JWT module has no known fixed version on that module path: Trivy reports Status=affected and omits FixedVersion; the Go advisory has no fixed event. The report records no known fix, while the policy preserves the absent field as unknown and conservatively applies the full no-fix weight. No missing field was filled in to change a decision. Migration to jwt/v4 or jwt/v5 is a different module path.

FIRST source: [2026-10-28](https://epss.empiricalsecurity.com/epss_scores-2026-10-28.csv.gz), 396,966 CVEs, model v2026.06.15; full source cached outside git in `state/epss/`.

- CSV SHA-256: `16ff6e9063ce2e2b31efbfa9bf98b574d501f84f22056861d31f3610665b5d06`
- Gzip SHA-256: `aa9eefe4155babd88985c26528087bba5053405d4d64aed85ea5dc5c2e16c4e9`

These eight cases contain only three distinct target CVEs, including paired versions of the same CVE. They test policy paths and are not eight independent vulnerability families or evidence of generalization. Both older x/net cases need a compatible gRPC pin; that pin has a real CRITICAL finding, so both cases block under the hard rule regardless of EPSS. Current fixed gRPC versions require HTTP/2 APIs absent from the vulnerable x/net versions. These two cases establish high-EPSS coverage but do not isolate an EPSS-only decision effect. **EPSS effect not tested (masked by critical findings).** The observed 0/8 decision changes do not identify the EPSS effect.

The initial Rapid Reset candidate CVE-2023-44487 is MEDIUM in its current upstream advisory and cannot be a HIGH target. The same x/net v0.16.0 case instead uses its real HIGH CVE-2023-45288, with EPSS 0.91969; the correction is recorded in the plan. No scanner severity, CVSS, reachability or result was invented or filtered to make an action change. All observed findings remain in the replay JSON.

The first local x/net build with gRPC 1.83.2 failed because Framer APIs were missing. It was rejected before pushing and is not counted as a security block. All eight reported scenario commits passed the real Linux build, unit tests, optional jobs, Trivy and pinned govulncheck in app-ci. Raw artifacts and local preparation logs remain outside git; committed sanitized observations include their SHA-256 values.

## Reproduction

From the repository root with the project environment and agent package installed:

```powershell
python ci/security/download_epss.py --date 2026-10-28 --cache state/epss --sha256 aa9eefe4155babd88985c26528087bba5053405d4d64aed85ea5dc5c2e16c4e9
python ci/security/report_development.py
```

The second command is offline: it validates the full snapshot, combines the 14 prior dev cases with the eight retained new cases, recomputes L0/L1 and counterfactuals, checks coverage, and writes `ci/security/dev-results.json`, `ci/security/development-results.json`, and this report. Tests use only synthetic scanner reports, CI metadata and FIRST fixtures; never the network or real held-out data.

To reproduce the application fixtures, use `python ci/security/prepare_development.py --go <Go executable>`. It prepares and checks builds locally in ignored state, refusing to overwrite prior preparations. The immutable scenario commits linked above retain the actual go.mod/go.sum and benign symbol-call probes. Rerunning hosted scans may see updated vulnerability databases.
