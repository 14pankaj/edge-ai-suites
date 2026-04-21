#!/usr/bin/env python3
# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
OVMS init script — downloads models from HuggingFace and creates
the graph.pbtxt + config_all.json required for multi-model serving.

Runs once as an init container before the OVMS service starts.

Environment variables (set via docker-compose):
    VLM_NAME        OVMS servable name for the VLM model
    VLM_REPO        HuggingFace repo ID for the VLM model
    LLM_NAME        OVMS servable name for the LLM model
    LLM_REPO        HuggingFace repo ID for the LLM model
    TARGET_DEVICE   OpenVINO target device (CPU, GPU)
    HF_TOKEN        HuggingFace token for gated models (optional)
"""

import json
import os
import subprocess
import sys
from pathlib import Path
VLM_GRAPH_PBTXT_TEMPLATE = """\
input_stream: "HTTP_REQUEST_PAYLOAD:input"
output_stream: "HTTP_RESPONSE_PAYLOAD:output"

node: {
  name: "LLMExecutor"
  calculator: "HttpLLMCalculator"
  input_stream: "LOOPBACK:loopback"
  input_stream: "HTTP_REQUEST_PAYLOAD:input"
  input_side_packet: "LLM_NODE_RESOURCES:llm"
  output_stream: "LOOPBACK:loopback"
  output_stream: "HTTP_RESPONSE_PAYLOAD:output"
  input_stream_info: {
    tag_index: 'LOOPBACK:0',
    back_edge: true
  }
  node_options: {
      [type.googleapis.com / mediapipe.LLMCalculatorOptions]: {
          models_path: "./"
          device: "__DEVICE__"
          cache_size: 8
          enable_prefix_caching: true
          max_num_batched_tokens: 1024
          max_num_seqs: 8
          plugin_config: '{"KV_CACHE_PRECISION": "u8", "DYNAMIC_QUANTIZATION_GROUP_SIZE": "32"}'
      }
  }
  input_stream_handler {
    input_stream_handler: "SyncSetInputStreamHandler",
    options {
      [mediapipe.SyncSetInputStreamHandlerOptions.ext] {
        sync_set {
          tag_index: "LOOPBACK:0"
        }
      }
    }
  }
}
"""

# LLM graph — smaller cache (used by ADK action agent if enabled)
LLM_GRAPH_PBTXT_TEMPLATE = """\
input_stream: "HTTP_REQUEST_PAYLOAD:input"
output_stream: "HTTP_RESPONSE_PAYLOAD:output"

node: {
  name: "LLMExecutor"
  calculator: "HttpLLMCalculator"
  input_stream: "LOOPBACK:loopback"
  input_stream: "HTTP_REQUEST_PAYLOAD:input"
  input_side_packet: "LLM_NODE_RESOURCES:llm"
  output_stream: "LOOPBACK:loopback"
  output_stream: "HTTP_RESPONSE_PAYLOAD:output"
  input_stream_info: {
    tag_index: 'LOOPBACK:0',
    back_edge: true
  }
  node_options: {
      [type.googleapis.com / mediapipe.LLMCalculatorOptions]: {
          models_path: "./"
          device: "__DEVICE__"
          cache_size: 4
          enable_prefix_caching: true
          max_num_batched_tokens: 512
          max_num_seqs: 4
          plugin_config: '{"KV_CACHE_PRECISION": "u8", "DYNAMIC_QUANTIZATION_GROUP_SIZE": "32"}'
      }
  }
  input_stream_handler {
    input_stream_handler: "SyncSetInputStreamHandler",
    options {
      [mediapipe.SyncSetInputStreamHandlerOptions.ext] {
        sync_set {
          tag_index: "LOOPBACK:0"
        }
      }
    }
  }
}
"""


def main():
    models_dir = Path("/models")

    vlm_name = os.environ.get("VLM_NAME", "Phi-3.5-Vision")
    vlm_repo = os.environ.get("VLM_REPO", "OpenVINO/Phi-3.5-vision-instruct-int4-ov")
    llm_name = os.environ.get("LLM_NAME", "Phi-4-mini-instruct")
    llm_repo = os.environ.get("LLM_REPO", "OpenVINO/Phi-4-mini-instruct-int4-ov")
    device = os.environ.get("TARGET_DEVICE", "CPU")

    # ------------------------------------------------------------------
    # Ensure huggingface_hub is available
    # ------------------------------------------------------------------
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("[init] Installing huggingface_hub ...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"]
        )
        from huggingface_hub import snapshot_download

    # ------------------------------------------------------------------
    # Download models (skip if already present on the persistent volume)
    # ------------------------------------------------------------------
    for name, repo in [(vlm_name, vlm_repo), (llm_name, llm_repo)]:
        model_dir = models_dir / name
        if (model_dir / "openvino_tokenizer.xml").exists():
            print(f"[init] {name} already downloaded — skipping")
        else:
            print(f"[init] Downloading {repo} -> /models/{name} ...")
            snapshot_download(repo, local_dir=str(model_dir))
            print(f"[init] {name} download complete")

    # ------------------------------------------------------------------
    # Write graph.pbtxt for each model (VLM and LLM get separate configs)
    # ------------------------------------------------------------------
    vlm_graph = VLM_GRAPH_PBTXT_TEMPLATE.replace("__DEVICE__", device)
    llm_graph = LLM_GRAPH_PBTXT_TEMPLATE.replace("__DEVICE__", device)

    vlm_graph_path = models_dir / vlm_name / "graph.pbtxt"
    vlm_graph_path.write_text(vlm_graph)
    print(f"[init] Created {vlm_graph_path} (VLM config: cache_size=8, prefix_caching=on, KV u8)")

    llm_graph_path = models_dir / llm_name / "graph.pbtxt"
    llm_graph_path.write_text(llm_graph)
    print(f"[init] Created {llm_graph_path} (LLM config: cache_size=4, prefix_caching=on, KV u8)")

    # ------------------------------------------------------------------
    # Write OVMS config file
    # ------------------------------------------------------------------
    config = {
        "model_config_list": [
            {
                "config": {
                    "name": vlm_name,
                    "base_path": f"/models/{vlm_name}",
                }
            },
            {
                "config": {
                    "name": llm_name,
                    "base_path": f"/models/{llm_name}",
                }
            },
        ]
    }
    config_path = models_dir / "config_all.json"
    config_path.write_text(json.dumps(config, indent=2))
    print(f"[init] Created {config_path}")

    # ------------------------------------------------------------------
    # Fix permissions for OVMS (runs as uid 5000)
    # ------------------------------------------------------------------
    subprocess.run(["chown", "-R", "5000:5000", "/models"], check=True)
    subprocess.run(["chmod", "-R", "755", "/models"], check=True)

    print("[init] OVMS model preparation complete")


if __name__ == "__main__":
    main()
