package panel

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/mhsanaei/3x-ui/v3/internal/config"
	"github.com/mhsanaei/3x-ui/v3/internal/web/service"
)

func TestIsNewerVersion(t *testing.T) {
	cases := []struct {
		latest  string
		current string
		want    bool
	}{
		{"v2.9.4", "2.9.3", true},
		{"v2.10.0", "2.9.9", true},
		{"v2.9.3", "2.9.3", false},
		{"v2.9.2", "2.9.3", false},
		{"v3.0.0", "2.9.3", true},
	}

	for _, tc := range cases {
		if got := isNewerVersion(tc.latest, tc.current); got != tc.want {
			t.Fatalf("isNewerVersion(%q, %q) = %v, want %v", tc.latest, tc.current, got, tc.want)
		}
	}
}

func TestShellQuote(t *testing.T) {
	if got := shellQuote("/usr/bin/curl"); got != "'/usr/bin/curl'" {
		t.Fatalf("unexpected quote result: %s", got)
	}
	if got := shellQuote("/tmp/a'b"); got != "'/tmp/a'\\''b'" {
		t.Fatalf("unexpected quote result with single quote: %s", got)
	}
}

// TestUpdateProxyEnvVars covers the bug this function fixes: ambient proxy
// vars must reach update.sh's systemd-run child, which inherits nothing.
func TestUpdateProxyEnvVars(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows env var names are case-insensitive, so both spellings resolve; the updater runs only on Linux")
	}
	allKeys := []string{"https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"}
	clearAll := func(t *testing.T) {
		t.Helper()
		for _, key := range allKeys {
			t.Setenv(key, "")
		}
	}

	t.Run("nothing set returns nil", func(t *testing.T) {
		clearAll(t)
		if got := updateProxyEnvVars(); got != nil {
			t.Fatalf("updateProxyEnvVars() = %v, want nil", got)
		}
	})

	t.Run("forwards each set var under its own name", func(t *testing.T) {
		clearAll(t)
		t.Setenv("https_proxy", "socks5://127.0.0.1:10808")
		t.Setenv("no_proxy", "10.0.0.0/8,localhost")
		got := updateProxyEnvVars()
		want := []string{"https_proxy=socks5://127.0.0.1:10808", "no_proxy=10.0.0.0/8,localhost"}
		if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
			t.Fatalf("updateProxyEnvVars() = %v, want %v", got, want)
		}
	})

	// A deliberately HTTP-only proxy config must not silently gain HTTPS traffic.
	t.Run("http_proxy is not promoted to https_proxy", func(t *testing.T) {
		clearAll(t)
		t.Setenv("http_proxy", "http://127.0.0.1:8080")
		got := updateProxyEnvVars()
		want := []string{"http_proxy=http://127.0.0.1:8080"}
		if len(got) != len(want) || got[0] != want[0] {
			t.Fatalf("updateProxyEnvVars() = %v, want %v", got, want)
		}
	})
}

func TestExtractReleaseCommit(t *testing.T) {
	full := "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
	cases := []struct {
		name    string
		release service.Release
		want    string
	}{
		{
			name:    "from body marker",
			release: service.Release{Body: "Rolling build\n\ncommit=" + full + "\nbuilt=2026-06-24T00:00:00Z"},
			want:    full,
		},
		{
			name:    "body marker is case-insensitive and wins over target",
			release: service.Release{Body: "COMMIT=" + full, TargetCommitish: "deadbeef"},
			want:    full,
		},
		{
			name:    "stale target commit is not a build marker",
			release: service.Release{Body: "no marker here", TargetCommitish: full},
			want:    "",
		},
		{
			name:    "branch target is not a commit",
			release: service.Release{Body: "no marker", TargetCommitish: "main"},
			want:    "",
		},
		{name: "short marker refused", release: service.Release{Body: "commit=deadbeef"}},
		{name: "long marker refused", release: service.Release{Body: "commit=" + full + "a"}},
		{name: "inline marker refused", release: service.Release{Body: "not commit=" + full}},
	}
	for _, tc := range cases {
		if got := extractReleaseCommit(&tc.release); got != tc.want {
			t.Fatalf("%s: extractReleaseCommit = %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestCommitsEqual(t *testing.T) {
	full := "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
	cases := []struct {
		a, b string
		want bool
	}{
		{"1a2b3c4d", full, true},  // injected 8-char prefix matches full release sha
		{full, "1a2b3c4d", true},  // order independent
		{"1A2B3C4D", full, true},  // case insensitive
		{"deadbeef", full, false}, // different commit
		{"", full, false},         // empty current never matches
		{"1a2b3c4d", "", false},   // empty latest never matches
		{"1", full, false},
		{"unknown", full, false},
	}
	for _, tc := range cases {
		if got := commitsEqual(tc.a, tc.b); got != tc.want {
			t.Fatalf("commitsEqual(%q, %q) = %v, want %v", tc.a, tc.b, got, tc.want)
		}
	}
}

type panelRoundTripFunc func(*http.Request) (*http.Response, error)

func (f panelRoundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func panelFixtureClient(t *testing.T, routes map[string]string) *http.Client {
	t.Helper()
	return &http.Client{Transport: panelRoundTripFunc(func(r *http.Request) (*http.Response, error) {
		body, ok := routes[r.URL.String()]
		if !ok {
			t.Errorf("unexpected release request: %s", r.URL)
			return nil, fmt.Errorf("unmocked URL")
		}
		status := http.StatusOK
		if body == "404" {
			status = http.StatusNotFound
		}
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}, nil
	})}
}

func TestForkPanelReleaseResolution(t *testing.T) {
	full := strings.Repeat("a", 40)
	for _, dev := range []bool{true, false} {
		t.Run(fmt.Sprintf("dev=%v", dev), func(t *testing.T) {
			tag, endpoint := "v3.10.2", "/releases/latest"
			if dev {
				tag, endpoint = "dev-latest", "/releases/tags/dev-latest"
			}
			routes := map[string]string{
				panelReleaseAPI + endpoint:           fmt.Sprintf(`{"tag_name":%q,"body":%q,"target_commitish":%q}`, tag, "commit="+full, strings.Repeat("b", 40)),
				panelReleaseAPI + "/commits/v3.10.2": fmt.Sprintf(`{"sha":%q}`, full),
			}
			release, commit, err := resolvePanelUpdateReleaseWithClient(panelFixtureClient(t, routes), dev)
			if err != nil || commit != full || release.TagName != tag {
				t.Fatalf("resolve: %v %q %v", release, commit, err)
			}
		})
	}
	client := panelFixtureClient(t, map[string]string{panelReleaseAPI + "/releases/latest": "404"})
	if _, _, err := resolvePanelUpdateReleaseWithClient(client, false); err == nil || !strings.Contains(err.Error(), "Dev channel") {
		t.Fatalf("missing stable must explain Dev channel, got %v", err)
	}
	client = panelFixtureClient(t, map[string]string{panelReleaseAPI + "/releases/tags/dev-latest": `{"tag_name":"dev-latest","target_commitish":"` + full + `"}`})
	if _, _, err := resolvePanelUpdateReleaseWithClient(client, true); err == nil {
		t.Fatal("missing Dev build marker accepted")
	}
}

func TestForkPanelUpdaterDownload(t *testing.T) {
	full := strings.Repeat("a", 40)
	url, err := panelUpdaterURL(full)
	if err != nil || url != "https://raw.githubusercontent.com/LelandJin/3x-ui/"+full+"/update.sh" {
		t.Fatalf("unpinned updater: %q %v", url, err)
	}
	for _, invalid := range []string{"main", "dev-latest", "deadbeef", full + "a"} {
		if _, err := panelUpdaterURL(invalid); err == nil {
			t.Fatalf("accepted ref %q", invalid)
		}
	}
	for _, body := range []string{"404", "", "#!/bin/bash\necho old-upstream-updater\n", strings.Repeat("x", maxPanelUpdaterBytes+1)} {
		client := panelFixtureClient(t, map[string]string{url: body})
		if path, err := downloadPanelUpdaterWithClient(client, full); err == nil {
			_ = os.Remove(path)
			t.Fatalf("accepted invalid updater of %d bytes", len(body))
		}
	}
	body := "#!/bin/bash\n# FORK_RELEASE_PROTOCOL=1\nXUI_RELEASE_REPO=\"LelandJin/3x-ui\"\n"
	path, err := downloadPanelUpdaterWithClient(panelFixtureClient(t, map[string]string{url: body}), full)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Remove(path) })
	data, err := os.ReadFile(path)
	if err != nil || string(data) != body {
		t.Fatalf("download content: %q %v", data, err)
	}
}

func TestShortCommit(t *testing.T) {
	if got := shortCommit("1a2b3c4d5e6f7a8b"); got != "1a2b3c4d" {
		t.Fatalf("shortCommit truncation = %q, want %q", got, "1a2b3c4d")
	}
	if got := shortCommit("abc"); got != "abc" {
		t.Fatalf("shortCommit short input = %q, want %q", got, "abc")
	}
}

func resetUpdateSlot(t *testing.T) {
	t.Helper()
	t.Cleanup(func() {
		updateMu.Lock()
		updateRunning = false
		updateRunID = 0
		updatePID = 0
		updateMu.Unlock()
	})
}

// writeStatusFile hand-writes the status file in the exact wire format
// update.sh itself produces (a bare printf, not Go's json.Marshal), since
// that's the real cross-language contract this package reads in production.
func writeStatusFile(t *testing.T, path string, runID int64, state string) {
	t.Helper()
	body := fmt.Sprintf(`{"runId":"%d","state":"%s","exitCode":0,"finishedAt":%d}`, runID, state, time.Now().Unix())
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestAcquireUpdateSlot(t *testing.T) {
	resetUpdateSlot(t)

	if !acquireUpdateSlot(1) {
		t.Fatal("first acquire: got false, want true")
	}
	if acquireUpdateSlot(2) {
		t.Fatal("second acquire while first is held: got true, want false")
	}
	releaseUpdateSlot()
	if !acquireUpdateSlot(3) {
		t.Fatal("acquire after release: got false, want true")
	}
	releaseUpdateSlot()
}

func TestAcquireUpdateSlotExpiresAfterStaleWindow(t *testing.T) {
	resetUpdateSlot(t)

	if !acquireUpdateSlot(1) {
		t.Fatal("first acquire: got false, want true")
	}
	updateMu.Lock()
	updateStarted = time.Now().Add(-(updateStaleAfter + time.Second))
	updateMu.Unlock()

	if !acquireUpdateSlot(2) {
		t.Fatal("acquire after stale window elapsed: got false, want true")
	}
	releaseUpdateSlot()
}

// TestAcquireUpdateSlotWaitsForAliveProcessPastStaleWindow is the regression
// test for the concurrency bug an upstream review found: past
// updateStaleAfter, the old logic freed the slot purely on elapsed time, even
// if the process it launched was still genuinely running (not crashed) --
// update.sh's own package-manager step plus several downloads can plausibly
// run long on a slow host with nothing actually wrong. Now a confirmed-alive
// PID keeps the slot held past the stale window.
func TestAcquireUpdateSlotWaitsForAliveProcessPastStaleWindow(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("processAlive is a no-op stub on non-Linux; this test only exercises real liveness checking on Linux")
	}
	resetUpdateSlot(t)

	if !acquireUpdateSlot(1) {
		t.Fatal("first acquire: got false, want true")
	}
	recordUpdatePID(os.Getpid()) // the test process itself: guaranteed alive
	updateMu.Lock()
	updateStarted = time.Now().Add(-(updateStaleAfter + time.Second))
	updateMu.Unlock()

	if acquireUpdateSlot(2) {
		t.Fatal("acquire past the stale window while the recorded PID is still alive: got true, want false")
	}
	releaseUpdateSlot()
}

// TestAcquireUpdateSlotHardCeilingOverridesLiveness confirms the absolute
// backstop: even a confirmed-alive process can't hold the slot forever, so a
// genuinely wedged run can't lock out retries permanently.
func TestAcquireUpdateSlotHardCeilingOverridesLiveness(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("processAlive is a no-op stub on non-Linux; this test only exercises real liveness checking on Linux")
	}
	resetUpdateSlot(t)

	if !acquireUpdateSlot(1) {
		t.Fatal("first acquire: got false, want true")
	}
	recordUpdatePID(os.Getpid())
	updateMu.Lock()
	updateStarted = time.Now().Add(-(updateHardCeiling + time.Second))
	updateMu.Unlock()

	if !acquireUpdateSlot(2) {
		t.Fatal("acquire past the hard ceiling despite a live PID: got false, want true")
	}
	releaseUpdateSlot()
}

// TestAcquireUpdateSlotReleasesOnTerminalStatus is the regression test for the
// bug adversarial review found: a fast failure used to still lock out retries
// for the full updateStaleAfter window, because acquireUpdateSlot only looked
// at the in-memory started-at timestamp, never at the status file's own
// terminal state.
func TestAcquireUpdateSlotReleasesOnTerminalStatus(t *testing.T) {
	t.Setenv("XUI_DB_FOLDER", t.TempDir())
	resetUpdateSlot(t)
	path := config.GetUpdateStatusFilePath()

	if !acquireUpdateSlot(111) {
		t.Fatal("first acquire: got false, want true")
	}
	writeStatusFile(t, path, 111, updateStateFailed)

	if !acquireUpdateSlot(222) {
		t.Fatal("acquire after the in-flight run reported failed: got false, want true (should not wait out updateStaleAfter)")
	}
	releaseUpdateSlot()
}

// TestAcquireUpdateSlotIgnoresStaleUnrelatedStatus confirms the terminal-state
// check is scoped to the run it actually launched: a status file left behind
// by some earlier, unrelated run (different runID) must not be mistaken for
// this run finishing.
func TestAcquireUpdateSlotIgnoresStaleUnrelatedStatus(t *testing.T) {
	t.Setenv("XUI_DB_FOLDER", t.TempDir())
	resetUpdateSlot(t)
	path := config.GetUpdateStatusFilePath()

	writeStatusFile(t, path, 999, updateStateSuccess)
	if !acquireUpdateSlot(111) {
		t.Fatal("first acquire: got false, want true")
	}

	if acquireUpdateSlot(222) {
		t.Fatal("acquire while status file only reflects an unrelated older runID: got true, want false")
	}
	releaseUpdateSlot()
}

// TestAcquireUpdateSlotConcurrency proves the check-then-set is actually
// atomic under real concurrent access, not just correct when called
// sequentially. A prior version of this test suite only ever called
// acquireUpdateSlot from a single goroutine, so it gave no signal if the
// mutex's core promise (only one concurrent launch wins) were broken.
func TestAcquireUpdateSlotConcurrency(t *testing.T) {
	resetUpdateSlot(t)

	const attempts = 200
	var wins atomic.Int32
	var wg sync.WaitGroup
	wg.Add(attempts)
	for i := range attempts {
		go func(runID int64) {
			defer wg.Done()
			if acquireUpdateSlot(runID) {
				wins.Add(1)
			}
		}(int64(i))
	}
	wg.Wait()

	if got := wins.Load(); got != 1 {
		t.Fatalf("concurrent acquireUpdateSlot: %d of %d attempts won, want exactly 1", got, attempts)
	}
	releaseUpdateSlot()
}

func TestGetUpdateStatus(t *testing.T) {
	t.Setenv("XUI_DB_FOLDER", t.TempDir())
	path := config.GetUpdateStatusFilePath()
	svc := &PanelService{}

	if got := svc.GetUpdateStatus(); got.State != updateStatePending {
		t.Fatalf("missing status file: State = %q, want %q", got.State, updateStatePending)
	}

	writeStatusFile(t, path, 1735689600123456789, updateStateSuccess)
	got := svc.GetUpdateStatus()
	if got.RunID != "1735689600123456789" {
		t.Fatalf("RunID = %q, want %q (must round-trip as a decimal string, not a JSON number, or it loses precision past 2^53 in JS)", got.RunID, "1735689600123456789")
	}
	if got.State != updateStateSuccess {
		t.Fatalf("State = %q, want %q", got.State, updateStateSuccess)
	}

	if err := os.WriteFile(path, []byte("not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := svc.GetUpdateStatus(); got.State != updateStatePending {
		t.Fatalf("corrupt status file: State = %q, want %q", got.State, updateStatePending)
	}

	writeStatusFile(t, path, 1, "some-unrecognized-state")
	if got := svc.GetUpdateStatus(); got.State != updateStatePending {
		t.Fatalf("unrecognized state normalizes to pending: State = %q, want %q", got.State, updateStatePending)
	}
}
