# Embedded verbatim in standalone scripts by deploy/sync-release-source.py.
# Keep panel release constants in internal/web/service/panel/panel.go in sync.
# FORK_RELEASE_PROTOCOL=1
XUI_RELEASE_REPO="LelandJin/3x-ui"
XUI_RELEASE_API="https://api.github.com/repos/${XUI_RELEASE_REPO}"
XUI_RELEASE_RAW="https://raw.githubusercontent.com/${XUI_RELEASE_REPO}"

xui_release_error() {
    echo "ERROR: $*. Existing installation has not been replaced." >&2
    return 1
}

xui_download() {
    local url="$1" dest="$2"
    curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 600 \
        -o "${dest}" "${url}" && [[ -s "${dest}" ]] || {
        rm -f "${dest}"
        xui_release_error "Download unavailable or empty: ${url}"
    }
}

xui_check_script() {
    grep -qx '# FORK_RELEASE_PROTOCOL=1' "$1" &&
        grep -qx 'XUI_RELEASE_REPO="LelandJin/3x-ui"' "$1" && bash -n "$1" || {
        xui_release_error "Release predates the fork updater; use a repaired fork release";
    }
}

# Re-execute standalone main downloads at the selected release commit too.
# Panel/CLI launchers already set the expected commit and skip this bootstrap.
xui_run_pinned_script() (
    local name="$1" temp
    temp=$(mktemp) || exit 1
    trap 'rm -f "${temp}"' EXIT
    xui_download "${XUI_RELEASE_RAW}/${XUI_RELEASE_COMMIT}/${name}" "${temp}" || exit 1
    xui_check_script "${temp}" || exit 1
    XUI_EXPECTED_COMMIT="${XUI_RELEASE_COMMIT}" XUI_UPDATE_TAG="${XUI_RELEASE_TAG}" \
        bash "${temp}" "${XUI_RELEASE_TAG}"
)

# No upstream or stable fallback: this fork currently publishes dev-latest only.
# An explicit stable channel or version must actually exist in this fork.
xui_resolve_release() {
    local requested="${1:-dev-latest}" endpoint commit_json
    command -v jq > /dev/null 2>&1 || { xui_release_error "jq is required (install jq and retry)"; return 1; }
    case "${requested}" in
        dev | dev-latest) requested="dev-latest"; endpoint="tags/dev-latest" ;;
        stable) endpoint="latest" ;;
        *)
            [[ "${requested}" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
                xui_release_error "Use dev, stable, or an explicit fork version such as v3.10.2"; return 1;
            }
            requested="v${requested#v}"; endpoint="tags/${requested}"
            ;;
    esac
    XUI_RELEASE_JSON=$(curl -fsSL --retry 3 --connect-timeout 15 --max-time 60 "${XUI_RELEASE_API}/releases/${endpoint}") || {
        xui_release_error "Fork release ${requested} is unavailable; use Dev (dev-latest) when no stable release exists"; return 1;
    }
    XUI_RELEASE_TAG=$(printf '%s' "${XUI_RELEASE_JSON}" | jq -er '.tag_name | select(type == "string")') || return 1
    [[ "${XUI_RELEASE_TAG}" == "${requested}" || ( "${requested}" == "stable" && "${XUI_RELEASE_TAG}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ) ]] || {
        xui_release_error "Unexpected fork release tag"; return 1;
    }
    # target_commitish on an edited rolling release can remain the original
    # creation commit. Only the CI body marker identifies the published build.
    if [[ "${XUI_RELEASE_TAG}" == "dev-latest" ]]; then
        XUI_RELEASE_COMMIT=$(printf '%s' "${XUI_RELEASE_JSON}" | jq -er '.body | capture("(?im)^commit=(?<sha>[0-9a-f]{40})\\r?$").sha | ascii_downcase') || {
            xui_release_error "Dev release has no full build commit marker"; return 1;
        }
    else
        commit_json=$(curl -fsSL --retry 3 --connect-timeout 15 --max-time 60 "${XUI_RELEASE_API}/commits/${XUI_RELEASE_TAG}") || return 1
        XUI_RELEASE_COMMIT=$(printf '%s' "${commit_json}" | jq -er '.sha') || return 1
    fi
    [[ "${XUI_RELEASE_COMMIT}" =~ ^[0-9a-f]{40}$ ]] || { xui_release_error "Release commit is unknown"; return 1; }
    # Panel/CLI launchers pass the exact release they used to obtain the script.
    [[ -z "${XUI_EXPECTED_COMMIT:-}" || "${XUI_EXPECTED_COMMIT}" == "${XUI_RELEASE_COMMIT}" ]] || {
        xui_release_error "Release moved while preparing the updater; retry"; return 1;
    }
}

xui_asset_url() {
    local name="$1" url expected
    expected="https://github.com/${XUI_RELEASE_REPO}/releases/download/${XUI_RELEASE_TAG}/${name}"
    url=$(printf '%s' "${XUI_RELEASE_JSON}" | jq -er --arg name "${name}" '[.assets[] | select(.name == $name) | .browser_download_url] | select(length == 1) | .[0]') || {
        xui_release_error "Required release asset ${name} is missing"; return 1;
    }
    [[ "${url}" == "${expected}" ]] || { xui_release_error "Unexpected asset source for ${name}"; return 1; }
    printf '%s\n' "${url}"
}

xui_verify_checksum() {
    local file="$1" sums="$2" expected actual
    expected=$(awk 'NR == 1 {print $1}' "${sums}")
    if command -v sha256sum > /dev/null 2>&1; then
        actual=$(sha256sum "${file}") || return 1
    else
        actual=$(shasum -a 256 "${file}") || return 1
    fi
    actual="${actual%% *}"
    [[ "${expected}" =~ ^[0-9a-f]{64}$ && "${actual}" == "${expected}" ]] || {
        xui_release_error "Required SHA256 checksum is invalid or does not match"; return 1;
    }
}

# Stage every required resource before the caller stops or removes anything.
# The archive marker also detects the publish-dev window where release notes
# point at commit B but some of the moving tag's assets still belong to A.
xui_prepare_release() {
    local stage="$1" platform="$2" os_release="$3" asset url sums_url marker unit name
    asset="x-ui-linux-${platform}.tar.gz"
    url=$(xui_asset_url "${asset}") || return 1
    sums_url=$(xui_asset_url "${asset}.sha256") || return 1
    xui_download "${url}" "${stage}/archive.tar.gz" || return 1
    xui_download "${sums_url}" "${stage}/archive.sha256" || return 1
    xui_verify_checksum "${stage}/archive.tar.gz" "${stage}/archive.sha256" || return 1
    tar -tzf "${stage}/archive.tar.gz" > "${stage}/entries" || { xui_release_error "Invalid release archive"; return 1; }
    awk '$0 !~ /^x-ui\// || $0 ~ /(^|\/)\.\.(\/|$)/ {bad=1} END {exit bad}' "${stage}/entries" || {
        xui_release_error "Unsafe archive paths"; return 1;
    }
    # Release packages contain regular files/directories, never links/devices.
    tar -tvzf "${stage}/archive.tar.gz" | awk 'substr($0,1,1) !~ /[-d]/ {bad=1} END {exit bad}' || {
        xui_release_error "Unsupported archive entry"; return 1;
    }
    tar -xzf "${stage}/archive.tar.gz" -C "${stage}" || { xui_release_error "Cannot extract release"; return 1; }
    marker=$(cat "${stage}/x-ui/.release-commit" 2> /dev/null)
    [[ "${marker}" == "${XUI_RELEASE_COMMIT}" ]] || {
        xui_release_error "Archive build commit does not match release; retry after publishing finishes (older unmarked artifacts are unsupported)"; return 1;
    }
    [[ -s "${stage}/x-ui/x-ui" && -s "${stage}/x-ui/bin/xray-linux-${platform}" ]] || {
        xui_release_error "Release is missing required binaries"; return 1;
    }
    case "${os_release}" in
        alpine) unit="x-ui.rc" ;;
        ubuntu | debian | armbian) unit="x-ui.service.debian" ;;
        arch | manjaro | parch) unit="x-ui.service.arch" ;;
        *) unit="x-ui.service.rhel" ;;
    esac
    # Prefer checksum-covered companions; fallback is pinned to the full commit.
    [[ "${os_release}" != "alpine" && -s "${stage}/x-ui/x-ui.service" ]] && unit="x-ui.service"
    for name in x-ui.sh "${unit}"; do
        if [[ ! -s "${stage}/x-ui/${name}" ]]; then
            xui_download "${XUI_RELEASE_RAW}/${XUI_RELEASE_COMMIT}/${name}" "${stage}/x-ui/${name}" || return 1
        fi
    done
    cp "${stage}/x-ui/${unit}" "${stage}/service-unit" || return 1
    bash -n "${stage}/x-ui/x-ui.sh" || { xui_release_error "Invalid CLI script"; return 1; }
    rm -f "${stage}/archive.tar.gz" "${stage}/archive.sha256" "${stage}/entries"
}
