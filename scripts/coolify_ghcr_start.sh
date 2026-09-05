#!/usr/bin/env bash
# Coolify custom start: run one exact private GHCR digest, verify it, remove
# pull credentials from the deployment artifact, then run Compose without build.
#
# There are two supported entry paths:
#   1. GitHub Actions stages short-lived GHCR credentials and publishes a new
#      digest before triggering Coolify.
#   2. A direct Coolify redeploy reuses the already-approved digest while it is
#      still present in the Docker cache (or uses a Coolify-managed Docker
#      credential if one is configured on the target server).
#
# Direct Coolify deploys must never silently fall back to a mutable tag or to a
# host-side application build. If the approved image is neither cached nor
# pullable with an explicitly supplied/managed credential, fail closed.
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

image_is_approved_locally() {
  docker image inspect "${image_ref}" --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null |
    grep -Fx -- "${image_ref}" >/dev/null
}

docker_config=""
pull_mode="cached"
if image_is_approved_locally; then
  echo "Using the cached approved image ${image_ref}."
else
  if [[ -n "${registry_username}" || -n "${registry_token}" ]]; then
    : "${registry_username:?Temporary GHCR username is missing}"
    : "${registry_token:?Temporary GHCR token is missing}"
    docker_config="$(mktemp -d)"
    pull_mode="short-lived-credential"
    printf '%s' "${registry_token}" |
      docker --config "${docker_config}" login ghcr.io --username "${registry_username}" --password-stdin >/dev/null
    docker --config "${docker_config}" pull "${image_ref}" >/dev/null
  else
    # A Coolify-managed registry login is exposed to the helper container only
    # when the target server has a Docker config. This keeps direct redeploys
    # credential-free when the release is cached, while still supporting an
    # explicitly managed pull credential.
    pull_mode="coolify-managed-credential"
    if ! docker pull "${image_ref}" >/dev/null; then
      echo "Approved image is not cached and no usable GHCR pull credential is available. Run the GitHub Actions release path first or configure a Coolify-managed read-only registry credential." >&2
      exit 1
    fi
  fi
  image_is_approved_locally || { echo "Pulled image RepoDigests do not contain the requested release." >&2; exit 1; }
fi

cleanup() {
  local rc="$?"
  trap - EXIT
  if [[ -n "${docker_config}" ]]; then
    docker --config "${docker_config}" logout ghcr.io >/dev/null 2>&1 || true
    rm -rf "${docker_config}"
  fi
  exit "${rc}"
}
trap cleanup EXIT
echo "Release image verification mode: ${pull_mode}."

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
