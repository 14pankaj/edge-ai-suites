#!/usr/bin/env bash
# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Downloads the YOLO model and converts the LLM configured for a use case (LLM_MODEL_NAME in
# apps/<dir>/.env_<use-case>) to OpenVINO Model Server (OVMS) format using
# the model-download microservice (https://github.com/open-edge-platform/edge-ai-libraries/
# tree/main/microservices/model-download), then writes the resulting local path
# back into the use case's env file as LLM_MODEL_PATH so setup.sh can mount it
# into the apm-llm (OVMS) container.
#
# Usage:
#   ./scripts/download_llm_model.sh --use-case pipeline-defect-detection
#   ./scripts/download_llm_model.sh --use-case gas-detection

set -Eeuo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[INFO ] $*${NC}"; }
warn() { echo -e "${YELLOW}[WARN ] $*${NC}" >&2; }
err()  { echo -e "${RED}[ERROR] $*${NC}" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

usage() {
    cat <<EOF
Usage:
  $(basename "$0") --use-case <use-case-name>

Downloads and converts the LLM configured for <use-case> (via LLM_MODEL_NAME,
LLM_DEVICE, LLM_WEIGHT_FORMAT in apps/<dir>/.env_<use-case>) to OVMS
format using the model-download microservice, then writes LLM_MODEL_PATH back
into that env file. The YOLO11 model named by YOLO_MODEL_NAME (default:
yolo11n; precision via YOLO_PRECISION, default FP32) is always downloaded too,
and YOLO_MODEL_PATH is written.

Options:
  --use-case <name>   Use case name, i.e. the <name> in apps/<dir>/.env_<name> (required)
  -h, --help          Show this help message
EOF
}

USE_CASE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --use-case) USE_CASE="$2"; shift 2 ;;
        --use-case=*) USE_CASE="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) err "Unknown argument: $1"; usage; exit 1 ;;
    esac
done

if [[ -z "${USE_CASE}" ]]; then
    err "--use-case is required."
    usage
    exit 1
fi

# A use case is identified by its env file (apps/<dir>/.env_<use-case>);
# <dir> is usually the use-case name but may differ (e.g. use case
# "gas-detection" lives in apps/gas-detection-multimodal/).
ENV_FILE="${ROOT_DIR}/apps/${USE_CASE}/.env_${USE_CASE}"
if [[ ! -f "${ENV_FILE}" ]]; then
    ENV_FILE=""
    for env_candidate in "${ROOT_DIR}"/apps/*/.env_"${USE_CASE}"; do
        if [[ -f "${env_candidate}" ]]; then
            ENV_FILE="${env_candidate}"
            break
        fi
    done
fi

if [[ -z "${ENV_FILE}" ]]; then
    err "Environment file not found: apps/<dir>/.env_${USE_CASE}"
    exit 1
fi
USE_CASE_DIR="$(dirname "${ENV_FILE}")"

# shellcheck disable=SC1090
set -a
source "${ENV_FILE}"
set +a

DOWNLOAD_LLM=true
DOWNLOAD_YOLO=true

if [[ "${LLM_MODE:-llm}" == "fallback" ]]; then
    warn "LLM_MODE=fallback in ${ENV_FILE} — no LLM model download needed."
    DOWNLOAD_LLM=false
fi

if [[ "${DOWNLOAD_LLM}" == "true" ]]; then
    LLM_MODEL_NAME="${LLM_MODEL_NAME:?LLM_MODEL_NAME must be set in ${ENV_FILE}}"
fi
LLM_DEVICE="${LLM_DEVICE:-CPU}"
LLM_WEIGHT_FORMAT="${LLM_WEIGHT_FORMAT:-int8}"
YOLO_MODEL_NAME="${YOLO_MODEL_NAME:-yolo11n}"
YOLO_PRECISION="${YOLO_PRECISION:-FP32}"
MODEL_DOWNLOAD_PORT="${MODEL_DOWNLOAD_PORT:-8200}"
USE_CASE_MODELS_DIR="${USE_CASE_DIR}/models"

if command -v curl >/dev/null 2>&1; then
    :
else
    err "Required command not found: curl"
    exit 1
fi

if [[ "${DOWNLOAD_LLM}" == "true" && "${LLM_DEVICE}" == "NPU" && "${LLM_WEIGHT_FORMAT}" != "int4" ]]; then
    warn "NPU only supports int4 conversion — overriding LLM_WEIGHT_FORMAT '${LLM_WEIGHT_FORMAT}' to 'int4'."
    LLM_WEIGHT_FORMAT="int4"
fi

mkdir -p "${USE_CASE_MODELS_DIR}"
# apm-model-download runs as a non-root container user (appuser); make sure it
# can write new model subdirectories into the host-mounted models directory
# regardless of the host UID that owns it.
chmod -R a+rwX "${USE_CASE_MODELS_DIR}" 2>/dev/null || true

log "Starting model-download service (apm-model-download)..."
export USE_CASE_DIR USE_CASE_MODELS_DIR
export USE_CASE_CONFIGS_DIR="${USE_CASE_DIR}/configs"
export USE_CASE_PROMPTS_DIR="${USE_CASE_DIR}/prompts"
export USE_CASE_RESOURCES_DIR="${USE_CASE_DIR}/resources"
export APP_HOST_PORT="${APP_HOST_PORT:-8080}"
# Services in compose.base.yaml (e.g. nginx) depend_on services defined in the
# other compose files (apm-ui, apm-agent), so all files must be loaded together
# for Compose to resolve the project — even though we only start model-download.
COMPOSE_CMD="docker compose \
    -f ${ROOT_DIR}/docker/compose.base.yaml \
    -f ${ROOT_DIR}/docker/compose.detection.yaml \
    -f ${ROOT_DIR}/docker/compose.agents.yaml \
    -f ${ROOT_DIR}/docker/compose.ui.yaml"
${COMPOSE_CMD} up -d model-download

log "Waiting for model-download service to become healthy on port ${MODEL_DOWNLOAD_PORT}..."
for i in $(seq 1 60); do
    if curl -sf "http://localhost:${MODEL_DOWNLOAD_PORT}/api/v1/models/results" >/dev/null 2>&1; then
        break
    fi
    if [[ "${i}" -eq 60 ]]; then
        err "model-download service did not become ready in time. Check: docker logs apm-model-download"
        exit 1
    fi
    sleep 2
done

# submit_and_wait <download_path> <json-body>
# Submits a download request and blocks until the job completes or fails.
submit_and_wait() {
    local download_path="$1" body="$2" response job_id job="" status="processing" i

    response=$(curl -sf -X POST \
        "http://localhost:${MODEL_DOWNLOAD_PORT}/api/v1/models/download?download_path=${download_path}" \
        -H "Content-Type: application/json" \
        -d "${body}") || {
        err "Failed to submit download request to model-download service."
        exit 1
    }

    job_id=$(echo "${response}" | python3 -c "import sys, json; print(json.load(sys.stdin)['job_ids'][0])" 2>/dev/null) || {
        err "Unexpected response from model-download service: ${response}"
        exit 1
    }

    log "Download job submitted (job_id=${job_id}). Waiting for completion — this may take a while..."

    for i in $(seq 1 600); do
        job=$(curl -sf "http://localhost:${MODEL_DOWNLOAD_PORT}/api/v1/jobs/${job_id}") || {
            err "Failed to query job status."
            exit 1
        }
        status=$(echo "${job}" | python3 -c "import sys, json; print(json.load(sys.stdin).get('status', 'unknown'))")
        if [[ "${status}" == "completed" || "${status}" == "failed" ]]; then
            break
        fi
        sleep 5
    done

    if [[ "${status}" != "completed" ]]; then
        err "Model download did not complete successfully (status=${status})."
        err "Job details: ${job}"
        exit 1
    fi
}

# set_env_var <KEY> <value> <comment>
# Persists KEY into the use-case env file (replace if present, append otherwise).
set_env_var() {
    local key="$1" value="$2" comment="$3"
    if grep -q "^${key}=" "${ENV_FILE}"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
    else
        {
            echo ""
            echo "# ${comment}"
            echo "${key}=${value}"
        } >> "${ENV_FILE}"
    fi
}

download_llm() {
    local download_path="llm_model_${USE_CASE}"
    log "Requesting download+conversion of '${LLM_MODEL_NAME}' (device=${LLM_DEVICE}, precision=${LLM_WEIGHT_FORMAT})..."

    submit_and_wait "${download_path}" "$(cat <<JSON
{
  "models": [
    {
      "name": "${LLM_MODEL_NAME}",
      "hub": "openvino",
      "type": "llm",
      "is_ovms": true,
      "config": {
        "precision": "${LLM_WEIGHT_FORMAT}",
        "device": "${LLM_DEVICE}",
        "pipeline_type": "LM_CB"
      }
    }
  ],
  "parallel_downloads": false
}
JSON
)"

    log "Download complete. Locating converted model files..."

    local device_lower precision_lower nested_dir model_basename model_src
    device_lower="$(echo "${LLM_DEVICE}" | tr '[:upper:]' '[:lower:]')"
    precision_lower="$(echo "${LLM_WEIGHT_FORMAT}" | tr '[:upper:]' '[:lower:]')"
    nested_dir="${USE_CASE_MODELS_DIR}/${download_path}/openvino_models/${device_lower}/${precision_lower}"
    model_basename="$(basename "${LLM_MODEL_NAME}")"

    model_src=$(find "${nested_dir}" -mindepth 1 -maxdepth 2 -type d -iname "${model_basename}" 2>/dev/null | head -1)
    if [[ -z "${model_src}" ]]; then
        err "Could not find converted model under: ${nested_dir}"
        err "Inspect the download output manually:"
        find "${USE_CASE_MODELS_DIR}/${download_path}" -maxdepth 4 -type d 2>/dev/null | sed 's/^/  /'
        exit 1
    fi

    log "Found model at: ${model_src}"
    set_env_var "LLM_MODEL_PATH" "${model_src}" "Set automatically by scripts/download_llm_model.sh"
    log "LLM_MODEL_PATH set to '${model_src}' in ${ENV_FILE}"
}

# Downloads the YOLO11 model via the Ultralytics plugin; the plugin exports it
# to OpenVINO IR under <download_path>/ultralytics/public/<name>/<PRECISION>/.
download_yolo() {
    local download_path="yolo_model_${USE_CASE}"
    log "Requesting download of '${YOLO_MODEL_NAME}' (Ultralytics, OpenVINO IR ${YOLO_PRECISION})..."

    submit_and_wait "${download_path}" "$(cat <<JSON
{
  "models": [
    {
      "name": "${YOLO_MODEL_NAME}",
      "hub": "ultralytics"
    }
  ]
}
JSON
)"

    local model_dir="${USE_CASE_MODELS_DIR}/${download_path}/ultralytics/public/${YOLO_MODEL_NAME}/${YOLO_PRECISION}"
    if [[ ! -f "${model_dir}/${YOLO_MODEL_NAME}.xml" ]]; then
        err "Could not find ${YOLO_MODEL_NAME}.xml under: ${model_dir}"
        find "${USE_CASE_MODELS_DIR}/${download_path}" -maxdepth 5 2>/dev/null | sed 's/^/  /'
        exit 1
    fi

    log "Found model at: ${model_dir}"
    set_env_var "YOLO_MODEL_PATH" "${model_dir}" "Set automatically by scripts/download_llm_model.sh"
    log "YOLO_MODEL_PATH set to '${model_dir}' in ${ENV_FILE}"
}

[[ "${DOWNLOAD_LLM}" == "true" ]] && download_llm
[[ "${DOWNLOAD_YOLO}" == "true" ]] && download_yolo

log "You can now run: source setup.sh --use-case ${USE_CASE}"
