#!/usr/bin/env bash
# Integration test on a real, throw-away Kubernetes cluster (k3s in Docker):
# builds the image from this checkout, installs the chart into two namespaces,
# sends notifications and checks that the target Deployments roll out to the
# new tag and that the status reports it. The chart's minimal Role applies in
# both namespaces; in the second one, the sidecar also runs as an arbitrary
# user ID with group 0, as on OpenShift, and its target Deployment spells the
# image with the `docker.io/` prefix.
#
# Needs bash 4.4 or newer, Docker (privileged containers), kubectl, Helm, curl
# and jq, and Internet access: the cluster pulls the target images from Docker
# Hub, and the sidecar checks Docker Hub for them. At the end, it removes the
# cluster, with its volumes, and the image it built. kubectl and Helm use a
# kubeconfig of their own, so no other cluster is touched.
#
# Usage: test/integration/k3s.sh
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
  local tries="$1" delay="$2" what="$3"
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

# Calls the sidecar of a namespace; the body goes to `${WORK}/body`, the
# status code to stdout
call() {
  local ns="$1" method="$2" path="$3" token="$4" body="${5:-}"
  local args=(--silent --show-error --output "${WORK}/body"
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
retry 60 2 "the node" kubectl get nodes --no-headers
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

echo "== install the chart"
for ns in "${NAMESPACES[@]}"; do
  helm install sidecar "${ROOT}/deployment" --namespace "${ns}" \
    --set 'targetDeployments[0]=tesk-api' \
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
    curl --silent --fail --output /dev/null "http://127.0.0.1:${port}/ui/"
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
  # Refused by the Role, whether the Deployment exists (site-a) or not (site-b):
  # a wider Role would give 404 or 202
  expect "Deployment outside the Role refused" 403 \
    "$(notify "${ns}" bystander "${TARGET_REPO}" "${NEW_TAG}")"
  expect "notification accepted" 202 \
    "$(notify "${ns}" tesk-api "${TARGET_REPO}" "${NEW_TAG}")"
done

for ns in "${NAMESPACES[@]}"; do
  echo "== rollout in ${ns}"
  out="$(await_report "${ns}" \
    ".status == \"complete\" and (.image | endswith(\":${NEW_TAG}\"))")"
  expect "status complete" '"status":"complete"' "${out}"
  expect "status reports the new image" ":${NEW_TAG}\"" \
    "$(jq --compact-output '{image}' <<< "${out}")"
  expect "status reports the requested image" ":${NEW_TAG}\"" \
    "$(jq --compact-output '{requested_image}' <<< "${out}")"
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
# The sidecar looks the tag up on Docker Hub at once, then retries every 30 s
sleep 10
expect "status waiting for the image" '"status":"waiting_for_image"' \
  "$(report site-a)"
expect "Deployment unchanged while waiting" "|${TARGET_REPO}:${NEW_TAG}|" \
  "|$(image_of site-a tesk-api)|"
# After the last try, about two minutes in
out="$(await_report site-a '.status == "failed"')"
expect "status failed" '"status":"failed"' "${out}"
expect "failure explained" '"detail":"Image' "${out}"
expect "Deployment unchanged after the failure" "|${TARGET_REPO}:${NEW_TAG}|" \
  "|$(image_of site-a tesk-api)|"

if ((FAILED != 0)); then
  echo "Integration test failed"
  exit 1
fi
echo "Integration test passed"
