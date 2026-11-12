#!/usr/bin/env bash
# Checks a deployed sidecar from outside, over its public URL: that it is only
# served over HTTPS, that it rejects calls without a token (400) and with a
# wrong one (401), that it lists the target Deployment and reports its status,
# and that it refuses a Deployment that isn't a target (403). It only reads:
# no notification is sent, nothing is rolled out.
#
# The token comes from the environment variable `ACCESS_TOKEN` and goes to
# curl on its standard input (`--config -`), never on the command line, where
# other users of the machine could see it. It is sent only over HTTPS (or over
# HTTP to this machine, with `CHECK_ALLOW_HTTP=1`, for tests against a port
# forward).
#
# Needs bash, curl and jq.
#
# Usage: tests/check_deployment.sh <url> <target> [<non-target>]
#   url         Base URL of the sidecar, e.g., https://sidecar.example.org
#   target      A Deployment of `targetDeployments` that exists
#   non-target  A Deployment name that isn't a target (default:
#               pubgrade-check-not-a-target; it needn't exist)
# Environment: ACCESS_TOKEN, the sidecar's token (required); CA_CERT, a CA
# certificate file to verify the server with, instead of the system's;
# CHECK_ALLOW_HTTP=1 allows an `http://` URL to localhost, 127.0.0.1 or [::1],
# and skips the HTTPS checks.
#
# Prints one line per check, `ok`, `FAIL` or `skip`. Exit status: 0 if all
# checks passed, 1 if any failed, 2 for wrong usage.
set -euo pipefail

usage() {
  echo "Usage: $0 <url> <target> [<non-target>]" >&2
  echo "The token comes from the environment variable ACCESS_TOKEN." >&2
  exit 2
}

(($# >= 2 && $# <= 3)) || usage
URL="${1%/}"
TARGET="$2"
NON_TARGET="${3:-pubgrade-check-not-a-target}"
TOKEN="${ACCESS_TOKEN:-}"
# Kubernetes object names (DNS subdomains), so that they are safe in a path
NAME_PATTERN='^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$'

if [[ -z "${TOKEN}" ]]; then
  echo "ACCESS_TOKEN is not set." >&2
  usage
fi
# A line break would end the header, and curl's configuration line
if [[ "${TOKEN}" == *[$'\r\n']* ]]; then
  echo "ACCESS_TOKEN must be a single line." >&2
  exit 2
fi
for name in "${TARGET}" "${NON_TARGET}"; do
  if ! [[ "${name}" =~ ${NAME_PATTERN} ]]; then
    echo "Not a Deployment name: ${name}" >&2
    exit 2
  fi
done
if [[ "${TARGET}" == "${NON_TARGET}" ]]; then
  echo "The target and the non-target must differ." >&2
  exit 2
fi
# Scheme, host (a name or an IPv6 address in brackets), optional port and path;
# no user information, query or fragment
if ! [[ "${URL}" =~ ^(https?)://([^/:@?#\[]+|\[[0-9A-Fa-f:.]+\])(:[0-9]+)?(/[^?#]*)?$ ]]; then
  echo "Not a valid base URL: ${URL}" >&2
  exit 2
fi
SCHEME="${BASH_REMATCH[1]}"
HOST="${BASH_REMATCH[2]}"
PORT="${BASH_REMATCH[3]}"
BASE_PATH="${BASH_REMATCH[4]}"
if [[ "${SCHEME}" == http ]]; then
  case "${HOST}" in
    localhost | 127.0.0.1 | "[::1]") ;;
    *) HOST="" ;;
  esac
  if [[ "${CHECK_ALLOW_HTTP:-}" != 1 || -z "${HOST}" ]]; then
    echo "FAIL  HTTPS: ${URL} isn't an https:// URL; the token isn't sent." >&2
    exit 1
  fi
fi

# Without `~/.curlrc` (`-q`, first), which could, e.g., print the headers;
# only the scheme of the URL
CURL=(curl -q --silent --show-error --max-time 15 --proto "=${SCHEME}")
if [[ -n "${CA_CERT:-}" ]]; then
  CURL+=(--cacert "${CA_CERT}")
fi
BODY="$(mktemp "${TMPDIR:-/tmp}/sidecar-check.XXXXXX")"
trap 'rm -f "${BODY}"' EXIT
FAILED=0

ok() { echo "ok    $1"; }
fail() {
  echo "FAIL  $1"
  FAILED=1
}

# Calls `GET <url><path>` with a token (none if empty); the body goes to
# `${BODY}`, the status code to stdout (000 if there was no response). The
# header goes to curl on stdin, as a line of its configuration, in double
# quotes, with backslashes and double quotes escaped
call() {
  local path="$1" token="$2" escaped
  : > "${BODY}"
  if [[ -z "${token}" ]]; then
    "${CURL[@]}" --output "${BODY}" --write-out '%{http_code}' \
      "${URL}${path}" < /dev/null || true
    return
  fi
  escaped="${token//\\/\\\\}"
  escaped="${escaped//\"/\\\"}"
  printf 'header = "X-Access-Token: %s"\n' "${escaped}" \
    | "${CURL[@]}" --config - --output "${BODY}" --write-out '%{http_code}' \
      "${URL}${path}" || true
}

# Checks the status code of a call
expect_status() {
  local what="$1" want="$2" got="$3"
  if [[ "${got}" == "${want}" ]]; then
    ok "${what} (${got})"
  else
    fail "${what}: want status ${want}, got ${got}$(problem)"
  fi
}

# The detail of a problem response, if any, for failure messages
problem() {
  local detail
  detail="$(jq --raw-output '.detail // empty' "${BODY}" 2> /dev/null || true)"
  if [[ -n "${detail}" ]]; then
    echo " (${detail})"
  fi
}

# The body of the last call, as compact JSON if it is JSON, at most 200
# characters
excerpt() {
  { jq --compact-output . "${BODY}" 2> /dev/null || cat "${BODY}"; } \
    | tr -s '\n' ' ' | cut -c 1-200
}

echo "== ${URL}"

# HTTPS: the certificate is verified by every call below. Plain HTTP on the
# same host must not serve the API: no answer, or a redirect to HTTPS. With a
# port in the URL, the HTTP port is unknown, so this is skipped
if [[ "${SCHEME}" == http ]]; then
  echo "skip  HTTPS (CHECK_ALLOW_HTTP=1)"
elif [[ -n "${PORT}" ]]; then
  echo "skip  HTTP not served (the URL has a port; check the HTTP port yourself)"
else
  result="$(curl -q --silent --max-time 10 --proto "=http" --output /dev/null \
    --write-out '%{http_code} %{redirect_url}' \
    "http://${HOST}${BASE_PATH}/deployment" 2> /dev/null)" || result=""
  code="${result%% *}"
  location="${result#* }"
  if [[ -z "${result}" || "${code}" == 000 ]]; then
    ok "HTTP not served (no answer on port 80)"
  elif [[ "${code}" == 3?? && "${location}" == https://* ]]; then
    ok "HTTP redirected to HTTPS (${code})"
  else
    fail "HTTP not served: http://${HOST}${BASE_PATH}/deployment answered ${code}, want a redirect to HTTPS or no answer"
  fi
fi

expect_status "token missing" 400 "$(call /deployment "")"

# Random, and different from the token
wrong="wrong-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
expect_status "token wrong" 401 "$(call /deployment "${wrong}")"

status="$(call /deployment "${TOKEN}")"
expect_status "Deployments listed" 200 "${status}"
if [[ "${status}" == 200 ]]; then
  if jq --exit-status --arg name "${TARGET}" 'index($name) != null' \
    "${BODY}" > /dev/null 2>&1; then
    ok "target ${TARGET} listed"
  else
    fail "target ${TARGET} listed: got $(excerpt)"
  fi
else
  fail "target ${TARGET} listed: no list"
fi

status="$(call "/deployment/${TARGET}" "${TOKEN}")"
expect_status "status of ${TARGET}" 200 "${status}"
if [[ "${status}" == 200 ]]; then
  echo "      $(jq --compact-output '{image, status}' "${BODY}" 2> /dev/null || excerpt)"
fi

expect_status "non-target ${NON_TARGET} refused" 403 \
  "$(call "/deployment/${NON_TARGET}" "${TOKEN}")"

if ((FAILED != 0)); then
  echo "Check failed"
  exit 1
fi
echo "Check passed"
