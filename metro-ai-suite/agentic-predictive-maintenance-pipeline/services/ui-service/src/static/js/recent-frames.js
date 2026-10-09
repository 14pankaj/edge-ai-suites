// Copyright (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Renders the dashboard's "Recent Defect Frames" gallery. There is no
// per-frame image stored server-side (storage-service only persists bbox +
// video_time_seconds + label/confidence per detection), so each thumbnail is
// produced client-side: a hidden <video> (same source as Camera Preview) is
// seeked to the detection's video_time_seconds, the resulting frame is drawn
// onto a small <canvas> together with its bounding box, and captioned with
// the real frame_id/label/confidence from /api/recent-frames.

(function () {
  "use strict";

  const THUMB_WIDTH = 150;
  const THUMB_HEIGHT = 90;

  function init() {
    const strip = document.getElementById("recent-frame-strip");
    const player = document.getElementById("camera-preview-player");
    const previewWrap = document.getElementById("camera-preview-wrap");
    const emptyNote = document.getElementById("recent-frames-empty");
    if (!strip || !player) return;

    const limit = parseInt(strip.dataset.limit, 10) || 6;
    const videoSrc = player.getAttribute("src");

    fetch(`/api/recent-frames?limit=${limit}`)
      .then((r) => (r.ok ? r.json() : []))
      .then((frames) => render(Array.isArray(frames) ? frames : []))
      .catch(() => render([]));

    function render(frames) {
      if (!frames.length || !videoSrc) {
        if (emptyNote) emptyNote.hidden = false;
        return;
      }
      if (emptyNote) emptyNote.hidden = true;

      // One offscreen <video> reused serially across frames — seeking is
      // async, so thumbnails are built one at a time to avoid racing seeks
      // on the same element.
      const grabber = document.createElement("video");
      grabber.muted = true;
      grabber.preload = "auto";
      grabber.src = videoSrc;

      const thumbs = frames.map((frame) => {
        const el = document.createElement("div");
        el.className = "frame-thumb frame-thumb-loading";
        el.tabIndex = 0;
        el.setAttribute("role", "button");
        el.setAttribute("aria-pressed", "false");
        el.title = "Click to jump the Camera Preview to this frame";
        el.addEventListener("click", () => selectFrame(frame, el));
        el.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            selectFrame(frame, el);
          }
        });
        strip.appendChild(el);
        return el;
      });

      grabber.addEventListener("loadedmetadata", () => {
        buildThumbnailsSequentially(grabber, frames, thumbs, 0);
      });
    }

    // Clicking a thumbnail seeks the (already-visible) Camera Preview player
    // to that detection's video_time_seconds and pauses on that exact frame
    // — camera-preview.js's own "seeked" listener redraws the bbox overlay
    // for us, so no overlay logic is duplicated here. Re-clicking a thumbnail
    // (same or different) just re-seeks/switches the highlight. There is no
    // explicit "clear selection" control: the player's own Play control is
    // always live, so pressing Play simply resumes playback from that point
    // and the highlighted thumbnail is understood to no longer reflect the
    // live position — no extra UI is needed for that.
    function selectFrame(frame, el) {
      strip.querySelectorAll(".frame-thumb-active").forEach((other) => {
        other.classList.remove("frame-thumb-active");
        other.setAttribute("aria-pressed", "false");
      });

      el.classList.add("frame-thumb-active");
      el.setAttribute("aria-pressed", "true");

      const time = Number(frame.video_time_seconds);
      if (Number.isFinite(time)) {
        player.pause();
        player.currentTime = time;
      }

      if (previewWrap) {
        previewWrap.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }

    function buildThumbnailsSequentially(grabber, frames, thumbs, index) {
      if (index >= frames.length) return;
      const frame = frames[index];
      const time = Number(frame.video_time_seconds);

      const onSeeked = () => {
        grabber.removeEventListener("seeked", onSeeked);
        drawThumbnail(grabber, frame, thumbs[index]);
        buildThumbnailsSequentially(grabber, frames, thumbs, index + 1);
      };

      if (Number.isFinite(time) && grabber.duration && time <= grabber.duration) {
        grabber.addEventListener("seeked", onSeeked);
        grabber.currentTime = time;
      } else {
        // No usable timestamp for this detection — leave a caption-only card.
        renderCaptionOnly(frame, thumbs[index]);
        buildThumbnailsSequentially(grabber, frames, thumbs, index + 1);
      }
    }

    function drawThumbnail(grabber, frame, container) {
      container.classList.remove("frame-thumb-loading");
      container.innerHTML = "";

      const canvas = document.createElement("canvas");
      canvas.width = THUMB_WIDTH;
      canvas.height = THUMB_HEIGHT;
      const ctx = canvas.getContext("2d");

      const vw = grabber.videoWidth || THUMB_WIDTH;
      const vh = grabber.videoHeight || THUMB_HEIGHT;
      ctx.drawImage(grabber, 0, 0, vw, vh, 0, 0, THUMB_WIDTH, THUMB_HEIGHT);

      const { x, y, width, height, label, confidence } = frame;
      if ([x, y, width, height].every((v) => typeof v === "number") && width > 0 && height > 0) {
        const scaleX = THUMB_WIDTH / vw;
        const scaleY = THUMB_HEIGHT / vh;
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#22c55e"; // matches camera-preview.js / skill's Stack C "active" status color
        ctx.strokeRect(x * scaleX, y * scaleY, width * scaleX, height * scaleY);
      }

      container.appendChild(canvas);
      container.appendChild(buildCaption(frame));
    }

    function renderCaptionOnly(frame, container) {
      container.classList.remove("frame-thumb-loading");
      container.innerHTML = "";
      container.appendChild(buildCaption(frame));
    }

    function buildCaption(frame) {
      const caption = document.createElement("div");
      caption.className = "frame-thumb-caption";
      const confidencePct = typeof frame.confidence === "number"
        ? `${(frame.confidence * 100).toFixed(0)}%`
        : "—";
      const timeLabel = typeof frame.video_time_seconds === "number"
        ? `${frame.video_time_seconds.toFixed(1)}s`
        : "—";
      caption.innerHTML =
        `<div class="frame-thumb-row">` +
          `<span class="frame-thumb-id">#${frame.frame_id}</span>` +
          `<span class="label-badge">${frame.label}</span>` +
        `</div>` +
        `<div class="frame-thumb-row frame-thumb-row-muted">` +
          `<span class="frame-thumb-confidence">${confidencePct}</span>` +
          `<span class="frame-thumb-time" title="Video timestamp">${timeLabel}</span>` +
        `</div>`;
      return caption;
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
