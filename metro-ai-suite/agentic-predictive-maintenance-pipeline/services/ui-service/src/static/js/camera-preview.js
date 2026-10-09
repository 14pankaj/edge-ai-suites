// Copyright (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Keeps the Camera Preview player in sync with the "Video" dropdown in the
// Run Inspection form, and draws a bounding-box overlay on top of it using
// the stored detections (video_time_seconds + pixel bbox + label), synced to
// source-frame playback time.

(function () {
  "use strict";

  function init() {
    const select = document.getElementById("video-select");
    const player = document.getElementById("camera-preview-player");
    const canvas = document.getElementById("camera-preview-overlay");
    const wrap = document.getElementById("camera-preview-wrap");
    if (!player) return;

    if (select && player) {
      select.addEventListener("change", () => {
        const filename = select.value;
        if (!filename) return;
        player.src = `/media/videos/${encodeURIComponent(filename)}`;
        player.load();
      });
    }

    if (!canvas || !wrap) return; // overlay elements only exist on the dashboard

    const fps = parseFloat(wrap.dataset.videoFps) || 30;
    const ctx = canvas.getContext("2d");
    let detectionsByVideoTime = new Map();
    let sortedVideoTimes = [];

    fetch("/api/overlay/detections")
      .then((r) => (r.ok ? r.json() : []))
      .then((rows) => {
        detectionsByVideoTime = new Map();
        for (const d of rows) {
          if (d.video_time_seconds != null && Number.isFinite(Number(d.video_time_seconds))) {
            const videoTime = Number(d.video_time_seconds);
            if (!detectionsByVideoTime.has(videoTime)) detectionsByVideoTime.set(videoTime, []);
            detectionsByVideoTime.get(videoTime).push(d);
          }
        }
        sortedVideoTimes = [...detectionsByVideoTime.keys()].sort((a, b) => a - b);
      })
      .catch(() => {
        detectionsByVideoTime = new Map();
        sortedVideoTimes = [];
      });

    function boxesForVideoTime(videoTime) {
      let low = 0;
      let high = sortedVideoTimes.length;
      while (low < high) {
        const middle = (low + high) >>> 1;
        if (sortedVideoTimes[middle] < videoTime) low = middle + 1;
        else high = middle;
      }

      const candidates = [sortedVideoTimes[low - 1], sortedVideoTimes[low]]
        .filter((time) => time != null);
      if (!candidates.length) return null;
      const nearest = candidates.reduce((best, time) =>
        Math.abs(time - videoTime) < Math.abs(best - videoTime) ? time : best
      );
      return Math.abs(nearest - videoTime) <= 0.5 / fps
        ? detectionsByVideoTime.get(nearest)
        : null;
    }

    function sizeCanvasToVideo() {
      // Match the canvas's internal pixel buffer to the video's native
      // resolution so bbox pixel coordinates can be drawn 1:1 — CSS (100%
      // width/height) then scales the whole canvas down visually in lockstep
      // with the <video> element, same as the browser does for the video itself.
      if (player.videoWidth && player.videoHeight) {
        canvas.width = player.videoWidth;
        canvas.height = player.videoHeight;
      }
    }

    function drawBoxesForCurrentFrame(videoTime = player.currentTime) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (player.paused && player.ended) return;

      const boxes = boxesForVideoTime(videoTime);
      if (!boxes || !boxes.length) return;

      ctx.lineWidth = Math.max(2, canvas.width / 240);
      // #22c55e matches the ui-ux-builder skill's Stack C "active" status color
      // (design-tokens.md), reused here for detected bounding boxes.
      ctx.strokeStyle = "#22c55e";
      ctx.fillStyle = "#22c55e";
      ctx.font = `${Math.max(14, Math.round(canvas.width / 45))}px sans-serif`;
      ctx.textBaseline = "bottom";

      for (const box of boxes) {
        const { x, y, width, height, label, confidence } = box;
        if ([x, y, width, height].some((v) => typeof v !== "number")) continue;
        // Whole-frame classification results (e.g. gas-detection's multimodal
        // runner) always post width=height=0 — there's no real bbox, so skip
        // drawing a meaningless zero-size mark at the corner.
        if (width <= 0 || height <= 0) continue;
        ctx.strokeRect(x, y, width, height);
        const text = confidence != null ? `${label} ${(confidence * 100).toFixed(0)}%` : String(label);
        const textY = y > 16 ? y - 4 : y + 16;
        ctx.fillText(text, x, textY);
      }
    }

    // requestVideoFrameCallback fires once per actually-rendered video frame
    // (frame-accurate); fall back to a rAF poll loop on browsers without it
    // (e.g. older Firefox) so the overlay still tracks, just less precisely.
    if (typeof player.requestVideoFrameCallback === "function") {
      const onFrame = (_now, metadata) => {
        drawBoxesForCurrentFrame(metadata?.mediaTime);
        player.requestVideoFrameCallback(onFrame);
      };
      player.requestVideoFrameCallback(onFrame);
    } else {
      const pollLoop = () => {
        drawBoxesForCurrentFrame();
        requestAnimationFrame(pollLoop);
      };
      requestAnimationFrame(pollLoop);
    }

    player.addEventListener("loadedmetadata", sizeCanvasToVideo);
    player.addEventListener("seeked", () => drawBoxesForCurrentFrame());
    window.addEventListener("resize", sizeCanvasToVideo);
    sizeCanvasToVideo();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
