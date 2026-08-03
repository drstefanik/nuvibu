#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-nuvibu}"
RUN_REGION="${CLOUD_RUN_REGION:-us-central1}"
BUCKET_NAME="${NUVIBU_BUCKET:-}"
VEO_BACKEND="${VEO_BACKEND:-vertex}"
VEO_LOCATION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
REPOSITORY="${ARTIFACT_REPOSITORY:-nuvibu}"
WEB_SERVICE="nuvibu-web"
WORKER_JOB="nuvibu-worker"
EDITORIAL_RELEASE_JOB="nuvibu-wimbledon-release-20260803"
WEB_SA="nuvibu-web@${PROJECT_ID}.iam.gserviceaccount.com"
WORKER_SA="nuvibu-worker@${PROJECT_ID}.iam.gserviceaccount.com"

for command_name in awk curl gcloud git python3 sha256sum; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Missing required command: ${command_name}" >&2
    exit 1
  fi
done

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "Refusing to deploy a dirty worktree." >&2
  echo "Commit the exact Nuvibù source and assets before deploying." >&2
  git status --short >&2
  exit 1
fi

SOURCE_COMMIT="$(git rev-parse HEAD)"
if [[ -n "${IMAGE_TAG:-}" && "${IMAGE_TAG}" != "${SOURCE_COMMIT}" ]]; then
  echo "IMAGE_TAG must equal the full release commit ${SOURCE_COMMIT}" >&2
  exit 1
fi
IMAGE_TAG="${SOURCE_COMMIT}"
IMAGE="${RUN_REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/nuvibu:${IMAGE_TAG}"

case "${VEO_BACKEND}" in
  vertex)
    VEO_MODEL="${VEO_MODEL:-veo-3.1-generate-001}"
    if [[ "${VEO_LOCATION}" != "us-central1" ]]; then
      echo "Vertex model ${VEO_MODEL} is currently deployed in us-central1" >&2
      exit 1
    fi
    ;;
  gemini)
    VEO_MODEL="${VEO_MODEL:-veo-3.1-fast-generate-preview}"
    ;;
  *)
    echo "VEO_BACKEND must be either vertex or gemini" >&2
    exit 1
    ;;
esac

PROJECT_NUMBER="$(
  gcloud projects describe "${PROJECT_ID}" \
    --project "${PROJECT_ID}" \
    --format='value(projectNumber)'
)"
if [[ -z "${BUCKET_NAME}" ]]; then
  BUCKET_NAME="${PROJECT_ID}-media-${PROJECT_NUMBER}"
fi
BUCKET_LOCATION="${NUVIBU_BUCKET_LOCATION:-${VEO_LOCATION}}"
VEO_OUTPUT_GCS_URI="${VEO_OUTPUT_GCS_URI:-gs://${BUCKET_NAME}/veo-output/}"

if [[ "${VEO_BACKEND}" == "vertex" ]] &&
  [[ "${VEO_OUTPUT_GCS_URI}" != "gs://${BUCKET_NAME}/"* ]]; then
  echo "VEO_OUTPUT_GCS_URI must be inside the mounted bucket gs://${BUCKET_NAME}/" >&2
  exit 1
fi

SERVICE_APIS=(
  artifactregistry.googleapis.com
  cloudbuild.googleapis.com
  iam.googleapis.com
  run.googleapis.com
  secretmanager.googleapis.com
  storage.googleapis.com
)
if [[ "${VEO_BACKEND}" == "vertex" ]]; then
  SERVICE_APIS+=(aiplatform.googleapis.com)
else
  SERVICE_APIS+=(generativelanguage.googleapis.com)
fi
gcloud services enable "${SERVICE_APIS[@]}" --project "${PROJECT_ID}"

if ! gcloud artifacts repositories describe "${REPOSITORY}" \
  --project "${PROJECT_ID}" \
  --location "${RUN_REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPOSITORY}" \
    --project "${PROJECT_ID}" \
    --repository-format docker \
    --location "${RUN_REGION}" \
    --description "Nuvibu production containers"
fi

if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" \
  --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET_NAME}" \
    --project "${PROJECT_ID}" \
    --location "${BUCKET_LOCATION}" \
    --uniform-bucket-level-access
fi

ensure_service_account() {
  local account_name="$1"
  local display_name="$2"
  if ! gcloud iam service-accounts describe \
    "${account_name}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${account_name}" \
      --project "${PROJECT_ID}" \
      --display-name "${display_name}"
  fi
}

ensure_secret() {
  local secret_name="$1"
  local prompt="$2"
  local minimum_length="${3:-1}"
  if gcloud secrets describe "${secret_name}" \
    --project "${PROJECT_ID}" >/dev/null 2>&1; then
    return
  fi
  local secret_value
  read -r -s -p "${prompt}: " secret_value
  echo
  if (( ${#secret_value} < minimum_length )); then
    echo "Secret ${secret_name} must contain at least ${minimum_length} characters" >&2
    exit 1
  fi
  printf '%s' "${secret_value}" |
    gcloud secrets create "${secret_name}" \
      --project "${PROJECT_ID}" \
      --replication-policy automatic \
      --data-file=-
  unset secret_value
}

ensure_generated_secret() {
  local secret_name="$1"
  if gcloud secrets describe "${secret_name}" \
    --project "${PROJECT_ID}" >/dev/null 2>&1; then
    return
  fi
  python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' |
    gcloud secrets create "${secret_name}" \
      --project "${PROJECT_ID}" \
      --replication-policy automatic \
      --data-file=-
}

ensure_service_account "nuvibu-web" "Nuvibu web service"
ensure_service_account "nuvibu-worker" "Nuvibu media worker"

ensure_secret "database-url" "Neon pooled DATABASE_URL"
ensure_secret "admin-username" "Console admin username"
ensure_secret "admin-password" "Console admin password" 16
ensure_generated_secret "app-secret-key"

if ! gcloud secrets describe "elevenlabs-api-key" \
  --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "Missing Secret Manager secret: elevenlabs-api-key" >&2
  exit 1
fi
if [[ "${VEO_BACKEND}" == "gemini" ]]; then
  if ! gcloud secrets describe "gemini-api-key" \
    --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "Missing Secret Manager secret: gemini-api-key" >&2
    echo "The secret is required only when VEO_BACKEND=gemini." >&2
    exit 1
  fi
fi

for service_account in "${WEB_SA}" "${WORKER_SA}"; do
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
    --project "${PROJECT_ID}" \
    --member "serviceAccount:${service_account}" \
    --role roles/storage.objectUser >/dev/null
done

if [[ "${VEO_BACKEND}" == "vertex" ]]; then
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${WORKER_SA}" \
    --role roles/aiplatform.user \
    --condition=None >/dev/null
  gcloud beta services identity create \
    --project "${PROJECT_ID}" \
    --service aiplatform.googleapis.com >/dev/null
  VERTEX_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
    --project "${PROJECT_ID}" \
    --member "serviceAccount:${VERTEX_SERVICE_AGENT}" \
    --role roles/storage.objectUser >/dev/null
fi

grant_secret() {
  local secret_name="$1"
  local service_account="$2"
  gcloud secrets add-iam-policy-binding "${secret_name}" \
    --project "${PROJECT_ID}" \
    --member "serviceAccount:${service_account}" \
    --role roles/secretmanager.secretAccessor >/dev/null
}

for secret_name in database-url admin-username admin-password app-secret-key; do
  grant_secret "${secret_name}" "${WEB_SA}"
done
for secret_name in database-url elevenlabs-api-key; do
  grant_secret "${secret_name}" "${WORKER_SA}"
done
if [[ "${VEO_BACKEND}" == "gemini" ]]; then
  grant_secret "gemini-api-key" "${WORKER_SA}"
fi

secret_version() {
  local secret_name="$1"
  local version_path
  version_path="$(
    gcloud secrets versions list "${secret_name}" \
      --project "${PROJECT_ID}" \
      --filter='state=ENABLED' \
      --sort-by='~createTime' \
      --limit=1 \
      --format='value(name)'
  )"
  if [[ -z "${version_path}" ]]; then
    echo "Secret ${secret_name} has no enabled version" >&2
    exit 1
  fi
  printf '%s' "${version_path##*/}"
}

DATABASE_SECRET_VERSION="$(secret_version database-url)"
ADMIN_USERNAME_SECRET_VERSION="$(secret_version admin-username)"
ADMIN_PASSWORD_SECRET_VERSION="$(secret_version admin-password)"
APP_SECRET_VERSION="$(secret_version app-secret-key)"
ELEVENLABS_SECRET_VERSION="$(secret_version elevenlabs-api-key)"

WORKER_SECRETS="DATABASE_URL=database-url:${DATABASE_SECRET_VERSION},ELEVENLABS_API_KEY=elevenlabs-api-key:${ELEVENLABS_SECRET_VERSION}"
if [[ "${VEO_BACKEND}" == "gemini" ]]; then
  GEMINI_SECRET_VERSION="$(secret_version gemini-api-key)"
  WORKER_SECRETS+=",GEMINI_API_KEY=gemini-api-key:${GEMINI_SECRET_VERSION}"
fi

gcloud builds submit \
  --project "${PROJECT_ID}" \
  --tag "${IMAGE}" .

COMMON_ENV="APP_ENV=production,PROVIDER_MODE=live,STORAGE_BACKEND=gcs_mount,STORAGE_ROOT=/mnt/nuvibu,VEO_BACKEND=${VEO_BACKEND},VEO_MODEL=${VEO_MODEL},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${VEO_LOCATION},MAX_EPISODE_SECONDS=180,MAX_MUSIC_VARIANTS=1,MAX_SCENE_RETRIES=0,MAX_ESTIMATED_COST_USD_PER_EPISODE=40,MAX_DAILY_ESTIMATED_COST_USD=0,CLOUD_RUN_DISPATCH_RETRY_SECONDS=180,JOB_STALE_AFTER_SECONDS=3900"
if [[ "${VEO_BACKEND}" == "vertex" ]]; then
  COMMON_ENV+=",VEO_OUTPUT_GCS_URI=${VEO_OUTPUT_GCS_URI}"
fi
VOLUME_SPEC="mount-path=/mnt/nuvibu,type=cloud-storage,bucket=${BUCKET_NAME},readonly=false,mount-options=uid=10001;gid=10001"

gcloud run jobs deploy "${WORKER_JOB}" \
  --project "${PROJECT_ID}" \
  --image "${IMAGE}" \
  --region "${RUN_REGION}" \
  --service-account "${WORKER_SA}" \
  --command python \
  --args scripts/run_worker.py,--once \
  --tasks 1 \
  --parallelism 1 \
  --max-retries 0 \
  --task-timeout 3600s \
  --cpu 2 \
  --memory 4Gi \
  --set-env-vars "${COMMON_ENV},RUNTIME_ROLE=worker" \
  --set-secrets "${WORKER_SECRETS}" \
  --add-volume "${VOLUME_SPEC}"

gcloud run jobs add-iam-policy-binding "${WORKER_JOB}" \
  --project "${PROJECT_ID}" \
  --region "${RUN_REGION}" \
  --member "serviceAccount:${WEB_SA}" \
  --role roles/run.jobsExecutorWithOverrides >/dev/null


echo "Creating the approved consistency-first Wimbledon episode"
gcloud run jobs deploy "${EDITORIAL_RELEASE_JOB}" \
  --project "${PROJECT_ID}" \
  --image "${IMAGE}" \
  --region "${RUN_REGION}" \
  --service-account "${WEB_SA}" \
  --command python \
  --args scripts/create_wimbledon_consistency_episode.py \
  --tasks 1 \
  --parallelism 1 \
  --max-retries 0 \
  --task-timeout 600s \
  --cpu 1 \
  --memory 512Mi \
  --set-env-vars "${COMMON_ENV},RUNTIME_ROLE=maintenance" \
  --set-secrets "DATABASE_URL=database-url:${DATABASE_SECRET_VERSION}" \
  --add-volume "${VOLUME_SPEC}"
gcloud run jobs execute "${EDITORIAL_RELEASE_JOB}" \
  --project "${PROJECT_ID}" \
  --region "${RUN_REGION}" \
  --wait

EXISTING_SERVICE_URL="$(
  gcloud run services describe "${WEB_SERVICE}" \
    --project "${PROJECT_ID}" \
    --region "${RUN_REGION}" \
    --format='value(status.url)' 2>/dev/null || true
)"
INITIAL_APP_BASE_URL="${EXISTING_SERVICE_URL:-https://placeholder.invalid}"

gcloud run deploy "${WEB_SERVICE}" \
  --project "${PROJECT_ID}" \
  --image "${IMAGE}" \
  --region "${RUN_REGION}" \
  --service-account "${WEB_SA}" \
  --port 8080 \
  --cpu 1 \
  --memory 1Gi \
  --timeout 300 \
  --min 1 \
  --max 3 \
  --min-instances default \
  --max-instances default \
  --concurrency 20 \
  --cpu-boost \
  --allow-unauthenticated \
  --set-env-vars "${COMMON_ENV},RUNTIME_ROLE=web,CLOUD_RUN_JOB_NAME=${WORKER_JOB},CLOUD_RUN_JOB_LOCATION=${RUN_REGION},APP_BASE_URL=${INITIAL_APP_BASE_URL}" \
  --set-secrets "DATABASE_URL=database-url:${DATABASE_SECRET_VERSION},ADMIN_USERNAME=admin-username:${ADMIN_USERNAME_SECRET_VERSION},ADMIN_PASSWORD=admin-password:${ADMIN_PASSWORD_SECRET_VERSION},SECRET_KEY=app-secret-key:${APP_SECRET_VERSION}" \
  --add-volume "${VOLUME_SPEC}"

SERVICE_URL="$(
  gcloud run services describe "${WEB_SERVICE}" \
    --project "${PROJECT_ID}" \
    --region "${RUN_REGION}" \
    --format='value(status.url)'
)"
gcloud run services update "${WEB_SERVICE}" \
  --project "${PROJECT_ID}" \
  --region "${RUN_REGION}" \
  --update-env-vars "APP_BASE_URL=${SERVICE_URL}" >/dev/null

HEALTH_JSON="$(
  curl --fail --silent --show-error "${SERVICE_URL}/health"
)"
if ! python3 -c '
import json
import sys

payload = json.loads(sys.argv[1])
expected = {"la-fattoria-v1", "nanna-arcobaleno-v1"}
actual = set(payload.get("reference_presets", []))
if payload.get("status") != "ok" or actual != expected:
    raise SystemExit(1)
' "${HEALTH_JSON}"; then
  echo "Reference preset health smoke test failed for ${SERVICE_URL}" >&2
  exit 1
fi
curl --fail --silent --show-error "${SERVICE_URL}/readyz" >/dev/null
LOGIN_PAGE="$(curl --fail --silent --show-error "${SERVICE_URL}/login")"
if [[ "${LOGIN_PAGE}" != *"Accedi allo studio"* ]]; then
  echo "Login smoke test failed for ${SERVICE_URL}/login" >&2
  exit 1
fi
EMMA_THUMBNAIL_PATH="app/static/emma-looks/emma-pink-dress-v1.webp"
EXPECTED_EMMA_THUMBNAIL_SHA="$(
  sha256sum "${EMMA_THUMBNAIL_PATH}" | awk '{print $1}'
)"
DEPLOYED_EMMA_THUMBNAIL_SHA="$(
  curl --fail --silent --show-error \
    "${SERVICE_URL}/static/emma-looks/emma-pink-dress-v1.webp?release=${SOURCE_COMMIT}" \
    | sha256sum \
    | awk '{print $1}'
)"
if [[ "${DEPLOYED_EMMA_THUMBNAIL_SHA}" != "${EXPECTED_EMMA_THUMBNAIL_SHA}" ]]; then
  echo "Emma look asset smoke test failed for ${SERVICE_URL}" >&2
  exit 1
fi

echo "Nuvibu web: ${SERVICE_URL}"
echo "Worker job: ${WORKER_JOB} (${RUN_REGION})"
echo "Media bucket: gs://${BUCKET_NAME}"
echo "Release commit: ${SOURCE_COMMIT}"
echo "Veo backend: ${VEO_BACKEND} (${VEO_MODEL}, ${VEO_LOCATION})"
