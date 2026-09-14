document.addEventListener('DOMContentLoaded', () => {
  // State
  let detections = [];
  let currentFilterTier = 'all';
  let searchQuery = '';
  let pollInterval = null;
  let currentMediaDuration = 0;
  let selectedDetection = null;

  // DOM Elements
  const videoPathInput = document.getElementById('videoPathInput');
  const srtPathInput = document.getElementById('srtPathInput');
  const modeSelect = document.getElementById('modeSelect');
  const presetSelect = document.getElementById('presetSelect');
  const scanBtn = document.getElementById('scanBtn');
  const scanStatusText = document.getElementById('scanStatusText');
  const workspace = document.getElementById('workspace');

  // Stats
  const statDuration = document.getElementById('statDuration');
  const statAudioLayout = document.getElementById('statAudioLayout');
  const statSurroundBadge = document.getElementById('statSurroundBadge');
  const statCount = document.getElementById('statCount');
  const statMuteDuration = document.getElementById('statMuteDuration');

  // Player & Table
  const previewPlayer = document.getElementById('previewPlayer');
  const playSelectedBtn = document.getElementById('playSelectedBtn');
  const playerTimecode = document.getElementById('playerTimecode');
  const detectionsTbody = document.getElementById('detectionsTbody');
  const activeCountSpan = document.getElementById('activeCount');
  const masterCheckbox = document.getElementById('masterCheckbox');
  const selectAllBtn = document.getElementById('selectAllBtn');
  const deselectAllBtn = document.getElementById('deselectAllBtn');
  const tableFilterInput = document.getElementById('tableFilterInput');
  const tierPills = document.querySelectorAll('.tier-pills .pill');

  // Output & Actions
  const outputVideoInput = document.getElementById('outputVideoInput');
  const centerOnlyToggle = document.getElementById('centerOnlyToggle');
  const cleanSrtToggle = document.getElementById('cleanSrtToggle');
  const wordTimingToggle = document.getElementById('wordTimingToggle');
  const startProcessBtn = document.getElementById('startProcessBtn');
  const renderBtnCount = document.getElementById('renderBtnCount');

  // Progress Modal
  const progressModal = document.getElementById('progressModal');
  const progressTitle = document.getElementById('progressTitle');
  const progressSubtitle = document.getElementById('progressSubtitle');
  const progressBar = document.getElementById('progressBar');
  const progressPercent = document.getElementById('progressPercent');
  const progressTime = document.getElementById('progressTime');
  const progressCompletionActions = document.getElementById('progressCompletionActions');
  const progressErrorMsg = document.getElementById('progressErrorMsg');
  const cancelProcessBtn = document.getElementById('cancelProcessBtn');
  const revealFinderBtn = document.getElementById('revealFinderBtn');
  const previewCleanedBtn = document.getElementById('previewCleanedBtn');
  const closeProgressBtn = document.getElementById('closeProgressBtn');

  // Browser Modal
  const browserModal = document.getElementById('browserModal');
  const closeBrowserBtn = document.getElementById('closeBrowserBtn');
  const browserCurrentPath = document.getElementById('browserCurrentPath');
  const browserList = document.getElementById('browserList');
  const browserUpBtn = document.getElementById('browserUpBtn');
  const browseVideoBtn = document.getElementById('browseVideoBtn');
  const browseSrtBtn = document.getElementById('browseSrtBtn');
  let browseTargetInput = null;
  let browserCurrentDir = '';

  // Helpers
  function formatSeconds(secs) {
    if (!secs || isNaN(secs)) return '00:00:00';
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = Math.floor(secs % 60);
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }

  function escapeHtml(str) {
    if (!str) return '';
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // File Upload Elements
  const videoFileInput = document.getElementById('videoFileInput');
  const srtFileInput = document.getElementById('srtFileInput');
  const pickVideoFileBtn = document.getElementById('pickVideoFileBtn');
  const pickSrtFileBtn = document.getElementById('pickSrtFileBtn');
  const browserBookmarks = document.getElementById('browserBookmarks');

  // Upload implementation
  function uploadFile(file, isVideo) {
    const progressBox = document.getElementById(isVideo ? 'videoUploadProgress' : 'srtUploadProgress');
    const fill = document.getElementById(isVideo ? 'videoUploadFill' : 'srtUploadFill');
    const text = document.getElementById(isVideo ? 'videoUploadText' : 'srtUploadText');
    const input = isVideo ? videoPathInput : srtPathInput;

    progressBox.classList.remove('hidden');
    fill.style.width = '0%';
    text.textContent = 'Uploading: 0%';

    const xhr = new XMLHttpRequest();
    const url = `/api/upload?filename=${encodeURIComponent(file.name)}`;
    xhr.open('POST', url, true);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        fill.style.width = `${pct}%`;
        text.textContent = `Uploading: ${pct}%`;
      }
    };

    xhr.onload = () => {
      if (xhr.status === 200) {
        const res = JSON.parse(xhr.responseText);
        input.value = res.path;
        fill.style.width = '100%';
        text.textContent = `✓ Uploaded: ${file.name}`;
        setTimeout(() => progressBox.classList.add('hidden'), 3500);
      } else {
        text.textContent = 'Upload failed';
      }
    };

    xhr.onerror = () => {
      text.textContent = 'Upload error';
    };

    xhr.send(file);
  }

  // Setup Drag & Drop and Upload
  function setupDropZone(dropZone, inputElement, fileInput, isVideo) {
    ['dragenter', 'dragover'].forEach(name => {
      dropZone.addEventListener(name, (e) => {
        e.preventDefault();
        dropZone.classList.add('dragover');
      });
    });
    ['dragleave', 'drop'].forEach(name => {
      dropZone.addEventListener(name, (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
      });
    });
    dropZone.addEventListener('drop', (e) => {
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        const file = e.dataTransfer.files[0];
        if (file.path) {
          inputElement.value = file.path;
        } else {
          uploadFile(file, isVideo);
        }
      }
    });

    // Clicking the drop zone opens the file picker
    dropZone.addEventListener('click', (e) => {
      if (!e.target.closest('.upload-progress-box')) {
        fileInput.click();
      }
    });
  }

  setupDropZone(document.getElementById('videoDropZone'), videoPathInput, videoFileInput, true);
  setupDropZone(document.getElementById('srtDropZone'), srtPathInput, srtFileInput, false);

  if (pickVideoFileBtn) {
    pickVideoFileBtn.addEventListener('click', () => videoFileInput.click());
  }
  if (pickSrtFileBtn) {
    pickSrtFileBtn.addEventListener('click', () => srtFileInput.click());
  }

  videoFileInput.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length > 0) {
      uploadFile(e.target.files[0], true);
    }
  });

  srtFileInput.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length > 0) {
      uploadFile(e.target.files[0], false);
    }
  });

  // Timing & Sync Elements
  const timingPresetSelect = document.getElementById('timingPresetSelect');
  const syncOffsetInput = document.getElementById('syncOffsetInput');
  const syncMinusHalfBtn = document.getElementById('syncMinusHalfBtn');
  const syncMinusTenthBtn = document.getElementById('syncMinusTenthBtn');
  const syncPlusTenthBtn = document.getElementById('syncPlusTenthBtn');
  const syncPlusHalfBtn = document.getElementById('syncPlusHalfBtn');

  function adjustSync(delta) {
    let val = parseFloat(syncOffsetInput.value) || 0.0;
    val = Math.round((val + delta) * 10) / 10;
    syncOffsetInput.value = val.toFixed(1);
  }
  if (syncMinusHalfBtn) syncMinusHalfBtn.addEventListener('click', () => adjustSync(-0.5));
  if (syncMinusTenthBtn) syncMinusTenthBtn.addEventListener('click', () => adjustSync(-0.2));
  if (syncPlusTenthBtn) syncPlusTenthBtn.addEventListener('click', () => adjustSync(+0.2));
  if (syncPlusHalfBtn) syncPlusHalfBtn.addEventListener('click', () => adjustSync(+0.5));

  // Timecode update in player
  previewPlayer.addEventListener('timeupdate', () => {
    playerTimecode.textContent = formatSeconds(previewPlayer.currentTime);
  });

  // Scan Media
  scanBtn.addEventListener('click', async () => {
    const videoPath = videoPathInput.value.trim();
    const srtPath = srtPathInput.value.trim();

    if (!videoPath) {
      alert('Please enter or select a video file path.');
      return;
    }

    scanStatusText.textContent = 'Scanning media & inspecting subtitles...';
    scanBtn.disabled = true;

    // Calculate timing parameters
    const presetVal = timingPresetSelect ? timingPresetSelect.value : 'safe';
    let padBefore = 0.35, padAfter = 0.35, leadIn = 0.25, timingMode = 'word';
    if (presetVal === 'safe') {
      padBefore = 0.35; padAfter = 0.35; leadIn = 0.25; timingMode = 'word';
    } else if (presetVal === 'standard') {
      padBefore = 0.25; padAfter = 0.25; leadIn = 0.15; timingMode = 'word';
    } else if (presetVal === 'tight') {
      padBefore = 0.15; padAfter = 0.15; leadIn = 0.05; timingMode = 'word';
    } else if (presetVal === 'segment') {
      padBefore = 0.20; padAfter = 0.20; leadIn = 0.0; timingMode = 'segment';
    }
    const syncOffset = syncOffsetInput ? (parseFloat(syncOffsetInput.value) || 0.0) : 0.0;

    try {
      const resp = await fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_path: videoPath,
          srt_path: srtPath,
          preset: presetSelect.value,
          timing_mode: timingMode,
          padding_before: padBefore,
          padding_after: padAfter,
          lead_in: leadIn,
          sync_offset: syncOffset
        })
      });

      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || 'Failed to scan media');
      }

      // Populate UI
      detections = data.detections || [];
      currentMediaDuration = data.duration || 0;
      outputVideoInput.value = data.suggested_output || '';

      // Update Subtitle input if auto-detected
      if (data.srt_path && !srtPathInput.value) {
        srtPathInput.value = data.srt_path;
      }

      // Update Stats Banner
      statDuration.textContent = formatSeconds(currentMediaDuration);
      statAudioLayout.textContent = data.channel_layout || 'Stereo';
      statSurroundBadge.textContent = data.is_surround ? '5.1 / 7.1' : 'Stereo';
      statSurroundBadge.className = data.is_surround ? 'badge badge-strict' : 'badge badge-info';
      centerOnlyToggle.checked = data.is_surround;

      // Mount Video Player Stream
      previewPlayer.src = `/api/stream?path=${encodeURIComponent(videoPath)}`;
      previewPlayer.load();

      // Show Workspace
      workspace.classList.remove('hidden');
      scanStatusText.textContent = `Scan complete: ${detections.length} profanities found.`;

      renderTable();
      updateCounters();
    } catch (err) {
      alert(`Scan Error: ${err.message}`);
      scanStatusText.textContent = '';
    } finally {
      scanBtn.disabled = false;
    }
  });

  // Render Table
  function renderTable() {
    detectionsTbody.innerHTML = '';

    const filtered = detections.filter(d => {
      const matchesTier = (currentFilterTier === 'all') || (d.tier === currentFilterTier);
      const matchesSearch = !searchQuery || 
        d.word.toLowerCase().includes(searchQuery.toLowerCase()) || 
        d.context_text.toLowerCase().includes(searchQuery.toLowerCase());
      return matchesTier && matchesSearch;
    });

    if (filtered.length === 0) {
      detectionsTbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 24px;">No matching profanities found.</td></tr>`;
      return;
    }

    filtered.forEach(d => {
      const tr = document.createElement('tr');
      if (selectedDetection && selectedDetection.id === d.id) {
        tr.style.backgroundColor = 'rgba(56, 189, 248, 0.08)';
      }

      // Highlight profanity in context text
      const regex = new RegExp(`\\b(${escapeHtml(d.word)})\\b`, 'gi');
      const highlightedContext = escapeHtml(d.context_text).replace(regex, '<mark>$1</mark>');

      tr.innerHTML = `
        <td><input type="checkbox" class="row-checkbox" data-id="${d.id}" ${d.enabled ? 'checked' : ''} /></td>
        <td>
          <button class="time-btn" data-time="${d.mute_start}">${formatSeconds(d.mute_start)}</button>
          <div style="margin-top: 4px; display: flex; gap: 2px;">
            <button class="nudge-btn nudge-earlier" title="Start mute 0.2s earlier (prevents missing start of word)">◀ Earlier</button>
            <button class="nudge-btn nudge-later" title="Extend mute 0.2s later">Later ▶</button>
          </div>
        </td>
        <td><span class="badge badge-${d.tier}">${escapeHtml(d.word)}</span></td>
        <td><span style="color: var(--text-muted); text-transform: capitalize;">${d.tier}</span></td>
        <td>${highlightedContext}</td>
        <td><button class="btn-xs test-mute-btn" title="Listen to this exact word muted in preview player">🎧 Test Mute</button></td>
      `;

      // Checkbox event
      tr.querySelector('.row-checkbox').addEventListener('change', (e) => {
        d.enabled = e.target.checked;
        updateCounters();
      });

      // Timecode click: jump player and play
      tr.querySelector('.time-btn').addEventListener('click', () => {
        selectedDetection = d;
        previewPlayer.currentTime = Math.max(0, d.mute_start - 0.5);
        previewPlayer.play();
        renderTable();
      });

      // Nudge Earlier
      tr.querySelector('.nudge-earlier').addEventListener('click', (e) => {
        e.stopPropagation();
        d.mute_start = Math.max(0, Math.round((d.mute_start - 0.2) * 100) / 100);
        renderTable();
        updateCounters();
      });

      // Nudge Later
      tr.querySelector('.nudge-later').addEventListener('click', (e) => {
        e.stopPropagation();
        d.mute_end = Math.round((d.mute_end + 0.2) * 100) / 100;
        renderTable();
        updateCounters();
      });

      // Test Mute with live audio ducking/muting on preview player
      tr.querySelector('.test-mute-btn').addEventListener('click', () => {
        selectedDetection = d;
        previewPlayer.currentTime = Math.max(0, d.mute_start - 1.0);
        previewPlayer.play();

        const muteChecker = () => {
          if (previewPlayer.paused || previewPlayer.ended) {
            previewPlayer.removeEventListener('timeupdate', muteChecker);
            previewPlayer.muted = false;
            return;
          }
          const t = previewPlayer.currentTime;
          if (t >= d.mute_start && t <= d.mute_end) {
            previewPlayer.muted = true;
          } else {
            previewPlayer.muted = false;
          }
          if (t > d.mute_end + 1.2) {
            previewPlayer.removeEventListener('timeupdate', muteChecker);
            previewPlayer.muted = false;
          }
        };
        previewPlayer.addEventListener('timeupdate', muteChecker);
        renderTable();
      });

      detectionsTbody.appendChild(tr);
    });
  }

  function updateCounters() {
    const active = detections.filter(d => d.enabled);
    statCount.textContent = detections.length;
    activeCountSpan.textContent = active.length;
    renderBtnCount.textContent = active.length;

    let totalMuteSecs = 0;
    active.forEach(d => {
      totalMuteSecs += Math.max(0, d.mute_end - d.mute_start);
    });
    statMuteDuration.textContent = `${totalMuteSecs.toFixed(1)}s`;

    masterCheckbox.checked = active.length === detections.length && detections.length > 0;
  }

  // Master Checkbox
  masterCheckbox.addEventListener('change', () => {
    detections.forEach(d => d.enabled = masterCheckbox.checked);
    renderTable();
    updateCounters();
  });

  selectAllBtn.addEventListener('click', () => {
    detections.forEach(d => d.enabled = true);
    renderTable();
    updateCounters();
  });

  deselectAllBtn.addEventListener('click', () => {
    detections.forEach(d => d.enabled = false);
    renderTable();
    updateCounters();
  });

  // Filter Input
  tableFilterInput.addEventListener('input', (e) => {
    searchQuery = e.target.value.trim();
    renderTable();
  });

  // Tier Pills
  tierPills.forEach(pill => {
    pill.addEventListener('click', () => {
      tierPills.forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      currentFilterTier = pill.dataset.filter;
      renderTable();
    });
  });

  // Play Selected Word Clip
  playSelectedBtn.addEventListener('click', () => {
    if (!selectedDetection && detections.length > 0) {
      selectedDetection = detections[0];
    }
    if (selectedDetection) {
      previewPlayer.currentTime = Math.max(0, selectedDetection.mute_start - 1.0);
      previewPlayer.play();
    }
  });

  // Start Processing
  startProcessBtn.addEventListener('click', async () => {
    const videoPath = videoPathInput.value.trim();
    const outputPath = outputVideoInput.value.trim();
    const srtPath = srtPathInput.value.trim();

    if (!outputPath) {
      alert('Please specify an output video destination.');
      return;
    }

    const enabledDetections = detections.filter(d => d.enabled);
    if (enabledDetections.length === 0) {
      if (!confirm('No profanities are selected to mute. Export video anyway?')) {
        return;
      }
    }

    try {
      const resp = await fetch('/api/process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_path: videoPath,
          output_path: outputPath,
          srt_path: srtPath,
          mode: modeSelect.value,
          center_channel_only: centerOnlyToggle.checked,
          generate_clean_srt: cleanSrtToggle.checked,
          detections: detections
        })
      });

      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || 'Failed to start processing');
      }

      // Show Progress Modal
      progressModal.classList.remove('hidden');
      progressTitle.textContent = 'Censoring Media...';
      progressSubtitle.textContent = 'Losslessly copying video stream & processing audio...';
      progressCompletionActions.classList.add('hidden');
      progressErrorMsg.classList.add('hidden');
      cancelProcessBtn.classList.remove('hidden');
      progressBar.style.width = '0%';
      progressPercent.textContent = '0.0%';

      startProgressPolling();
    } catch (err) {
      alert(`Error starting censorship: ${err.message}`);
    }
  });

  function startProgressPolling() {
    if (pollInterval) clearInterval(pollInterval);

    pollInterval = setInterval(async () => {
      try {
        const resp = await fetch('/api/progress');
        const data = await resp.json();

        if (data.status === 'processing') {
          const pct = data.percent || 0;
          progressBar.style.width = `${pct}%`;
          progressPercent.textContent = `${pct.toFixed(1)}%`;
          progressTime.textContent = `${formatSeconds(data.current_time)} / ${formatSeconds(data.total_duration)}`;
        } else if (data.status === 'completed') {
          clearInterval(pollInterval);
          progressBar.style.width = '100%';
          progressPercent.textContent = '100%';
          progressTitle.textContent = '🎉 Processing Complete!';
          progressSubtitle.textContent = `Saved to: ${data.output_path}`;
          cancelProcessBtn.classList.add('hidden');
          progressCompletionActions.classList.remove('hidden');

          // Hook completion buttons
          revealFinderBtn.onclick = () => {
            fetch('/api/reveal', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ path: data.output_path })
            });
          };

          previewCleanedBtn.onclick = () => {
            progressModal.classList.add('hidden');
            previewPlayer.src = `/api/stream?path=${encodeURIComponent(data.output_path)}`;
            previewPlayer.play();
          };
        } else if (data.status === 'failed') {
          clearInterval(pollInterval);
          progressTitle.textContent = 'Processing Failed';
          progressSubtitle.textContent = data.error || 'An error occurred during FFmpeg execution';
          progressErrorMsg.textContent = data.error || 'Unknown error';
          progressErrorMsg.classList.remove('hidden');
        }
      } catch (e) {
        // network glitch
      }
    }, 400);
  }

  cancelProcessBtn.addEventListener('click', async () => {
    if (confirm('Cancel video processing?')) {
      await fetch('/api/cancel', { method: 'POST' });
      clearInterval(pollInterval);
      progressModal.classList.add('hidden');
    }
  });

  closeProgressBtn.addEventListener('click', () => {
    progressModal.classList.add('hidden');
  });

  // Local File Browser
  async function loadBrowserDir(path) {
    try {
      const resp = await fetch(`/api/browse?dir=${encodeURIComponent(path || '')}`);
      const data = await resp.json();
      browserCurrentDir = data.current_dir;
      browserCurrentPath.textContent = browserCurrentDir;
      browserList.innerHTML = '';

      if (browserBookmarks && data.bookmarks) {
        browserBookmarks.innerHTML = '';
        data.bookmarks.forEach(bm => {
          const btn = document.createElement('button');
          btn.className = 'bookmark-btn';
          btn.textContent = bm.name;
          btn.onclick = () => loadBrowserDir(bm.path);
          browserBookmarks.appendChild(btn);
        });
      }

      browserUpBtn.onclick = () => {
        if (data.parent_dir) loadBrowserDir(data.parent_dir);
      };

      data.items.forEach(item => {
        const div = document.createElement('div');
        div.className = 'browser-item';
        div.innerHTML = `<span>${item.is_dir ? '📁' : (item.is_video ? '🎬' : '📄')}</span> <span>${escapeHtml(item.name)}</span>`;
        div.addEventListener('click', () => {
          if (item.is_dir) {
            loadBrowserDir(item.path);
          } else {
            if (browseTargetInput) {
              browseTargetInput.value = item.path;
            }
            browserModal.classList.add('hidden');
          }
        });
        browserList.appendChild(div);
      });
    } catch (err) {
      browserList.innerHTML = `<div style="padding: 12px; color: var(--tier-strict);">Failed to load directory</div>`;
    }
  }

  browseVideoBtn.addEventListener('click', () => {
    browseTargetInput = videoPathInput;
    browserModal.classList.remove('hidden');
    loadBrowserDir(browserCurrentDir || '');
  });

  browseSrtBtn.addEventListener('click', () => {
    browseTargetInput = srtPathInput;
    browserModal.classList.remove('hidden');
    loadBrowserDir(browserCurrentDir || '');
  });

  closeBrowserBtn.addEventListener('click', () => {
    browserModal.classList.add('hidden');
  });
});
