# NAS deployment preparation: `1afb410`

This is a preparation plan only. It must not be executed on the home NAS until the
independent review passes and the existing Stage 4 authorization gates are met.

## Anchors

- Candidate branch / PR: `agent/gpt/273fba8c1bba`, PR #3.
- Candidate HEAD: `1afb410b` (candidate image: `sha256:d326f9e099be02dfbd134e7f028a11d2ba1cd4497418c5aaf1cfcd26dae0c59b`).
- Production target directory: `/vol1/1000/handle-pri/media-browser`.
- Current production image: `sha256:5ec1127b96bae04c723dc1160675aff723070b536108e65d6001a79b7cbb0c49`.
- Existing rollback image tag: `media-browser:rollback-20260920T001825Z`.
- Existing backup: `/home/yangchenghao/media-browser-deploy-backups/20260920T001825Z/`.
- The failed `71bd9f4` candidate remains only as an unused image tag
  `media-browser:71bd9f4`; the production container is on the original image.

## Restored production state checked on 2026-09-23

These production source-file SHA-256 values match
`source-sha256-before.txt` in the backup directory:

```text
3000ebda133e39c1cadb8b07e79c2c1b559dd7e3b8b8d5696e89b2bce56bd5d5  media_browser.py
05ec412534200dc32fbaa5b367be3fdf900344522b114bbc8e6bb83c9cfa0b77  templates/index.html
31bf317180fadb3a66778d04d6316a214e2e302d688b73371b087218ca84f874  Dockerfile
b03e5a103436ef1812c25d9ad468b6d0f7b9cc4ab0042a3fb561430710c63b0b  docker-compose.yml
01a55a4d4feaab031468232b4e03540ed05b0ea713e4d4fe60ebb6ec5dc0af2e  on_demand.py
73372ee8c8a0a211d3f178f9209931684336d9f49fa7031c5fcede5d85ce8dad  cache_manager.py
```

The production container is `media-browser`, uses `media-browser:local`
(the original image above), is `running`/`healthy`, and its internal
`/health` probe returned `200` with `ok: true`, version `2.5.3`.

## Deployment rules

1. Do **not** copy this branch's `docker-compose.yml` over production. The
   production compose file has the existing two read-only media mounts and
   original account/password variables. Keeping it unchanged prevents both a
   mount-point regression and credential changes.
2. Do not run database/config migrations; this application uses no destructive
   migration. The candidate only changes application/image behavior.
3. Do not change file ownership, media permissions, credentials, or real media.
4. Build from a temporary Git checkout, not the production directory.

## Candidate checks completed locally

- `python -m pytest -q`: exit 0, `143 passed, 3 skipped`.
- Full-repo auth caller search: only `Handler._is_authorized`,
  `Handler._require_authorized`, and their tests gate requests; browser calls
  are same-origin and automatically supply HTTP Basic credentials.
- Docker build of `1afb410`: exit 0.
- E2E, real container image: missing/wrong credentials returned `401` with
  `WWW-Authenticate: Basic`; original-style `MB_AUTH_USER` +
  `MB_AUTH_PASSWORD` returned `200`; desktop and iPhone UA both rendered the
  homepage; controlled delete -> recycle -> restore preserved SHA-256.
- Read-only mount + `MB_MEDIA_READONLY=1`: authenticated delete returned `403`
  `MEDIA_READONLY`, and the controlled file remained byte-identical.

## Executable deployment sequence

Run on the NAS, with `${REPO_URL}` set to the GitHub HTTPS URL:

```bash
set -euo pipefail
prod=/vol1/1000/handle-pri/media-browser
tmp=$(mktemp -d /home/yangchenghao/media-browser-candidate-1afb410.XXXXXX)
git clone "${REPO_URL}" "${tmp}"
git -C "${tmp}" checkout 1afb41039d4897108be6fa3cc802fad3b208f5b1
docker build --no-cache -t media-browser:1afb410 "${tmp}"
test "$(docker image inspect media-browser:1afb410 --format '{{.Id}}')" = "sha256:d326f9e099be02dfbd134e7f028a11d2ba1cd4497418c5aaf1cfcd26dae0c59b"
test "$(docker image inspect media-browser:local --format '{{.Id}}')" = "sha256:5ec1127b96bae04c723dc1160675aff723070b536108e65d6001a79b7cbb0c49"
```

Before `up`, confirm without printing values that the existing container has
non-empty `MB_AUTH_USER` and `MB_AUTH_PASSWORD` and empty `MB_ACCESS_TOKEN`.
Then switch only the image reference and recreate the service:

```bash
set -euo pipefail
prod=/vol1/1000/handle-pri/media-browser
stamp=$(date -u +%Y%m%dT%H%M%SZ)
docker image tag media-browser:local "media-browser:rollback-before-1afb410-${stamp}" && \
docker image tag media-browser:1afb410 media-browser:local && \
docker compose -f "${prod}/docker-compose.yml" --project-directory "${prod}" up -d --no-build --force-recreate media-browser
```

Post-deploy checks, in this order:

```bash
prod=/vol1/1000/handle-pri/media-browser
test "$(docker image inspect media-browser:local --format '{{.Id}}')" = "sha256:d326f9e099be02dfbd134e7f028a11d2ba1cd4497418c5aaf1cfcd26dae0c59b"
test "$(docker inspect media-browser --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')" = "running/healthy"
docker exec media-browser python -u -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=5); print(r.status, r.read().decode())"
```

If any post-deploy check fails, stop and execute the rollback below. Do not
edit credentials, mounts, permissions, or production files to work around it.

## Single-line emergency rollback

```bash
cd /vol1/1000/handle-pri/media-browser && docker image tag media-browser:rollback-20260920T001825Z media-browser:local && docker compose up -d --no-build --force-recreate media-browser
```

Then verify the image ID returns to
`sha256:5ec1127b96bae04c723dc1160675aff723070b536108e65d6001a79b7cbb0c49`
and the container is healthy.

## Full deployment-file restore

Only if production files—not just the running image—were modified, restore the
existing archive and verify the six hashes above:

```bash
prod=/vol1/1000/handle-pri/media-browser
backup=/home/yangchenghao/media-browser-deploy-backups/20260920T001825Z
tar -xzf "${backup}/deploy-files.tar.gz" -C "${prod}"
cd "${prod}" && sha256sum media_browser.py templates/index.html Dockerfile docker-compose.yml on_demand.py cache_manager.py
```

Then use the single-line emergency rollback command. Media libraries and the
cache directory are mounted data and are not contained in or replaced by this
archive.
