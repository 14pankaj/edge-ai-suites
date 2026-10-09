#!/usr/bin/env python3
# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
Download a dataset and prepare train/val splits for the Agentic Predictive
Maintenance blueprint.

Adapted from intel/predictive-maintenance-pipeline — original logic unchanged;
the blueprint-specific changes are:

  1. ``--use-case`` flag selects the use case (default: pipeline-defect-detection)
  2. The generated sample video is placed at
     ``apps/<dir>/resources/videos/datastream.mp4`` so DL Streamer can read
     it immediately after running this script.
  3. Training data is written to ``datasets/<use_case>/`` (YOLO format).

Supported use cases
-------------------
  pipeline-defect-detection
      Splits randomly (default 90/10) into YOLO detection format:
        datasets/pipeline_defect_detection/images/{train,val}/
        datasets/pipeline_defect_detection/labels/{train,val}/
      Creates the inference video at:
        apps/pipeline-defect-detection/resources/videos/datastream.mp4

  gas-detection
      Stratified 80/20 split per gas class (Mixture, NoGas, Perfume, Smoke):
        datasets/gas_detection/images/train/{class}/
        datasets/gas_detection/images/val/
        datasets/gas_detection/sensor_data/
      Creates the inference video + frame-index -> filename manifest at:
        apps/gas-detection-multimodal/resources/videos/datastream.mp4
        datasets/gas_detection/images/val/frame_manifest.json

DISCLAIMER:
    By using this script you are solely responsible for ensuring you have the
    necessary rights, permissions, and licenses to download and use the dataset
    at the provided URL.

Usage
-----
  # Pipeline defect dataset (Kaggle)
  python scripts/download_and_prep_data.py \\
      "https://www.kaggle.com/api/v1/datasets/download/simplexitypipeline/pipeline-defect-dataset"

  # Gas detection dataset (Mendeley)
  python scripts/download_and_prep_data.py \\
      "https://data.mendeley.com/public-api/zip/zkwgkjkjn9/download/2" \\
      --use-case gas-detection

  # Custom split ratio
  python scripts/download_and_prep_data.py "<url>" --train-ratio 0.8
"""

import argparse
import json
import random
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    import cv2
except ImportError:
    cv2 = None


GAS_DETECTION_CLASSES = ["Mixture", "NoGas", "Perfume", "Smoke"]

DISCLAIMER = """
⚠️  DISCLAIMER: By using this script, you acknowledge that YOU are solely
    responsible for ensuring you have the necessary rights, permissions, and
    licenses to download and use the dataset at the provided URL. Intel takes
    no responsibility for any misuse of data or violation of terms of service.
"""

# Maps CLI use-case names → internal snake_case IDs and dataset directory names
_USE_CASE_MAP = {
    "pipeline-defect-detection": "pipeline_defect_detection",
    "gas-detection":             "gas_detection",
    # extend for new use cases (e.g. "weld-defect-detection": "weld_defect_detection")
}

# Maps CLI use-case names → the apps/<dir> directory the video is written under.
# Not always identical to the use-case name itself (use case "gas-detection"
# lives in apps/gas-detection-multimodal/).
_APP_DIR_MAP = {
    "pipeline-defect-detection": "pipeline-defect-detection",
    "gas-detection":             "gas-detection-multimodal",
}


# ─────────────────────────────────────────────────────────────────────────────
# Download helpers
# ─────────────────────────────────────────────────────────────────────────────

def download_dataset(dataset_url: str, download_dir: Path,
                     zip_name: str = "dataset.zip") -> Path:
    """Download the dataset from *dataset_url* using curl."""
    print("📥 Downloading dataset...")
    print(f"   URL: {dataset_url}")
    print(f"   Destination: {download_dir}")
    print()

    download_dir.mkdir(parents=True, exist_ok=True)
    zip_path = download_dir / zip_name

    try:
        subprocess.run(["curl", "-L", "-o", str(zip_path), dataset_url], check=True)
        print("✅ Download complete\n")

        print("📦 Extracting dataset...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(download_dir)
        zip_path.unlink()
        print("✅ Extraction complete\n")

        return download_dir

    except FileNotFoundError:
        print("❌ Error: 'curl' not found. Please install curl.")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"❌ Download failed (exit code {e.returncode})")
        sys.exit(1)
    except zipfile.BadZipFile:
        print("❌ Downloaded file is not a valid zip archive.")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline defect detection helpers
# ─────────────────────────────────────────────────────────────────────────────

def find_images_and_labels(source_dir: Path):
    """Recursively find all image files and match to YOLO .txt label files."""
    image_extensions = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}

    images = sorted(
        [f for f in source_dir.rglob("*")
         if f.suffix.lower() in image_extensions and f.is_file()],
        key=lambda p: p.name,
    )

    label_map: dict[str, Path] = {}
    for f in source_dir.rglob("*.txt"):
        if f.is_file():
            stem = f.stem
            if stem not in label_map or "labels" in str(f.parent).lower():
                label_map[stem] = f

    pairs = []
    matched = 0
    for img in images:
        lbl = label_map.get(img.stem)
        if lbl:
            matched += 1
        pairs.append((img, lbl))

    print(f"   Matched {matched}/{len(images)} images with label files")
    return pairs


def split_dataset(pairs: list, train_ratio: float, seed: int):
    random.seed(seed)
    shuffled = pairs.copy()
    random.shuffle(shuffled)
    idx = int(len(shuffled) * train_ratio)
    return shuffled[:idx], shuffled[idx:]


def copy_pairs(pairs: list, dest_images: Path, dest_labels: Path, split_name: str):
    dest_images.mkdir(parents=True, exist_ok=True)
    dest_labels.mkdir(parents=True, exist_ok=True)
    print(f"   Copying {len(pairs)} samples to {split_name}...")
    for img, lbl in pairs:
        shutil.copy2(img, dest_images / img.name)
        if lbl and lbl.exists():
            shutil.copy2(lbl, dest_labels / lbl.name)
    print(f"   ✅ {split_name}: {len(pairs)} images")


# ─────────────────────────────────────────────────────────────────────────────
# Gas detection helpers
# ─────────────────────────────────────────────────────────────────────────────

def find_gas_images_by_class(source_dir: Path) -> dict:
    class_files: dict = {cls: [] for cls in GAS_DETECTION_CLASSES}
    for f in source_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            parts = f.stem.rsplit("_", 1)
            if len(parts) == 2 and parts[1] in class_files:
                class_files[parts[1]].append(f)
    for cls in GAS_DETECTION_CLASSES:
        class_files[cls].sort(key=lambda p: p.name)
    return class_files


def prep_gas_detection(download_dir: Path, output_dir: Path,
                       train_ratio: float, seed: int) -> tuple:
    print("🔍 Scanning for gas detection images...")
    class_files = find_gas_images_by_class(download_dir)
    total = sum(len(v) for v in class_files.values())
    if total == 0:
        print("❌ No gas detection images found.")
        sys.exit(1)
    for cls in GAS_DETECTION_CLASSES:
        print(f"   {cls}: {len(class_files[cls])} images")
    print()

    print(f"🔀 Stratified split (train={train_ratio:.0%} / val={1-train_ratio:.0%})...")
    random.seed(seed)
    train_files: dict = {}
    val_files: dict = {}
    for cls, files in class_files.items():
        shuffled = files.copy()
        random.shuffle(shuffled)
        idx = int(len(shuffled) * train_ratio)
        train_files[cls] = shuffled[:idx]
        val_files[cls] = shuffled[idx:]
        print(f"   {cls}: {len(train_files[cls])} train, {len(val_files[cls])} val")
    print()

    print("📂 Organising dataset...")
    for cls, files in train_files.items():
        dest = output_dir / "images" / "train" / cls
        dest.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, dest / f.name)
    n_train = sum(len(v) for v in train_files.values())
    print(f"   ✅ train: {n_train} images  →  images/train/{{class}}/")

    val_dir = output_dir / "images" / "val"
    val_dir.mkdir(parents=True, exist_ok=True)
    n_val = 0
    for files in val_files.values():
        for f in files:
            shutil.copy2(f, val_dir / f.name)
            n_val += 1
    print(f"   ✅ val:   {n_val} images  →  images/val/ (flat for inference)")
    print()

    print("📊 Looking for sensor data CSV...")
    csv_files = list(download_dir.rglob("*.csv"))
    if csv_files:
        sensor_dir = output_dir / "sensor_data"
        sensor_dir.mkdir(parents=True, exist_ok=True)
        for csv_f in csv_files:
            shutil.copy2(csv_f, sensor_dir / csv_f.name)
            print(f"   ✅ {csv_f.name}  →  sensor_data/")
    else:
        print("   ⚠️  No CSV found — place sensor data manually.")
    print()

    return n_train, n_val


# ─────────────────────────────────────────────────────────────────────────────
# Video creation
# ─────────────────────────────────────────────────────────────────────────────

def list_images_sorted(images_dir: Path) -> list:
    """Return all .jpg (falling back to .png) images in *images_dir*, sorted by name.

    This is the single source of truth for "frame order" — both video creation
    and the frame-index → filename manifest must iterate this same list so a
    DL Streamer frame id can be reliably mapped back to its source image.
    """
    image_files = sorted(images_dir.glob("*.jpg"))
    if not image_files:
        image_files = sorted(images_dir.glob("*.png"))
    return image_files


def write_frame_manifest(image_files: list, manifest_path: Path) -> None:
    """Write a frame_index -> filename JSON manifest for *image_files* (in order).

    Lets a later stage join a DL Streamer pipeline's per-frame results (only a
    frame id, no filename) back to the original image filename (e.g. to re-join
    per-sample sensor CSV rows keyed by filename).
    """
    manifest = {str(i): f.name for i, f in enumerate(image_files)}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"   ✅ {manifest_path}  ({len(manifest)} frames)")


def create_video_from_images(images_dir: Path, video_path: Path, fps: int = 30,
                             manifest_path: Path = None) -> bool:
    """Build an MP4 from all .jpg images in *images_dir* and write to *video_path*.

    If *manifest_path* is given, also writes a frame_index -> filename JSON
    manifest (see ``write_frame_manifest``) using the exact same sorted file
    order the video frames are written in.
    """
    if cv2 is None:
        print("   ⚠️  opencv-python not installed — skipping video creation.")
        print("       Install with: pip install opencv-python")
        return False

    image_files = list_images_sorted(images_dir)
    if not image_files:
        print(f"   ⚠️  No images found in {images_dir} — skipping video creation.")
        return False

    first = cv2.imread(str(image_files[0]))
    if first is None:
        print(f"   ⚠️  Could not read {image_files[0]} — skipping video creation.")
        return False

    h, w = first.shape[:2]
    video_path.parent.mkdir(parents=True, exist_ok=True)

    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (w, h),
    )
    if not writer.isOpened():
        print(f"   ❌ Could not open {video_path} for writing (check directory permissions).")
        return False

    # Only frames actually written to the video go into the manifest, so a
    # frame index always maps to the image that produced that frame.
    written_files = []
    for img_file in image_files:
        frame = cv2.imread(str(img_file))
        if frame is not None:
            if frame.shape[:2] != (h, w):
                frame = cv2.resize(frame, (w, h))
            writer.write(frame)
            written_files.append(img_file)
    writer.release()
    written = len(written_files)

    if not video_path.is_file() or video_path.stat().st_size == 0:
        print(f"   ❌ Video was not written to {video_path}.")
        return False

    # cv2's mp4v writer uses MPEG-4 Part 2 (not H.264) and places the moov
    # atom at the end of the file. Browsers' <video> tag can't decode
    # MPEG-4 Part 2 at all (only used for the GStreamer/DL Streamer
    # pipeline), and GStreamer qtdemux only scans the first 10 MB, so it
    # fails to find a trailing moov. One ffmpeg pass fixes both: re-encode
    # to H.264 (universally playable, still fine for DL Streamer) with
    # +faststart (moov moved to the front for streaming). This same file is
    # used both for DL Streamer inference and for the browser Camera Preview
    # (so overlay timing always matches what was actually detected) — use a
    # near-lossless CRF so the re-encode doesn't perceptibly shift detection
    # confidence scores near threshold cutoffs.
    tmp_path = video_path.with_suffix(".faststart.mp4")
    try:
        ret = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path),
             "-c:v", "libx264", "-preset", "slow", "-crf", "15",
             "-pix_fmt", "yuv420p", "-an",
             "-movflags", "+faststart", str(tmp_path)],
            capture_output=True,
            check=False,
            shell=False,
        )
        if ret.returncode == 0:
            tmp_path.replace(video_path)
        else:
            tmp_path.unlink(missing_ok=True)
            print("   ⚠️  ffmpeg H.264/faststart repack failed — video may not stream correctly.")
            print(ret.stderr.decode(errors="replace")[-300:])
    except (FileNotFoundError, OSError):
        print("   ⚠️  ffmpeg not found — video may not stream correctly.")
        print("      Install ffmpeg to optimize video streaming.")

    print(f"   ✅ {video_path}  ({written} frames @ {fps} fps, {written/fps:.1f}s)")

    if manifest_path is not None:
        write_frame_manifest(written_files, manifest_path)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download and prepare a dataset for Agentic Predictive Maintenance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "dataset_url",
        help="URL of the dataset zip to download",
    )
    parser.add_argument(
        "--use-case",
        default="pipeline-defect-detection",
        choices=list(_USE_CASE_MAP),
        help="Use case name (default: pipeline-defect-detection)",
    )
    parser.add_argument(
        "--train-ratio",
        type=float,
        default=0.9,
        help="Fraction of data for training (default: 0.9)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed (default: 42)",
    )
    parser.add_argument(
        "--keep-download",
        action="store_true",
        help="Keep raw downloaded files after splitting",
    )

    args = parser.parse_args()

    use_case_id = _USE_CASE_MAP[args.use_case]
    output_dir  = Path("datasets") / use_case_id
    download_dir = output_dir / "_raw_download"
    app_dir_name = _APP_DIR_MAP[args.use_case]

    # Blueprint: video output goes directly into the DL Streamer resources dir
    video_output_path = Path("apps") / app_dir_name / "resources" / "videos" / "datastream.mp4"

    print(DISCLAIMER)
    print("=" * 70)
    print("Agentic Predictive Maintenance — Dataset Download & Prepare")
    print("=" * 70)
    print(f"  Use case:    {args.use_case}")
    print(f"  Dataset dir: {output_dir}")
    print(f"  Video →      {video_output_path}")
    print(f"  Train ratio: {args.train_ratio:.0%}")
    print(f"  Val ratio:   {1 - args.train_ratio:.0%}")
    print(f"  Seed:        {args.seed}")
    print()

    # ── Gas detection ─────────────────────────────────────────────────────────
    if use_case_id == "gas_detection":
        manifest_path = output_dir / "images" / "val" / "frame_manifest.json"
        if output_dir.exists() and (output_dir / "images" / "val").exists():
            print(f"✅ Dataset already at {output_dir} — skipping download.")
            if not video_output_path.exists() or not manifest_path.exists():
                print("🎬 Creating video + frame manifest from existing val images...")
                if not create_video_from_images(
                    output_dir / "images" / "val", video_output_path,
                    manifest_path=manifest_path,
                ):
                    sys.exit("❌ Failed to create the gas-detection video + frame manifest.")
            return

        download_dataset(args.dataset_url, download_dir, "gas-dataset.zip")
        n_train, n_val = prep_gas_detection(
            download_dir, output_dir, args.train_ratio, args.seed
        )

        if not args.keep_download:
            shutil.rmtree(download_dir, ignore_errors=True)

        # Create sample video (+ frame-index -> filename manifest for later
        # sensor-fusion re-joining) for DL Streamer inference.
        print("🎬 Creating video + frame manifest for DL Streamer inference...")
        if not create_video_from_images(
            output_dir / "images" / "val", video_output_path,
            manifest_path=manifest_path,
        ):
            sys.exit("❌ Failed to create the gas-detection video + frame manifest.")
        print()

        print("=" * 70)
        print("✅ Dataset ready!")
        print(f"  📁 {output_dir}/images/train/{{class}}/  ({n_train} images)")
        print(f"  📁 {output_dir}/images/val/             ({n_val} images)")
        print(f"  📁 {output_dir}/sensor_data/")
        print(f"  🎬 {video_output_path}  ← DL Streamer sample video")
        print(f"  🗺️  {manifest_path}  ← frame-index → filename manifest")
        print()
        return

    # ── Pipeline defect detection (default) ───────────────────────────────────
    if output_dir.exists() and (output_dir / "images").exists():
        print(f"✅ Dataset already at {output_dir} — skipping download.")
        # Still create video if missing
        if not video_output_path.exists():
            print("🎬 Creating video from existing val images...")
            create_video_from_images(output_dir / "images" / "val", video_output_path)
        return

    download_dataset(args.dataset_url, download_dir, "pipeline-defect-dataset.zip")

    print("🔍 Scanning for images and labels...")
    pairs = find_images_and_labels(download_dir)
    print(f"   Found {len(pairs)} images ({sum(1 for _, l in pairs if l)} with labels)\n")

    if not pairs:
        print("❌ No images found. Check the dataset URL and structure.")
        sys.exit(1)

    print(f"🔀 Splitting dataset (seed={args.seed})...")
    train_pairs, val_pairs = split_dataset(pairs, args.train_ratio, args.seed)
    print(f"   Train: {len(train_pairs)}  Val: {len(val_pairs)}\n")

    print("📂 Organising dataset...")
    copy_pairs(train_pairs,
               output_dir / "images" / "train", output_dir / "labels" / "train", "train")
    copy_pairs(val_pairs,
               output_dir / "images" / "val",   output_dir / "labels" / "val",   "val")
    print()

    # dataset.yaml for YOLO training
    dataset_yaml = output_dir / "dataset.yaml"
    dataset_yaml.write_text(
        f"path: {output_dir.resolve()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "names: [Deformation, Obstacle, Rupture, Disconnect, Misalignment, Deposition]\n"
    )
    print(f"📋 dataset.yaml written to {dataset_yaml}\n")

    # Create sample video for DL Streamer
    print("🎬 Creating sample video for DL Streamer inference...")
    create_video_from_images(output_dir / "images" / "val", video_output_path)
    print()

    if not args.keep_download:
        print("🧹 Cleaning up raw download...")
        shutil.rmtree(download_dir, ignore_errors=True)
        print("   ✅ Done\n")

    print("=" * 70)
    print("✅ Dataset ready!")
    print("=" * 70)
    print(f"  📁 {output_dir}/images/train/   ({len(train_pairs)} images)")
    print(f"  📁 {output_dir}/images/val/     ({len(val_pairs)} images)")
    print(f"  📁 {output_dir}/labels/")
    print(f"  📋 {dataset_yaml}")
    print(f"  🎬 {video_output_path}  ← DL Streamer sample video")
    print()
    print("Next steps:")
    print(f"  ./setup.sh --use-case {args.use_case}")
    print()


if __name__ == "__main__":
    main()
