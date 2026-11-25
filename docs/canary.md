# Canary

The canary is a permanent smoke test of automatic upgrades on the live
deployments. Once a week (and by hand), the workflow `Canary`
([`.github/workflows/canary.yml`](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/blob/dev/.github/workflows/canary.yml))
releases a new version of a tiny image and checks that every site rolled it out
automatically, through Pubgrade and a sidecar, the same path that real
releases take. A failed run means that an upgrade didn't reach a site. GitHub
notifies the person who last changed the schedule (or enabled the workflow)
of failed scheduled runs; others see them if they subscribed to Actions
notifications for the repository, or by the badge in the README.

## What it proves

Each run goes through the whole chain:

1. **Push canary:** builds `elixircloud/pubgrade-canary:1.0.N`, where N is
   the run number times 100 plus the attempt, so that it grows with every
   run, re-runs included,
   from [`canary/Dockerfile`](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/blob/dev/canary/Dockerfile)
   and pushes it to Docker Hub. The image is about 2 MB: BusyBox's `httpd`,
   run as any non-root user on port 8080, which serves `/version.json`, e.g.,
   `{"version": "1.0.42", "commit": "<sha>"}`.
2. **Canary rollout:** registers the image as a build of the repository
   `pubgrade-canary` at Pubgrade, with Pubgrade's
   [notify action](https://github.com/elixir-cloud-aai/Pubgrade/tree/v0.3.0/actions/notify),
   and Pubgrade notifies every subscription of that repository, one per site.
   Right after that, the same job polls `/version.json` of each site until it
   serves the new version (10 minutes at most), and then checks that
   Pubgrade delivered every notification. The job summary lists, per site,
   the gap from the notification to the new version (accurate to the polling
   interval of 10 s), and per subscription Pubgrade's result.

At each site, the canary sidecar checks the tag in the registry, patches the
canary Deployment, and Kubernetes rolls it out. The versions only grow, so
every run is an upgrade to a tag that didn't exist before.

The checks are in
[`scripts/canary.py`](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/blob/dev/scripts/canary.py),
with unit tests against stub sites and a stub Pubgrade
(`tests/unit/test_canary.py`). CI builds the image on every pull request and
checks that it serves its version as an arbitrary user (job "Canary image").

## Configuration

The workflow runs in the GitHub environment `canary`. Restrict its deployment
branches to `dev`, and add no required reviewers, so that the schedule can run.

| Name | Kind | Value |
| --- | --- | --- |
| `PUBGRADE_CANARY_TOKEN` | secret | Access token of the repository `pubgrade-canary` at Pubgrade (`access_token` of `POST /repositories`); it can only publish builds of that repository |
| `PUBGRADE_URL` | variable | Base URL of Pubgrade, e.g., `https://upgrade.elixir-on.cloud` |
| `PUBGRADE_CANARY_REPOSITORY_ID` | variable | Identifier of the repository `pubgrade-canary` at Pubgrade |
| `CANARY_SITES` | variable | JSON list of the sites' base URLs (HTTPS), e.g., `["https://pubgrade-canary-tesk.2.rahtiapp.fi"]` |

The Docker Hub credentials are the org's shared secrets `DOCKERHUB_LOGIN` and
`DOCKERHUB_TOKEN`. While the secret or a variable is unset, a run only pushes
the image and says so in a notice; nothing is notified or checked. Forks skip
the workflow unless they set the variable `DOCKERHUB_ORG` (and push to that
namespace instead).

## Adding a site

A site is a Deployment of the canary image with a sidecar that may only
upgrade it, subscribed at Pubgrade:

1. Deploy the canary: a Deployment `pubgrade-canary` with the current canary
   image, one replica, `RollingUpdate` with `maxUnavailable: 0` and a
   readiness probe on `GET /version.json` (port 8080), so that a broken
   image leaves the old pod serving; a Service and an HTTPS Route or Ingress.
   Small requests suffice (e.g., 10m CPU, 16Mi memory).
2. Install a sidecar of its own with the chart (see
   [Deployment](deployment.md)): `targetDeployments: [pubgrade-canary]`,
   `refuseDowngrades: true`, `allowedTags: 1\.0\.[0-9]+`, its own access token
   and Route, and `registryCredentials` where the cluster shares Docker Hub's
   anonymous rate limit (see [Registries](deployment.md#registries)). A
   sidecar of its own keeps the canary away from the service sidecars and
   their allowed tags, and can be removed without touching them.
3. Subscribe the sidecar to the repository `pubgrade-canary` at Pubgrade
   (type `tag`, value `*`).
4. Add the site's base URL to `CANARY_SITES`, and start the workflow by hand
   (`gh workflow run canary.yml --repo elixir-cloud-aai/Pubgrade-Sidecar`).

## Stopping and removing it

- Stop the test: `gh workflow disable canary.yml --repo elixir-cloud-aai/Pubgrade-Sidecar`.
- Take one site out: remove it from `CANARY_SITES`, then delete its
  subscription at Pubgrade, uninstall its sidecar and delete the canary
  Deployment, Service and Route. Nothing else at the site changes.
- A bad canary image keeps the old pod serving (readiness probe,
  `maxUnavailable: 0`); the next run fixes it, or roll back with
  `kubectl rollout undo deployment/pubgrade-canary`.

## When a run fails

- **"Pubgrade notified no subscription"** or a subscription `failed`: the
  sidecar didn't accept the notification (wrong token, unreachable, or it
  refused the tag); see the HTTP status in the summary and the sidecar's
  logs.
- **"... didn't serve 1.0.N"**: the sidecar accepted the notification, but the
  site didn't roll out. Ask the sidecar for the delivery
  (`GET /deployment/pubgrade-canary`): its `detail` says why, e.g., that the
  image check failed (see [Registries](deployment.md#registries)).
