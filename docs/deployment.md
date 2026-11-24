# Deployment

The chart in
[`deployment/`](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/tree/dev/deployment)
installs the sidecar into the namespace of the Deployments to upgrade, on
Kubernetes and on OpenShift. It needs no fixed user ID, so OpenShift's
default security constraints apply.

1. Create a Secret with a token of its own for this site:

   ```bash
   kubectl -n production create secret generic pubgrade-sidecar \
     --from-literal=access-token="$(openssl rand -hex 32)"
   ```

2. Write the values, e.g., `my-values.yaml`:

   ```yaml
   targetDeployments:
     - tesk-api
   accessToken:
     existingSecret: pubgrade-sidecar
   hostName: sidecar.example.org
   # On OpenShift:
   route:
     enabled: true
   # Elsewhere, instead:
   # ingress:
   #   enabled: true
   #   className: nginx
   #   tlsSecretName: sidecar-tls
   ```

   Without a Route or Ingress, the sidecar is only reachable in the cluster.
   A Route redirects HTTP to HTTPS. With an Ingress, that depends on the
   controller (ingress-nginx does it by default if TLS is set): check it, and
   set `ingress.annotations` if needed, as every call carries the token.

3. Install it:

   ```bash
   helm install pubgrade-sidecar deployment/ -n production -f my-values.yaml
   ```

   The chart deploys the image of its `appVersion` unless `image.tag` is set
   (see [Image](#image)): on `dev` and in a release, the image of that
   release (e.g., `0.2.0`). To follow development instead, set `image.tag` to
   `sha-<commit>` or `dev`.

`PUT /deployment` updates all `targetDeployments` that use the notified image
repository (see [API](api.md)). To limit it to some of them, set
`deploymentLabel` to a label they have, `key=value`, or `key` for any value (a
value without a key, e.g., `=api`, matches no deployment); `GET` and
`PUT /deployment/{name}` aren't limited by it. Outside the chart, the sidecar
takes these settings from the environment variables `TARGET_DEPLOYMENTS`
(comma-separated names; without it, all deployments in the namespace, which
needs the right to list them) and `DEPLOYMENT_LABEL`. `GET /deployment` lists
the same deployments as `PUT /deployment` updates, and the calls for one
deployment (`GET` and `PUT /deployment/{name}`) answer 403 for a deployment
that `TARGET_DEPLOYMENTS` doesn't name. The deployments of
`TARGET_DEPLOYMENTS` are read one after the other (each within 10 seconds),
so with many of them, or a slow Kubernetes API, `GET /deployment` and
`PUT /deployment` take longer than a caller such as Pubgrade (10 seconds by
default) may wait: keep the list to what the sidecar has to update.

The sidecar gets `get` and `patch` on the `targetDeployments` and nothing else.
Kubernetes can't limit `patch` to one field, so these rights cover the whole of
each listed Deployment, although the sidecar's API only changes the image tag.
List only what the sidecar has to update, and add an admission policy in your
cluster if you need a stricter limit.
All values are described in `deployment/values.yaml`.

To change the token, update the Secret and restart the sidecar, which reads the
token at start:

```bash
kubectl -n production rollout restart deployment/pubgrade-sidecar
```

## Rollouts and Helm

A rollout changes only the image of the target Deployment's containers
(`spec.template.spec.containers[*].image`), in the cluster. It doesn't change
the values or the manifest of the Helm release that installed the target, nor
the files in Git that a GitOps tool deploys from. These keep the image they
pin, and the next time they are applied, the Deployment goes back to it:

- **`helm upgrade` of the target's release**, e.g., to change a setting:
  Helm (3) patches the live Deployment towards the new manifest, which has
  the release's image, so the Deployment rolls back to the older image, even
  if the image value didn't change between the releases. `--reuse-values`
  doesn't help, as the release's values never had the new tag; `helm
  rollback` restores an old release's image in the same way.
- **GitOps tools** (Argo CD, Flux): the rollout is drift from Git. A tool
  that corrects drift (Argo CD's self-heal, Flux's drift detection) undoes it
  right away; otherwise, the next sync or upgrade of the release does.

The sidecar doesn't stop any of this: `refuseDowngrades` only applies to its
own notifications. To keep a rollout, choose one of these:

- **Pass the live image to `helm upgrade`.** Once the rollout is complete,
  read the images of the containers that the sidecar updates (every
  container of the notified image repository) and set their tags in the
  chart's image values (often `image.tag`; see the target chart's values),
  e.g., for a target `tesk-api`:

  ```bash
  kubectl -n production rollout status deployment/tesk-api
  kubectl -n production get deployment tesk-api -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"\t"}{.image}{"\n"}{end}'
  # e.g., tesk-api    elixircloud/tesk-api:1.2.0
  helm upgrade tesk-api <chart> -n production -f values.yaml \
    --set image.tag=1.2.0
  ```

  These commands read the Deployment's template, i.e., the image that it
  should run; `rollout status` waits until its pods run it, and fails if
  they don't. Don't pin the tag of a rollout that failed (`"status":
  "failed"` from `GET /deployment/{name}`, see [API](api.md), whose `image`
  is that of the first container only). A delivery that the sidecar rolls
  out between reading the tag and `helm upgrade` is undone by the upgrade,
  so upgrade when no notification is expected, or read the image again
  afterwards.
- **Update the values file after each rollout** that completed, so that it
  pins the tag that the sidecar rolled out, and upgrade from that file only.
- **With a GitOps tool**, either make it ignore the image of every container
  that the sidecar updates, e.g., Argo CD's `ignoreDifferences` on
  `/spec/template/spec/containers/0/image` for the first container (one
  entry per container) together with the sync option
  `RespectIgnoreDifferences=true` (without it, a sync still applies the
  image from Git), or let the tool's own image updater (Argo CD Image
  Updater, Flux's image automation) follow the registry and write new tags
  to Git, instead of the sidecar.

Labels and annotations of the target stay as the chart set them: in
particular, `app.kubernetes.io/version` (and `helm.sh/chart`) keep the
versions that the chart set (usually its `appVersion`) after a rollout, so
they don't say which image runs. Read the image from the Deployment, as
above, instead.

## Checking a deployed sidecar

[`tests/check_deployment.sh`](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/blob/dev/tests/check_deployment.sh)
checks a sidecar from outside, over its Route or Ingress, after installing or
upgrading it, or after changing the token. It only reads: it sends no
notification, so nothing is rolled out. It needs bash, curl and jq, and checks
that:

- the URL is `https://` (otherwise it stops before sending the token), the
  certificate is valid, and plain HTTP on port 80 of the same host doesn't
  serve the API (skipped if the URL has a port). It passes on no answer
  (connection refused or timed out; other errors, e.g., of name resolution,
  fail), a redirect to HTTPS, or an error that the API doesn't give a call
  without a token (anything but 400 and 401, e.g., 404 from a default
  backend); 2xx and other redirects fail;
- calls without a token get 400, and calls with a wrong one 401;
- `GET /deployment` lists the target Deployment (with `deploymentLabel`, it
  must have that label), and `GET /deployment/{name}` reports its status
  (printed);
- a Deployment that isn't in `targetDeployments` gets 403 (by default
  `pubgrade-check-not-a-target`, which needn't exist; a third argument names
  another one).

The token comes from the environment variable `ACCESS_TOKEN` and goes to curl
on its standard input, never on the command line. For example, with the token
from the Secret of step 1:

```bash
ACCESS_TOKEN="$(kubectl -n production get secret pubgrade-sidecar \
  -o jsonpath='{.data.access-token}' | base64 -d)" \
  tests/check_deployment.sh https://sidecar.example.org tesk-api
```

It prints one line per check (`ok`, `FAIL` or `skip`) and exits with 0 if all
passed, 1 if any failed, and 2 for wrong usage; a URL that isn't `https://`
fails (1) before any call. Calls over HTTPS use the proxy of the environment
(`https_proxy`), and the check of plain HTTP that of `http_proxy`: behind a
proxy, that check sees the proxy's answer. `CA_CERT` names a CA certificate
to verify the server with, e.g., for a private CA. `CHECK_ALLOW_HTTP=1`
allows an `http://` URL to `localhost`, `127.0.0.1` or `[::1]` (e.g., a
`kubectl port-forward`), never through a proxy, and skips the HTTPS checks;
the integration test uses it.

## Security contexts

The security contexts of the pod and the container are the values
`podSecurityContext` and `securityContext`, in Helm's usual form. By default,
the pod runs as non-root with the container runtime's seccomp profile
(`RuntimeDefault`), and the container without privilege escalation or
capabilities. Values you set are merged with these defaults; set a key to
`null` to remove it.

Some clusters set their own seccomp profile and reject pods that name one,
e.g., CSC Rahti (OpenShift) since November 2026. There, remove the profile in
the values:

```yaml
podSecurityContext:
  seccompProfile: null
```

or with `--set podSecurityContext.seccompProfile=null`. An empty map
(`seccompProfile: {}`) doesn't remove the profile, as Helm merges it with the
default; `seccompProfile.type: null` does.

Up to chart 0.2.0, the security contexts were fixed in the template. When
upgrading a release installed with such a chart, don't use
`helm upgrade --reuse-values`: it takes the old release's values instead of
the new chart's defaults, which have no security contexts, so the pod would
run without them. Pass your values again (`-f`), or use
`--reset-then-reuse-values` (Helm 3.14 or later).

## Registries

Before a rollout, the sidecar checks that the image is in its registry, with
the OCI distribution API: Docker Hub for repositories without a registry host,
otherwise the registry named in the repository (e.g., a Harbor project,
`harbor.example.org/project/tesk-api`). It asks anonymously over HTTPS, which
suffices for public images. For private images, create a Secret with
credentials that may pull them (e.g., a Harbor robot account), and name it in
`registryCredentials.existingSecret`:

```bash
kubectl -n production create secret docker-registry registry-credentials \
  --docker-server=harbor.example.org \
  --docker-username='robot$sidecar' --docker-password=<secret>
```

The chart mounts its key `.dockerconfigjson` (set another one with
`registryCredentials.key`, e.g., for a Docker `config.json`) and passes the
path in `REGISTRY_AUTH_FILE`. Only its `auths` entries are read, with `auth` or
`username` and `password` (not credential helpers or identity tokens). If the
registry names a token service, as Docker Hub and Harbor do, the credentials go
to that service, as with Docker's own client, but only over HTTPS (or to an
insecure registry itself); token services over HTTP aren't used otherwise, and
neither are challenges after a redirect to another host or from HTTPS to HTTP.
The sidecar only uses the credentials to check images; the cluster pulls images with
the Deployments' own `imagePullSecrets`. Registries that serve only HTTP, e.g.,
one in the cluster, go in `insecureRegistries` (`host` or `host:port`;
environment variable `INSECURE_REGISTRIES`, comma-separated); credentials for
them are sent unencrypted.

Docker Hub limits anonymous requests per source IP address (e.g.,
`ratelimit-limit: 100;w=3600`, 100 per hour). In a cluster whose pods share
one egress IP, e.g., CSC Rahti, other tenants can use the limit up, and every
anonymous check then gets 429, also for public images. Authenticated checks
count against the account instead: create a [personal access
token](https://docs.docker.com/security/access-tokens/) with read-only access
to public repositories and put it in `registryCredentials` as above, with
`--docker-server=https://index.docker.io/v1/`.

## Waiting for images

A notification usually arrives right after its image was pushed, so the
sidecar answers it at once (202) and waits for the image in the background:
it checks the registry once, then up to `imageWait.retries` more times (3 by
default), `imageWait.interval` seconds apart (30 by default; whole seconds in
the chart), so for about 90 seconds in all. Outside the chart, the settings
are the environment variables `IMAGE_WAIT_RETRIES` (0 or more) and
`IMAGE_WAIT_INTERVAL` (seconds above 0); an invalid value keeps its default,
with a warning in the log. If the registry answers 429 or 503 with
`Retry-After`, the next check waits that long instead, if it is longer than
the interval; the waits never add up to more than `retries` x `interval`
seconds, and a wait that wouldn't fit ends the delivery at once. With
`retries: 0`, the sidecar checks once and doesn't wait. The chart accepts 0 to
100 retries and intervals of 1 to 3600 seconds. Each waiting delivery holds
one of the sidecar's 16 workers, so keep the wait short.

Each failed check is logged at the level WARNING, with the reason: the
registry's status code with a short explanation and its `Retry-After` and
rate-limit headers, or the class of the network error (never tokens,
credentials, URLs or error messages), e.g.:

```text
Check 1 of 4 for image elixircloud/tesk-api:2.0.0 failed: registry answered 429 (rate limit reached; ratelimit-remaining: 0;w=3600)
```

If the image is still missing after the last check, the delivery fails, and
`GET /deployment/{name}` reports the reason of the last check in `detail`,
e.g., "Image not available, or its registry not reachable: registry answered
404 (image or tag not found)." The deployment stays as it was.

### Troubleshooting "Image not available"

| Reason | Likely cause | What to do |
| --- | --- | --- |
| `registry answered 404 (image or tag not found)` | The tag isn't (yet) in the registry, or the image name is wrong | Check the tag in the registry; push before notifying; a longer wait if pushes take longer |
| `registry answered 401 (authentication failed)` | A private image without credentials, or credentials that the registry or its token service refused | Check `registryCredentials` (see [Registries](#registries)) |
| `registry answered 429 (rate limit reached; ...)` | The registry's rate limit, e.g., Docker Hub's for anonymous requests from a shared egress IP | Set `registryCredentials`; with `ratelimit-remaining: 0`, waiting longer rarely helps |
| `registry or token service not reachable (ConnectionError)`, `(Timeout)`, `(SSLError)` | DNS, egress rules or a proxy between the cluster and the registry, or TLS interception | Check egress from the sidecar's namespace, e.g., with `curl -I https://registry-1.docker.io/v2/` in a pod there |
| `token service answered unusably (JSONDecodeError)` | The registry's token service answered something other than a token | Check the registry; often a proxy's error page |

## Refusing downgrades

By default, the sidecar rolls out any tag that a notification names, as long
as the registry has it. Whoever holds the sidecar's access token, or makes
Pubgrade send a notification (with a repository's token, or with access to
Pubgrade's database, which stores the sidecars' tokens), can therefore roll
the deployments back to any older release that is still in the registry, with
its known vulnerabilities. Build types don't prevent it: the publisher chooses
them. Two settings limit what a notification can roll out; both are off by
default, and a notification they refuse changes nothing and gets a 409:

- `refuseDowngrades: true` (environment variable `REFUSE_DOWNGRADES=true`)
  rolls out only upgrades: a notification's tag must be a higher version than
  the current tag of every container it would change. In a `PUT /deployment`
  for all deployments, one container that it wouldn't upgrade when the
  notification arrives refuses the whole notification, so no deployment rolls
  out; a deployment that changes later, while the deliveries wait, fails only
  its own delivery (see below), and the others still get their upgrades.
    - **Comparison:** tags are compared as versions by
      [PEP 440](https://peps.python.org/pep-0440/), which also reads SemVer
      pre-releases (`1.2.0-rc.1` as `1.2.0rc1`) and allows a leading `v`:
      `1.2.0rc1` is lower than `1.2.0`, and `1.10.0` higher than `1.9.0`.
      Both tags must have the same number of release segments: `1.2.0` to
      `1.3.0`, or `1.2` to `1.3`, but not `1.2` to `1.2.0` (a tag like `1.2`
      often moves along its patch releases, so `1.2.0` may be older) or
      `1.2.0` to `20230101` (a build number or date, or a commit hash of
      digits, reads as a very high version). The same version spelled
      otherwise (`v1.2.0` after `1.2.0`) isn't an upgrade either.
    - **Refused tags:** tags that aren't versions (e.g., `latest`, `dev`,
      `sha-<commit>` or `1.2.0-alpine`), post-releases written otherwise than
      `.postN` (e.g., `1.2.0-1`, `1.2.0-r1` or `1.2.0_r1`, which SemVer reads
      as pre-releases, or which name rebuilds rather than later releases,
      while PEP 440 reads them as later releases), and notifications for
      containers whose
      current tag isn't a version, or that have no tag (only a digest, or
      none, which means `latest`). So pin the target containers to version
      tags before turning it on. A digest next to the tag
      (`app:1.2.0@sha256:...`) doesn't count; the tag does.
    - **Waiting deliveries:** the sidecar compares with the deployment as it
      is, both when the notification arrives and again right before its
      patch: a deployment that got a higher version while the delivery
      waited for its image isn't patched back, and the delivery fails (see
      [API](api.md)). A notification also can't undercut a delivery that
      still waits for its image: unless it names the same tag or a higher
      version, it is refused, so that, e.g., a notification of the current
      tag doesn't cancel a waiting release. In turn, a waiting higher tag,
      even one that never appears in the registry, refuses lower
      notifications until it fails (after the [wait for its
      image](#waiting-for-images), by default about 90 s); `allowedTags`
      doesn't change that.
    - **Values:** `true`, `1`, `yes` and `on` turn it on, and `false`, `0`,
      `no`, `off` and an empty value leave it off (in any case); any other
      value turns it on too, so that a typo doesn't allow downgrades, and
      the sidecar logs an error (with each notification).
- `allowedTags` (environment variable `ALLOWED_TAGS`) is a regular expression
  (Python syntax) that the whole tag must match, e.g., stable releases only:

  ```yaml
  refuseDowngrades: true
  allowedTags: '[0-9]+\.[0-9]+\.[0-9]+'
  ```

  Other tags are refused before the sidecar reads any deployment. A pattern
  that isn't a regular expression refuses every notification (with a 500),
  and the sidecar logs an error. The pattern is matched against every
  notification's tag (at most 128 characters), without a time limit: keep it
  simple, without nested repetitions such as `([0-9]+)+`, which can take very
  long on some tags.

Turn on both: `refuseDowngrades` alone accepts any tag that reads as a
higher version with as many segments, e.g., an old image tagged `2023.01.0`
after `2.5.0`, or a commit hash that reads as a version (`1234567`, or
`12345a6`, a pre-release of 12345, after another one), so name the tags of
your releases in `allowedTags`. Together,
they don't stop a holder of a token from rolling out a higher release that
is in the registry; they keep old, still published images, and tags that
move (such as `latest`), out of the deployments.

## Resources

The chart sets the CPU and memory requests and limits of the container
explicitly (`resources` in `deployment/values.yaml`: CPU 50m request and 250m
limit, memory 128Mi and 256Mi), so that the default limits of a LimitRange in
the namespace don't apply. Earlier versions of the chart set no CPU limit; the
250m limit now applies on every cluster. Before upgrading, check your values:
if they raise `requests.cpu` above 250m without setting `limits.cpu`, set a
limit too, or Kubernetes refuses the new pod. Clusters with a LimitRange often cap
the ratio of limit to request (`maxLimitRequestRatio`, e.g., 5 for CPU) and
refuse pods above it at admission; the defaults keep the CPU ratio at 5. If
you change `resources`, set requests and limits together (a request above the
default limit needs a higher limit too), within the ratio and the
minimum and maximum of your LimitRange (`kubectl -n production get limitrange
-o yaml` shows them), e.g.:

```yaml
resources:
  requests:
    cpu: 100m
  limits:
    cpu: 500m
```

Values you set are merged with the defaults; set a key to `null` to remove it.

## Changed image tags (November 2026)

Until the release workflow was added (November 2026), every push to `dev`
moved the image tag `latest` (and a dated tag, `YYYYMMDD`), and the chart
deployed `latest` by default. Now pushes to `dev` publish `dev` and
`sha-<commit>`, `latest` moves only with releases, to the highest stable
release, and dated tags are no longer published (existing ones stay as they
are). The chart deploys the image of its `appVersion` unless `image.tag` is
set. Since the first release, 0.2.0, `latest` is the image of the highest
stable release. If you pulled `latest` (or deployed the chart with its
default tag) to follow development, switch to `sha-<commit>` (or `dev`), or
pin a release.

## Upgrading to version 4.1.0 of the API

Version 4.1.0 of the API adds the chart values `refuseDowngrades` and
`allowedTags` (environment variables `REFUSE_DOWNGRADES` and `ALLOWED_TAGS`,
see [Refusing downgrades](#refusing-downgrades)), under which `PUT` calls
answer 409 for notifications that they refuse. Both are off by default, so
nothing changes until you set them.

## Upgrading to version 4.0.0 of the API

Version 4.0.0 of the API removes `DELETE /deployment/{deployment-name}`,
which deleted the pod of that name (not a deployment) and which Pubgrade
never called; it now gets a 405. With the chart, its Role refused the call
already, so deployments with the chart change nothing. If you gave the
sidecar the right to delete pods with a Role of your own for this call,
remove that right. Calls that update deployments (`PUT`) and read them
(`GET`) are unchanged.

## Upgrading to version 3.2.0 of the API

Version 3.2.0 of the API applies `TARGET_DEPLOYMENTS` (the chart's
`targetDeployments`) to `GET /deployment` and to `GET` and
`PUT /deployment/{deployment-name}`, not only to `PUT /deployment`. These
calls change as follows:

- `GET /deployment` lists the deployments that `PUT /deployment` updates:
  those in `TARGET_DEPLOYMENTS` that exist (read one by one, so the chart's
  Role suffices), or all in the namespace without it; with
  `DEPLOYMENT_LABEL`, only those with that label. Before, it listed all
  deployments in the namespace, which the chart's Role refused.
- `GET` and `PUT /deployment/{deployment-name}` answer 403 for a deployment
  that `TARGET_DEPLOYMENTS` doesn't name, without calling the Kubernetes
  API. With the chart, its Role refused these calls already.
- Notifications whose `image_name` has a tag or a digest (e.g.,
  `org/app:1.0`) get a 400. Before, the sidecar dropped them and answered
  202, and the delivery failed later. A registry's port (e.g.,
  `registry:5000/org/app`) is no tag.
- `registry-1.docker.io/` in `image_name` counts as Docker Hub, as
  `docker.io/` and `index.docker.io/` do.

## Upgrading to version 3.1.0 of the API

Version 3.1.0 of the API adds `PUT /deployment`, for all deployments that use
an image repository (see [API](api.md)), and the chart value
`deploymentLabel`. Existing calls stay, with one change:
`PUT /deployment/{deployment-name}` updates every container of the
deployment whose image repository equals `image_name`. Before, it updated
only the first container, and answered 409 if that one used another
repository; now a 409 means that no container uses it.

Images are no longer limited to Docker Hub: the sidecar checks for them in the
registry named in the repository, with optional credentials and the chart
values `registryCredentials` and `insecureRegistries` (see
[Registries](#registries)). Repositories on other registries, which got a 409
before, are now rolled out.

## Upgrading to version 3.0.0 of the API

Version 3.0.0 of the API (November 2026) runs on FOCA 0.22 and Python 3.13
instead of FOCA 0.7 and Python 3.9. Calls and their successful responses stay
the same, and so do the chart and its values. What changes:

- **Errors are problem details** (`application/problem+json`, see
  [API](api.md)): `title`, `status` (an integer) and `detail` instead of `msg`
  and `status_code` (a string), and for requests that don't match the API's
  schemas, the invalid fields in `errors`. Clients that read the error body
  need to change; Pubgrade only reads the status code.
- Errors of the Kubernetes API other than `403` and `404` are answered with
  `500`, as documented, instead of passing on their status (e.g., `503`).
- Request bodies larger than 64 KiB are rejected with `413`.
- The image serves the app with Gunicorn instead of Flask's development
  server, still on port 8080, with one worker process (keep it at one: the
  sidecar keeps its deliveries in memory). Gunicorn's settings can be changed
  via `GUNICORN_CMD_ARGS`.
- The image runs as user 1000 (group 0), the user of the FOCA image, instead
  of 1005, and its app files are read-only. The chart sets no fixed user, so
  it needs no change; deployments of their own that set `runAsUser` may set
  any user ID.

## Upgrading from chart version 0.1

Uninstall the old release first (`helm uninstall`), then install as above:
Kubernetes doesn't allow changing the role of an existing RoleBinding, so
`helm upgrade` fails. The values changed:

```text
Old                  New
appname              appName
imagename            image.repository, image.tag
serviceaccountname   appName
containerport        service.port
secret               accessToken.existingSecret, a Secret that you create
clusterType          route.enabled or ingress.enabled
nodeport             none
none                 targetDeployments, required
```

## Image

The image is published as
[`elixircloud/pubgrade-sidecar`](https://hub.docker.com/r/elixircloud/pubgrade-sidecar),
with these tags:

```text
Tag              Image                                     Moves
X.Y.Z            release X.Y.Z (also X.Y.ZrcN etc.)        no
latest           highest stable release                    with each release
dev              newest commit on branch dev               with each push to dev
sha-<commit>     that commit on branch dev                 no
```

Releases are signed (Sigstore) and come with build provenance attestations;
each GitHub release also has the chart of that version attached. The chart
deploys the image of its `appVersion`, i.e., of its release, unless you set
`image.tag`. To try unreleased changes, set `image.tag` to `sha-<commit>`
(the only tag of a `dev` image that stays on one commit) or `dev`.

Images from `dev` are published by CI on every push to `dev`, after all checks
have passed, in the environment `dev-images`, which allows only the branch
`dev`. Releases are published from version tags, in the environment
`release` (see
[Versioning and releases](development.md#versioning-and-releases)). Both
need the Docker Hub secrets `DOCKERHUB_LOGIN` and `DOCKERHUB_TOKEN`
(organization secrets).

A fork publishes to its own Docker Hub organization once it sets these
secrets and the Actions variable `DOCKERHUB_ORG` (for the repository or
organization, not the environment); otherwise it skips publishing. To deploy
a fork's image, set `image.repository` to `<organization>/pubgrade-sidecar`.
