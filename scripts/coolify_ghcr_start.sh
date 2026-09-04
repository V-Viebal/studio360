#!/usr/bin/env bash
# Coolify custom start: pull one exact private GHCR digest, verify it, remove
# pull credentials from the deployment artifact, then run Compose without build.
set -Eeuo pipefail

artifact_dir=""
compose_file=""
for candidate in "${PWD}" "${COOLIFY_RESOURCE_UUID:+/data/coolify/applications/${COOLIFY_RESOURCE_UUID}}" "${COOLIFY_RESOURCE_UUID:+/artifacts/${COOLIFY_RESOURCE_UUID}}"; do
  [[ -n "${candidate}" && -f "${candidate}/.env" && -f "${candidate}/compose.yaml" ]] || continue
  artifact_dir="${candidate}"; compose_file="${candidate}/compose.yaml"; break
done
[[ -n "${artifact_dir}" ]] || { echo "Coolify artifact does not contain .env and compose.yaml." >&2; exit 1; }
env_file="${artifact_dir}/.env"

read_artifact_value() {
  local key="$1" ambient="${!1:-}"
  if [[ -n "${ambient}" ]]; then printf '%s' "${ambient}"; return; fi
  awk -F= -v wanted="${key}" '$1 == wanted { value=substr($0,index($0,"=")+1); gsub(/^[[:space:]\047"]+|[[:space:]\047"]+$/, "", value); print value; exit }' "${env_file}"
}

image_ref="$(read_artifact_value RELEASE_IMAGE)"
registry_username="$(read_artifact_value GHCR_USERNAME)"
registry_token="$(read_artifact_value GHCR_TOKEN)"
compose_project="$(read_artifact_value COMPOSE_PROJECT_NAME)"
compose_project="${compose_project:-$(basename "${artifact_dir}")}"

[[ "${image_ref}" == ghcr.io/v-viebal/*@sha256:* ]] || { echo "RELEASE_IMAGE must be an approved exact GHCR digest." >&2; exit 1; }
[[ "${image_ref##*@}" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "RELEASE_IMAGE digest is malformed." >&2; exit 1; }
: "${registry_username:?Temporary GHCR username is missing}"
: "${registry_token:?Temporary GHCR token is missing}"

docker_config="$(mktemp -d)"
cleanup() { docker --config "${docker_config}" logout ghcr.io >/dev/null 2>&1 || true; rm -rf "${docker_config}"; }
trap cleanup EXIT
printf '%s' "${registry_token}" | docker --config "${docker_config}" login ghcr.io --username "${registry_username}" --password-stdin >/dev/null
docker --config "${docker_config}" pull "${image_ref}" >/dev/null
docker image inspect "${image_ref}" --format '{{range .RepoDigests}}{{println .}}{{end}}' | grep -Fx -- "${image_ref}" >/dev/null || { echo "Pulled image RepoDigests do not contain the requested release." >&2; exit 1; }

# Credentials are needed only for the pull and must not enter app containers.
sed -i '/^GHCR_USERNAME=/d;/^GHCR_TOKEN=/d' "${env_file}"
unset GHCR_USERNAME GHCR_TOKEN || true
export RELEASE_IMAGE="${image_ref}"
export RELEASE_PULL_POLICY=never
export COMPOSE_PROJECT_NAME="${compose_project}"

# The Compose contract has no build section. Explicitly reject any accidental build.
if grep -Eq '^[[:space:]]+build:' "${compose_file}"; then echo "Deployment Compose must not build on the target host." >&2; exit 1; fi
docker compose -p "${compose_project}" --project-directory "${artifact_dir}" --env-file "${env_file}" -f "${compose_file}" config -q
docker compose -p "${compose_project}" --project-directory "${artifact_dir}" --env-file "${env_file}" -f "${compose_file}" up -d --force-recreate --remove-orphans
