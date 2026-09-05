#!/usr/bin/env bash
# Target-lock, mutate, deploy, poll, verify and rollback one Coolify Application.
set -Eeuo pipefail

for required in \
  COOLIFY_URL COOLIFY_API_TOKEN COOLIFY_APPLICATION_UUID COOLIFY_APPLICATION_NAME \
  COOLIFY_PROJECT_UUID COOLIFY_ENVIRONMENT_UUID COOLIFY_DESTINATION_UUID COOLIFY_SERVER_UUID \
  COOLIFY_ENVIRONMENT_NAME COOLIFY_PUBLIC_DOMAIN COOLIFY_GIT_BRANCH COOLIFY_GIT_REPOSITORY \
  COOLIFY_COMPOSE_LOCATION IMAGE_REF IMAGE_REPO RELEASE_SHA HEALTH_URL; do
  [[ -n "${!required:-}" ]] || { printf 'Required deployment variable is missing: %s\n' "${required}" >&2; exit 1; }
done
[[ "${IMAGE_REF}" == "${IMAGE_REPO}@sha256:"* ]] || { echo "IMAGE_REF must be the exact digest for IMAGE_REPO." >&2; exit 1; }
[[ "${IMAGE_REF##*@}" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "IMAGE_REF digest is malformed." >&2; exit 1; }

api_base="${COOLIFY_URL%/}/api/v1"
application_url="${api_base}/applications/${COOLIFY_APPLICATION_UUID}"
envs_url="${application_url}/envs"
curl_args=(--fail-with-body --silent --show-error --retry 5 --retry-all-errors --retry-delay 3 --connect-timeout 15 --max-time 120)
if [[ -n "${COOLIFY_ORIGIN_IP:-}" ]]; then
  coolify_host="${COOLIFY_URL#*://}"; coolify_host="${coolify_host%%/*}"
  curl_args+=(--resolve "${coolify_host}:443:${COOLIFY_ORIGIN_IP}")
fi
auth_args=(--header "Authorization: Bearer ${COOLIFY_API_TOKEN}")
get_application() { curl "${curl_args[@]}" "${auth_args[@]}" --header 'Accept: application/json' "${application_url}"; }
get_envs() { curl "${curl_args[@]}" "${auth_args[@]}" --header 'Accept: application/json' "${envs_url}"; }

assert_target() {
  local application_json="$1"
  jq -e \
    --arg uuid "${COOLIFY_APPLICATION_UUID}" --arg name "${COOLIFY_APPLICATION_NAME}" \
    --arg project "${COOLIFY_PROJECT_UUID}" --arg environment "${COOLIFY_ENVIRONMENT_UUID}" \
    --arg environment_name "${COOLIFY_ENVIRONMENT_NAME}" --arg destination "${COOLIFY_DESTINATION_UUID}" \
    --arg server "${COOLIFY_SERVER_UUID}" --arg domain "${COOLIFY_PUBLIC_DOMAIN}" \
    --arg branch "${COOLIFY_GIT_BRANCH}" --arg repo "${COOLIFY_GIT_REPOSITORY}" \
    --arg compose "${COOLIFY_COMPOSE_LOCATION}" \
    'def decode: if type == "string" and (startswith("{") or startswith("[")) then (try fromjson catch .) else . end;
     (.uuid == $uuid) and (.name == $name)
     and (.git_repository == $repo or .git_repository == ("https://github.com/" + $repo + ".git"))
     and (.git_branch == $branch)
     and ((.project_uuid // .environment.project.uuid // "") == $project)
     and ((.environment_uuid // .environment.uuid // "") == $environment)
     and ((.environment.name // "") == $environment_name)
     and ((.destination_uuid // .destination.uuid // "") == $destination)
     and ((.server_uuid // .destination.server.uuid // .server.uuid // "") == $server)
     and (((.docker_compose_domains // .fqdn // "") | decode | tostring | contains($domain)))
     and ((.docker_compose_location // "") == $compose)' <<<"${application_json}" >/dev/null
}

application_json="$(get_application)"
assert_target "${application_json}" || { echo "Coolify Application target lock failed; refusing mutation." >&2; exit 1; }

envs_json="$(get_envs)"
previous_image="$(jq -r 'map(select(.key == "RELEASE_IMAGE" and .is_preview == false)) | .[0] | (.value // .real_value // empty)' <<<"${envs_json}")"
previous_release_sha="$(jq -r 'map(select(.key == "RELEASE_SHA" and .is_preview == false)) | .[0] | (.value // .real_value // empty)' <<<"${envs_json}")"
if [[ -z "${previous_image}" || "${previous_image}" == "null" ]]; then
  if [[ "${ALLOW_EMPTY_PREVIOUS_IMAGE:-false}" != true ]]; then echo "No previous immutable image exists; rollback is not safe." >&2; exit 1; fi
  previous_image=""
fi
if [[ -n "${previous_image}" && ! "${previous_image}" =~ ^ghcr.io/v-viebal/.+@sha256:[0-9a-f]{64}$ ]]; then echo "Previous image is not an approved immutable GHCR reference." >&2; exit 1; fi
printf 'previous_image=%s\n' "${previous_image}" >>"${GITHUB_OUTPUT:-/dev/null}"

set_env_key() {
  local key="$1" value="$2" current uuid payload
  current="$(get_envs)"
  # Coolify mirrors a POSTed non-preview row into a preview row. Remove both
  # scopes before writing the canonical value or repeated deployments will
  # accumulate stale release metadata and make readback ambiguous.
  mapfile -t uuids < <(jq -r --arg key "${key}" '.[] | select(.key == $key) | (.uuid // empty)' <<<"${current}")
  for uuid in "${uuids[@]}"; do
    curl "${curl_args[@]}" "${auth_args[@]}" --request DELETE "${envs_url}/${uuid}" >/dev/null
  done
  payload="$(jq -cn --arg key "${key}" --arg value "${value}" '{key:$key,value:$value,is_preview:false,is_literal:true,is_runtime:true,is_buildtime:true}')"
  curl "${curl_args[@]}" "${auth_args[@]}" --request POST --header 'Content-Type: application/json' --data "${payload}" "${envs_url}" >/dev/null
  jq -e --arg key "${key}" --arg value "${value}" '
    any(.[]; .key == $key and .is_preview == false and ((.value // .real_value // "") == $value))
    and all(.[]; select(.key == $key) | ((.value // .real_value // "") == $value))
  ' <<<"$(get_envs)" >/dev/null
}

set_image() {
  set_env_key RELEASE_IMAGE "$1"
}

set_release_sha() {
  set_env_key RELEASE_SHA "$1"
}


wait_for_deployment() {
  local deployment_uuid="$1" label="$2" deployment state
  for attempt in $(seq 1 240); do
    deployment="$(curl "${curl_args[@]}" "${auth_args[@]}" --header 'Accept: application/json' "${api_base}/deployments/${deployment_uuid}")"
    state="$(jq -r '.status // .deployment_status // .state // "unknown"' <<<"${deployment}" | tr '[:upper:]' '[:lower:]')"
    case "${state}" in
      finished|success|succeeded|completed|successful) echo "${label} deployment reached terminal success: ${state}"; return 0;;
      failed|error|cancelled|canceled|aborted|timeout|timed_out) echo "${label} deployment failed: ${state}" >&2; return 1;;
    esac
    [[ "${attempt}" -lt 240 ]] || { echo "${label} deployment polling timed out." >&2; return 1; }
    sleep 5
  done
}

queue_deployment() {
  local response deployment_uuid
  response="$(curl "${curl_args[@]}" "${auth_args[@]}" --request POST --header 'Accept: application/json' --header 'Content-Type: application/json' --data "$(jq -cn --arg uuid "${COOLIFY_APPLICATION_UUID}" '{uuid:$uuid}')" "${api_base}/deploy?force=true")"
  deployment_uuid="$(jq -r '.deployment_uuid // .deployments[0].deployment_uuid // .deployments[0].uuid // .uuid // empty' <<<"${response}")"
  : "${deployment_uuid:?Coolify did not return a deployment UUID}"
  printf '%s' "${deployment_uuid}"
}

url_with_release_query() { local url="$1" sep='?'; [[ "${url}" == *\?* ]] && sep='&'; printf '%s%s__release=%s' "${url}" "${sep}" "${RELEASE_SHA:0:12}"; }
verify_health() {
  local health_body marker_body health_url marker_url
  health_url="$(url_with_release_query "${HEALTH_URL}")"
  for attempt in $(seq 1 60); do
    health_body="$(mktemp)"
    if curl --fail --silent --show-error --location --retry 2 --retry-all-errors --connect-timeout 10 --max-time 30 -H 'Cache-Control: no-cache, no-store' -o "${health_body}" "${health_url}"; then
      if [[ -z "${HEALTH_EXPECTED_REGEX:-}" ]] || grep -Eiq -- "${HEALTH_EXPECTED_REGEX}" "${health_body}"; then
        rm -f "${health_body}"
        if [[ -n "${RELEASE_MARKER_URL:-}" ]]; then
          marker_url="$(url_with_release_query "${RELEASE_MARKER_URL}")"; marker_body="$(mktemp)"
          if ! curl --fail --silent --show-error --location --retry 2 --retry-all-errors --connect-timeout 10 --max-time 30 -H 'Cache-Control: no-cache, no-store' -o "${marker_body}" "${marker_url}" || ! grep -Fq -- "${RELEASE_SHA}" "${marker_body}"; then rm -f "${marker_body}"; [[ "${attempt}" -lt 60 ]] || return 1; sleep 5; continue; fi
          rm -f "${marker_body}"
        fi
        return 0
      fi
    fi
    rm -f "${health_body}"
    [[ "${attempt}" -lt 60 ]] || return 1
    sleep 5
  done
}

credentials_staged=false
image_mutated=false
deployment_succeeded=false
rollback() {
  [[ -n "${previous_image}" ]] || { echo "No previous image is available for rollback." >&2; return 1; }
  echo "Restoring previous immutable image."
  set_image "${previous_image}"
  if [[ -n "${previous_release_sha}" ]]; then set_release_sha "${previous_release_sha}"; fi
  local rollback_uuid="$(queue_deployment)"
  printf 'rollback_deployment_uuid=%s\n' "${rollback_uuid}" >>"${GITHUB_OUTPUT:-/dev/null}"
  wait_for_deployment "${rollback_uuid}" Rollback
  local saved_marker="${RELEASE_MARKER_URL:-}"; RELEASE_MARKER_URL=""; verify_health; RELEASE_MARKER_URL="${saved_marker}"
}
cleanup() {
  local rc="$?"; trap - EXIT
  if [[ "${rc}" -ne 0 && "${image_mutated}" == true && "${deployment_succeeded}" == false ]]; then rollback || echo "Rollback did not converge; manual intervention is required." >&2; fi
  if [[ "${credentials_staged}" == true ]]; then bash scripts/coolify_registry_credentials.sh clear || echo "Fast-path credential cleanup failed; independent cleanup job must recover it." >&2; fi
  exit "${rc}"
}
trap cleanup EXIT

bash scripts/coolify_registry_credentials.sh stage
credentials_staged=true
configuration_payload="$(jq -cn --arg branch "${COOLIFY_GIT_BRANCH}" --arg revision "${RELEASE_SHA}" --arg compose "${COOLIFY_COMPOSE_LOCATION}" --arg port "${COOLIFY_PORTS_EXPOSES:-}" '{git_branch:$branch,git_commit_sha:$revision,build_pack:"dockercompose",docker_compose_location:$compose,docker_compose_custom_start_command:"bash scripts/coolify_ghcr_start.sh",pre_deployment_command:null} + (if $port != "" then {ports_exposes:$port} else {} end)')"
curl "${curl_args[@]}" "${auth_args[@]}" --request PATCH --header 'Content-Type: application/json' --data "${configuration_payload}" "${application_url}" >/dev/null
application_json="$(get_application)"; assert_target "${application_json}"
jq -e --arg revision "${RELEASE_SHA}" --arg compose "${COOLIFY_COMPOSE_LOCATION}" '(.git_commit_sha == $revision) and (.docker_compose_custom_start_command == "bash scripts/coolify_ghcr_start.sh") and (.docker_compose_location == $compose)' <<<"${application_json}" >/dev/null
image_mutated=true
set_image "${IMAGE_REF}"
set_release_sha "${RELEASE_SHA}"
deployment_uuid="$(queue_deployment)"
printf 'deployment_uuid=%s\n' "${deployment_uuid}" >>"${GITHUB_OUTPUT:-/dev/null}"
wait_for_deployment "${deployment_uuid}" Release
verify_health || { echo "Public/application health or release verification failed." >&2; exit 1; }
deployment_succeeded=true
echo "Exact-digest Coolify release verified."
