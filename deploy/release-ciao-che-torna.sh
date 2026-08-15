#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-nuvibu}"
RUN_REGION="${CLOUD_RUN_REGION:-us-central1}"
REPOSITORY="${ARTIFACT_REPOSITORY:-nuvibu}"
BUCKET_NAME="${NUVIBU_BUCKET:-}"
WEB_SA="nuvibu-web@${PROJECT_ID}.iam.gserviceaccount.com"
JOB_NAME="nuvibu-ciao-che-torna-release-20260815"
SOURCE_COMMIT="$(git rev-parse HEAD)"
IMAGE="${RUN_REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/nuvibu:${SOURCE_COMMIT}"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --project "${PROJECT_ID}" --format='value(projectNumber)')"
if [[ -z "${BUCKET_NAME}" ]]; then
  BUCKET_NAME="${PROJECT_ID}-media-${PROJECT_NUMBER}"
fi

if ! gcloud artifacts docker images describe "${IMAGE}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "Building release image ${IMAGE}"
  gcloud builds submit --project "${PROJECT_ID}" --tag "${IMAGE}" .
fi

DATABASE_SECRET_VERSION="$(
  gcloud secrets versions list database-url \
    --project "${PROJECT_ID}" \
    --filter='state=ENABLED' \
    --sort-by='~createTime' \
    --limit=1 \
    --format='value(name)' | awk -F/ '{print $NF}'
)"

if [[ -z "${DATABASE_SECRET_VERSION}" ]]; then
  echo "database-url has no enabled secret version" >&2
  exit 1
fi

COMMON_ENV="APP_ENV=production,PROVIDER_MODE=live,STORAGE_BACKEND=gcs_mount,STORAGE_ROOT=/mnt/nuvibu,MAX_EPISODE_SECONDS=180,RUNTIME_ROLE=maintenance"
VOLUME_SPEC="mount-path=/mnt/nuvibu,type=cloud-storage,bucket=${BUCKET_NAME},readonly=false,mount-options=uid=10001;gid=10001"

echo "Creating/updating Emma e il Ciao che Torna in the production database"
gcloud run jobs deploy "${JOB_NAME}" \
  --project "${PROJECT_ID}" \
  --image "${IMAGE}" \
  --region "${RUN_REGION}" \
  --service-account "${WEB_SA}" \
  --command python \
  --args scripts/create_ciao_che_torna_episode.py \
  --tasks 1 \
  --parallelism 1 \
  --max-retries 0 \
  --task-timeout 600s \
  --cpu 1 \
  --memory 512Mi \
  --set-env-vars "${COMMON_ENV}" \
  --set-secrets "DATABASE_URL=database-url:${DATABASE_SECRET_VERSION}" \
  --add-volume "${VOLUME_SPEC}"

gcloud run jobs execute "${JOB_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${RUN_REGION}" \
  --wait

echo "Episode release completed: Emma e il Ciao che Torna (160s / 20 scenes)"
