#!/usr/bin/env bash
# Integration test on a real, throw-away Kubernetes cluster (k3s in Docker):
# builds the image from this checkout, installs the chart into two namespaces,
# sends notifications and checks that the target Deployments roll out to the
# new tag and that the status reports it. The chart's minimal Role applies in
# both namespaces; in the second one, the sidecar also runs as an arbitrary
# user ID with group 0, as on OpenShift, and its target Deployment spells the
# image with the `docker.io/` prefix. In the first one, a notification for all
# Deployments of an image repository also updates a Deployment with several
# containers, and the check of deployed instances (`tests/check_deployment.sh`)
# runs against its sidecar.
#
# Needs bash 4.4 or newer, Docker (privileged containers), kubectl, Helm, curl
# and jq, and Internet access: the cluster pulls the target images from Docker
# Hub, and the sidecar checks Docker Hub for them. At the end, it removes the
# cluster, with its volumes, and the image it built. kubectl and Helm use a
# kubeconfig of their own, so no other cluster is touched.
#
# Usage: tests/integration/k3s.sh
# Environment (optional): K3S_IMAGE, the k3s image; API_PORT, the port of the
# Kubernetes API on 127.0.0.1 (default 16443); FORWARD_PORT, the first of two
# ports for the sidecars (default 18080); KEEP=1 keeps the cluster and the
# image for debugging (remove them with `docker rm -f -v
# pubgrade-sidecar-integration` and `docker rmi pubgrade-sidecar:integration`)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Also removed at the start, if left from an earlier run
NAME=pubgrade-sidecar-integration
IMAGE_REPOSITORY=pubgrade-sidecar
IMAGE_TAG=integration
K3S_IMAGE="${K3S_IMAGE:-rancher/k3s:v1.35.10-k3s1}"
API_PORT="${API_PORT:-16443}"
FORWARD_PORT="${FORWARD_PORT:-18080}"
# Unprivileged, so that the target Deployments run under the defaults of
# both namespaces
TARGET_REPO=nginxinc/nginx-unprivileged
OLD_TAG=1.27-alpine
NEW_TAG=1.28-alpine
MISSING_TAG=no-such-tag-integration
# Another image, which notifications for the target repository leave alone
OTHER_IMAGE=busybox:1.37
# As OpenShift assigns them: a high user ID, and group 0
ARBITRARY_UID=1000770000
NAMESPACES=(site-a site-b)
TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sidecar-integration.XXXXXX")"
export KUBECONFIG="${WORK}/kubeconfig"
FORWARDS=()
declare -A PORTS
FAILED=0

cleanup() {
  local status=$?
  # Not cut short by a further signal
  trap '' INT TERM
  if ((status != 0 || FAILED != 0)) && [[ -s "${KUBECONFIG}" ]]; then
    echo "== diagnostics"
    for ns in "${NAMESPACES[@]}"; do
      kubectl -n "${ns}" get pods -o wide || true
      kubectl -n "${ns}" get events --sort-by=.lastTimestamp | tail -20 || true
      kubectl -n "${ns}" logs deployment/pubgrade-sidecar --tail=50 || true
    done
  fi
  for pid in "${FORWARDS[@]}"; do
    kill "${pid}" 2> /dev/null || true
  done
  if [[ "${KEEP:-}" == 1 ]]; then
    echo "Kept the cluster: KUBECONFIG=${KUBECONFIG}"
  else
    docker rm -f -v "${NAME}" > /dev/null 2>&1 || true
    docker rmi "${IMAGE_REPOSITORY}:${IMAGE_TAG}" > /dev/null 2>&1 || true
    rm -rf "${WORK}"
  fi
}
trap cleanup EXIT
# Clean up also when stopped
trap 'exit 130' INT
trap 'exit 143' TERM

# Runs a command until it succeeds, at most a number of times with a delay in
# seconds; fails with a message if it never does
retry() {
  local tries="$1" delay="$2" what="$3" i
  shift 3
  for ((i = 1; i <= tries; i++)); do
    if "$@" > /dev/null 2>&1; then
      return 0
    fi
    sleep "${delay}"
  done
  echo "Timed out waiting for ${what}" >&2
  return 1
}

# Checks that a value contains what is wanted; records a failure, so that all
# checks run
expect() {
  local what="$1" want="$2" got="$3"
  if [[ "${got}" == *"${want}"* ]]; then
    echo "ok    ${what}"
  else
    echo "FAIL  ${what}: want ${want}, got ${got}"
    FAILED=1
  fi
}

# Checks the exit status of a command and that its output (stdout and stderr,
# to `${WORK}/output`, shown on failure) has a line that matches an extended
# regular expression; records a failure, so that all checks run
expect_exit() {
  local what="$1" want="$2" pattern="$3" got=0
  shift 3
  "$@" > "${WORK}/output" 2>&1 || got=$?
  if [[ "${got}" != "${want}" ]]; then
    echo "FAIL  check script ${what}: want exit status ${want}, got ${got}"
  elif ! grep --quiet --extended-regexp -- "${pattern}" "${WORK}/output"; then
    echo "FAIL  check script ${what}: no line matches ${pattern}"
  else
    echo "ok    check script ${what}"
    return
  fi
  sed 's/^/      /' "${WORK}/output"
  FAILED=1
}

# Calls the sidecar of a namespace; the body goes to `${WORK}/body`, the
# status code to stdout
call() {
  local ns="$1" method="$2" path="$3" token="$4" body="${5:-}"
  # No stale body of an earlier call, if this one fails
  rm -f "${WORK}/body"
  local args=(--silent --show-error --max-time 15 --output "${WORK}/body"
    --write-out '%{http_code}' --request "${method}"
    --header "X-Access-Token: ${token}")
  if [[ -n "${body}" ]]; then
    args+=(--header 'Content-Type: application/json' --data "${body}")
  fi
  curl "${args[@]}" "http://127.0.0.1:${PORTS[${ns}]}${path}"
}

# Reports the status of the target Deployment as one line of JSON (or the
# response as is, if it isn't JSON)
report() {
  call "$1" GET /deployment/tesk-api "${TOKEN}" > /dev/null || true
  jq --compact-output --sort-keys . "${WORK}/body" 2> /dev/null \
    || cat "${WORK}/body"
}

# Polls the status of the target Deployment until a jq filter is true, at most
# about three minutes; prints the last status
await_report() {
  local ns="$1" filter="$2" out=""
  for _ in $(seq 1 60); do
    out="$(report "${ns}")"
    if jq --exit-status "${filter}" <<< "${out}" > /dev/null 2>&1; then
      break
    fi
    sleep 3
  done
  echo "${out}"
}

notify() {
  local ns="$1" deployment="$2" repository="$3" tag="$4"
  call "${ns}" PUT "/deployment/${deployment}" "${TOKEN}" \
    "$(jq --null-input --compact-output --arg image "${repository}" \
      --arg tag "${tag}" '{image_name: $image, tag: $tag}')"
}

image_of() {
  kubectl -n "$1" get deployment "$2" \
    -o jsonpath='{.spec.template.spec.containers[0].image}'
}

# The images of all containers of a Deployment, separated by spaces
images_of() {
  kubectl -n "$1" get deployment "$2" \
    -o jsonpath='{.spec.template.spec.containers[*].image}'
}

# Whether a Deployment has the images, in the order of its containers
has_images() {
  [[ "$(images_of "$1" "$2")" == "$3" ]]
}

# Whether the cluster has registered its node
node_registered() {
  [[ -n "$(kubectl get nodes --no-headers 2> /dev/null)" ]]
}

# Whether a namespace has exactly one sidecar pod, e.g., after a rollout, once
# the old pod is gone
one_sidecar_pod() {
  [[ "$(kubectl -n "$1" get pods -l app=pubgrade-sidecar --no-headers | wc -l)" -eq 1 ]]
}

echo "== build image"
docker build --quiet --tag "${IMAGE_REPOSITORY}:${IMAGE_TAG}" "${ROOT}" > /dev/null

echo "== start k3s (${K3S_IMAGE})"
docker rm -f -v "${NAME}" > /dev/null 2>&1 || true
docker run --detach --privileged --name "${NAME}" \
  --publish "127.0.0.1:${API_PORT}:6443" "${K3S_IMAGE}" server \
  --disable traefik --disable metrics-server > /dev/null
retry 60 2 "the kubeconfig" docker exec "${NAME}" test -s /etc/rancher/k3s/k3s.yaml
docker exec "${NAME}" cat /etc/rancher/k3s/k3s.yaml \
  | sed "s#https://127.0.0.1:6443#https://127.0.0.1:${API_PORT}#" > "${KUBECONFIG}"
chmod 600 "${KUBECONFIG}"
retry 60 2 "the node" node_registered
kubectl wait --for=condition=Ready node --all --timeout=120s > /dev/null
# The sidecar resolves Docker Hub, so DNS must be up
retry 60 2 "CoreDNS" kubectl -n kube-system get deployment coredns
kubectl -n kube-system rollout status deployment/coredns --timeout=180s > /dev/null
# Into the namespace that Kubernetes uses
docker save "${IMAGE_REPOSITORY}:${IMAGE_TAG}" \
  | docker exec -i "${NAME}" ctr -n k8s.io images import - > /dev/null

echo "== target Deployments"
for ns in "${NAMESPACES[@]}"; do
  kubectl create namespace "${ns}" > /dev/null
  kubectl -n "${ns}" create secret generic sidecar-token \
    --from-literal=access-token="${TOKEN}" > /dev/null
done
kubectl -n site-a create deployment tesk-api \
  --image="${TARGET_REPO}:${OLD_TAG}" > /dev/null
# The Docker Hub prefix, which the sidecar must keep
kubectl -n site-b create deployment tesk-api \
  --image="docker.io/${TARGET_REPO}:${OLD_TAG}" > /dev/null
# Not in `targetDeployments`, so outside the sidecar's Role
kubectl -n site-a create deployment bystander \
  --image="${TARGET_REPO}:${OLD_TAG}" > /dev/null
# Several containers, two of them of the target repository, in both
# spellings; without replicas, as they would share a port. Only in site-a: in
# site-b, the listed Deployment is missing
jq --null-input --arg a "${TARGET_REPO}:${OLD_TAG}" \
  --arg b "docker.io/${TARGET_REPO}:${OLD_TAG}" --arg c "${OTHER_IMAGE}" '{
    apiVersion: "apps/v1", kind: "Deployment",
    metadata: {name: "tesk-worker"},
    spec: {replicas: 0, selector: {matchLabels: {app: "tesk-worker"}},
      template: {metadata: {labels: {app: "tesk-worker"}},
        spec: {containers: [{name: "a", image: $a}, {name: "b", image: $b},
          {name: "c", image: $c}]}}}}' \
  | kubectl -n site-a apply -f - > /dev/null

echo "== install the chart"
for ns in "${NAMESPACES[@]}"; do
  helm install sidecar "${ROOT}/deployment" --namespace "${ns}" \
    --set 'targetDeployments[0]=tesk-api' \
    --set 'targetDeployments[1]=tesk-worker' \
    --set accessToken.existingSecret=sidecar-token \
    --set image.repository="${IMAGE_REPOSITORY}" \
    --set image.tag="${IMAGE_TAG}" \
    --set image.pullPolicy=Never > /dev/null
done
# In site-b, an arbitrary user ID with group 0, as OpenShift assigns them. The
# chart has no value for the user (OpenShift sets it), so the Deployment is
# patched, which rolls it out again
kubectl -n site-b patch deployment pubgrade-sidecar --type=json --patch "$(
  jq --null-input --compact-output --argjson uid "${ARBITRARY_UID}" '[
    {op: "add", path: "/spec/template/spec/securityContext/runAsUser",
     value: $uid},
    {op: "add", path: "/spec/template/spec/securityContext/runAsGroup",
     value: 0}]')" > /dev/null

port="${FORWARD_PORT}"
for ns in "${NAMESPACES[@]}"; do
  kubectl -n "${ns}" rollout status deployment/pubgrade-sidecar --timeout=180s > /dev/null
  # Until the pod of a previous rollout is gone, which the port forward and
  # `exec` might otherwise pick
  retry 60 2 "the old sidecar pod in ${ns} to end" one_sidecar_pod "${ns}"
  kubectl -n "${ns}" rollout status deployment/tesk-api --timeout=180s > /dev/null
  kubectl -n "${ns}" port-forward service/pubgrade-sidecar "${port}:8080" > /dev/null &
  FORWARDS+=("$!")
  PORTS[${ns}]="${port}"
  retry 30 1 "the port forward to ${ns}" \
    curl --silent --fail --max-time 5 --output /dev/null \
    "http://127.0.0.1:${port}/ui/"
  port=$((port + 1))
done
# shellcheck disable=SC2016 # expanded in the container
expect "site-b runs as the arbitrary user" "|${ARBITRARY_UID} 0|" \
  "|$(kubectl -n site-b exec deployment/pubgrade-sidecar -- sh -c 'echo "$(id -u) $(id -g)"')|"

for ns in "${NAMESPACES[@]}"; do
  echo "== notifications in ${ns}"
  expect "wrong token rejected" 401 "$(call "${ns}" GET /deployment/tesk-api wrong)"
  expect "status before" '"status":"complete"' "$(report "${ns}")"
  expect "other repository rejected" 409 \
    "$(notify "${ns}" tesk-api other/image "${NEW_TAG}")"
  # Refused by the sidecar itself (not in `targetDeployments`), before the
  # Role would refuse it, whether the Deployment exists (site-a) or not
  # (site-b)
  expect "Deployment outside the targets refused" 403 \
    "$(notify "${ns}" bystander "${TARGET_REPO}" "${NEW_TAG}")"
  # Read one by one, which the Role allows: the targets that exist (tesk-worker
  # only in site-a), without the bystander
  want='["tesk-api"]'
  if [[ "${ns}" == site-a ]]; then
    want='["tesk-api","tesk-worker"]'
  fi
  expect "targets listed" 200 "$(call "${ns}" GET /deployment "${TOKEN}")"
  expect "list of the targets" "|${want}|" \
    "|$(jq --compact-output sort "${WORK}/body" 2> /dev/null \
      || cat "${WORK}/body")|"
  expect "notification accepted" 202 \
    "$(notify "${ns}" tesk-api "${TARGET_REPO}" "${NEW_TAG}")"
done

echo "== check script"
# The check of deployed instances (`tests/check_deployment.sh`), over the port
# forward (HTTP, so its HTTPS checks are skipped): it passes as configured,
# fails for the right reason with a wrong token, a target that isn't one, a
# non-target that is one, an `http://` URL without `CHECK_ALLOW_HTTP=1` or to
# another host, and refuses wrong usage. Its HTTPS checks need a host with a
# certificate, so they don't run here
check="${ROOT}/tests/check_deployment.sh"
url="http://127.0.0.1:${PORTS[site-a]}"
allowed=(env ACCESS_TOKEN="${TOKEN}" CHECK_ALLOW_HTTP=1 "${check}" "${url}")
expect_exit "passes" 0 '^Check passed$' "${allowed[@]}" tesk-api bystander
expect_exit "reports the status" 0 '^ +\{"image":".+","status":".+"\}$' \
  "${allowed[@]}" tesk-api
expect_exit "checks the token" 0 '^ok    token missing \(400\)$' \
  "${allowed[@]}" tesk-api
expect_exit "fails with a wrong token" 1 \
  '^FAIL  Deployments listed: want status 200, got 401' \
  env ACCESS_TOKEN=wrong CHECK_ALLOW_HTTP=1 "${check}" "${url}" tesk-api
expect_exit "fails for a Deployment outside the targets" 1 \
  '^FAIL  target bystander listed: got \[' "${allowed[@]}" bystander tesk-api
expect_exit "fails for a non-target that is a target" 1 \
  '^FAIL  non-target tesk-api refused: want status 403, got 200' \
  "${allowed[@]}" tesk-worker tesk-api
expect_exit "refuses HTTP without CHECK_ALLOW_HTTP" 1 "isn't an https:// URL" \
  env ACCESS_TOKEN="${TOKEN}" "${check}" "${url}" tesk-api
expect_exit "refuses HTTP to another host" 1 "isn't an https:// URL" \
  env ACCESS_TOKEN="${TOKEN}" CHECK_ALLOW_HTTP=1 \
  "${check}" http://sidecar.example.org tesk-api
expect_exit "needs a token" 2 '^ACCESS_TOKEN is not set' \
  env -u ACCESS_TOKEN "${check}" "${url}" tesk-api
expect_exit "needs a single-line token" 2 'must be a single line' \
  env ACCESS_TOKEN=$'a\nb' "${check}" "${url}" tesk-api
expect_exit "needs two or three arguments" 2 '^Usage: ' \
  env ACCESS_TOKEN="${TOKEN}" "${check}" "${url}"
expect_exit "needs Deployment names" 2 '^Not a Deployment name: Tesk_API$' \
  env ACCESS_TOKEN="${TOKEN}" "${check}" "${url}" Tesk_API
expect_exit "needs a non-target other than the target" 2 'must differ' \
  env ACCESS_TOKEN="${TOKEN}" "${check}" "${url}" tesk-api tesk-api
expect_exit "refuses user information in the URL" 2 '^Not a valid base URL' \
  env ACCESS_TOKEN="${TOKEN}" "${check}" https://user@sidecar.example.org \
  tesk-api

for ns in "${NAMESPACES[@]}"; do
  echo "== rollout in ${ns}"
  # The image as the Deployment spells it (exact matches, between `|`)
  want="${TARGET_REPO}:${NEW_TAG}"
  if [[ "${ns}" == site-b ]]; then
    want="docker.io/${want}"
  fi
  out="$(await_report "${ns}" \
    ".status == \"complete\" and .image == \"${want}\"")"
  expect "status complete" '"status":"complete"' "${out}"
  expect "status reports the new image" "|${want}|" \
    "|$(jq --raw-output .image <<< "${out}")|"
  expect "status reports the requested image" "|${want}|" \
    "|$(jq --raw-output .requested_image <<< "${out}")|"
done
# Exact matches, between `|`
expect "site-a keeps the image without prefix" "|${TARGET_REPO}:${NEW_TAG}|" \
  "|$(image_of site-a tesk-api)|"
expect "site-b keeps the Docker Hub prefix" "|docker.io/${TARGET_REPO}:${NEW_TAG}|" \
  "|$(image_of site-b tesk-api)|"
expect "bystander unchanged" "|${TARGET_REPO}:${OLD_TAG}|" \
  "|$(image_of site-a bystander)|"

echo "== image that doesn't exist"
expect "notification accepted" 202 \
  "$(notify site-a tesk-api "${TARGET_REPO}" "${MISSING_TAG}")"
# The sidecar looks the tag up in its registry (Docker Hub) at once, then
# retries every 30 s
sleep 10
expect "status waiting for the image" '"status":"waiting_for_image"' \
  "$(report site-a)"
expect "Deployment unchanged while waiting" "|${TARGET_REPO}:${NEW_TAG}|" \
  "|$(image_of site-a tesk-api)|"
# After the last try, about 90 s in
out="$(await_report site-a '.status == "failed"')"
expect "status failed" '"status":"failed"' "${out}"
# The sidecar's explanation of a missing image
expect "failure explained" \
  '|Image not available, or its registry not reachable.|' \
  "|$(jq --raw-output .detail <<< "${out}")|"
expect "Deployment unchanged after the failure" "|${TARGET_REPO}:${NEW_TAG}|" \
  "|$(image_of site-a tesk-api)|"

echo "== all Deployments of the repository"
expect "repository without Deployments" 404 \
  "$(call site-a PUT /deployment "${TOKEN}" \
    "{\"image_name\": \"other/image\", \"tag\": \"${NEW_TAG}\"}")"
expect "notification accepted" 202 \
  "$(call site-a PUT /deployment "${TOKEN}" \
    "{\"image_name\": \"${TARGET_REPO}\", \"tag\": \"${NEW_TAG}\"}")"
# tesk-api already has the tag, so only tesk-worker rolls out
expect "Deployments that roll out" '["tesk-worker"]' \
  "$(jq --compact-output '[.deployments[].name]' "${WORK}/body")"
want="${TARGET_REPO}:${NEW_TAG} docker.io/${TARGET_REPO}:${NEW_TAG} ${OTHER_IMAGE}"
if retry 60 3 "the containers of tesk-worker" has_images site-a tesk-worker "${want}"; then
  echo "ok    containers of the repository updated, others unchanged"
else
  echo "FAIL  containers of tesk-worker: want ${want}, got $(images_of site-a tesk-worker)"
  FAILED=1
fi
expect "tesk-api keeps the image it has" "|${TARGET_REPO}:${NEW_TAG}|" \
  "|$(image_of site-a tesk-api)|"
expect "bystander still unchanged" "|${TARGET_REPO}:${OLD_TAG}|" \
  "|$(image_of site-a bystander)|"

if ((FAILED != 0)); then
  echo "Integration test failed"
  exit 1
fi
echo "Integration test passed"
