document.addEventListener('DOMContentLoaded', () => {
  // State
  let detections = [];
  let currentFilterTier = 'all';
  let searchQuery = '';
  let pollInterval = null;
  let currentMediaDuration = 0;
  let selectedDetection = null;
  let activeMuteChecker = null;
  let activeUploads = 0;
  let lastScannedVideoPath = '';
  let isProcessingJob = false;
  let syncRescanTimer = null;

  // DOM Elements
  const ffmpegWarningBanner = document.getElementById('ffmpegWarningBanner');
  const ffmpegWarningText = document.getElementById('ffmpegWarningText');
  const videoPathInput = document.getElementById('videoPathInput');
  const srtPathInput = document.getElementById('srtPathInput');
  const modeSelect = document.getElementById('modeSelect');
  const presetSelect = document.getElementById('presetSelect');
  const customWordsInput = document.getElementById('customWordsInput');
  const scanBtn = document.getElementById('scanBtn');
  const scanStatusText = document.getElementById('scanStatusText');
  const scanErrorBanner = document.getElementById('scanErrorBanner');
  const scanErrorText = document.getElementById('scanErrorText');
  const scanErrorSearchSubsBtn = document.getElementById('scanErrorSearchSubsBtn');
  const workspace = document.getElementById('workspace');
  const playerCompatNotice = document.getElementById('playerCompatNotice');
  const stopStudioBtn = document.getElementById('stopStudioBtn');

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
  const searchOnlineSubsBtn = document.getElementById('searchOnlineSubsBtn');
  const subtitleSearchDialog = document.getElementById('subtitleSearchDialog');
  const subtitleSearchForm = document.getElementById('subtitleSearchForm');
  const subSearchTitle = document.getElementById('subSearchTitle');
  const subSearchYear = document.getElementById('subSearchYear');
  const subSearchSeason = document.getElementById('subSearchSeason');
  const subSearchEpisode = document.getElementById('subSearchEpisode');
  const subSearchImdb = document.getElementById('subSearchImdb');
  const subSearchLang = document.getElementById('subSearchLang');
  const runSubSearchBtn = document.getElementById('runSubSearchBtn');
  const subSearchStatus = document.getElementById('subSearchStatus');
  const subtitleResultsList = document.getElementById('subtitleResultsList');
  const subOsApiKeyInput = document.getElementById('subOsApiKeyInput');
  const subOsUserInput = document.getElementById('subOsUserInput');
  const subOsPassInput = document.getElementById('subOsPassInput');
  const subDlApiKeyInput = document.getElementById('subDlApiKeyInput');
  const saveSubKeysBtn = document.getElementById('saveSubKeysBtn');
  const subKeysStatus = document.getElementById('subKeysStatus');
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

  function escapeRegExp(str) {
    return String(str || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function clearTestMute() {
    if (activeMuteChecker) {
      previewPlayer.removeEventListener('timeupdate', activeMuteChecker);
      activeMuteChecker = null;
    }
    previewPlayer.muted = false;
  }

  function parseCustomWords() {
    if (!customWordsInput) return [];
    return customWordsInput.value
      .split(',')
      .map((w) => w.trim())
      .filter(Boolean);
  }

  function clearScanError() {
    if (scanErrorBanner) scanErrorBanner.classList.add('hidden');
    if (scanErrorText) scanErrorText.textContent = '';
    if (scanErrorSearchSubsBtn) scanErrorSearchSubsBtn.classList.add('hidden');
  }

  function showScanError(message, showSearchAction = false) {
    if (!scanErrorBanner || !scanErrorText) return;
    scanErrorText.textContent = message;
    if (scanErrorSearchSubsBtn) {
      scanErrorSearchSubsBtn.classList.toggle('hidden', !showSearchAction);
    }
    scanErrorBanner.classList.remove('hidden');
  }

  if (scanErrorSearchSubsBtn && searchOnlineSubsBtn) {
    scanErrorSearchSubsBtn.addEventListener('click', () => {
      searchOnlineSubsBtn.click();
    });
  }

  function playPreviewSafely() {
    const playPromise = previewPlayer.play();
    if (playPromise && typeof playPromise.catch === 'function') {
      playPromise.catch(() => {
        if (previewPlayer.error && playerCompatNotice) {
          playerCompatNotice.classList.remove('hidden');
        }
      });
    }
  }

  function onVideoSelectionChanged() {
    if (srtPathInput) srtPathInput.value = '';
    if (subSearchTitle) subSearchTitle.value = '';
    if (subSearchYear) subSearchYear.value = '';
    if (subSearchSeason) subSearchSeason.value = '';
    if (subSearchEpisode) subSearchEpisode.value = '';
    if (subSearchImdb) subSearchImdb.value = '';
    clearScanError();
    if (playerCompatNotice) playerCompatNotice.classList.add('hidden');
    scanStatusText.textContent = '';
    detections = [];
    selectedDetection = null;
    lastScannedVideoPath = '';
    clearTestMute();
    previewPlayer.removeAttribute('src');
    previewPlayer.load();
    workspace.classList.add('hidden');
  }

  function triggerRescanIfActive(debounced = false) {
    if (workspace.classList.contains('hidden') || !videoPathInput.value.trim() || scanBtn.disabled) {
      return;
    }
    if (syncRescanTimer) {
      clearTimeout(syncRescanTimer);
      syncRescanTimer = null;
    }
    if (debounced) {
      syncRescanTimer = setTimeout(() => {
        syncRescanTimer = null;
        if (!workspace.classList.contains('hidden') && videoPathInput.value.trim() && !scanBtn.disabled) {
          scanBtn.click();
        }
      }, 350);
    } else {
      scanBtn.click();
    }
  }

  function appendHighlightedContext(container, contextText, word) {
    const text = String(contextText || '');
    const safeWord = String(word || '').trim();
    if (!safeWord) {
      container.appendChild(document.createTextNode(text));
      return;
    }
    const escaped = escapeRegExp(safeWord);
    const left = /^\w/.test(safeWord) ? '\\b' : '';
    const right = /\w$/.test(safeWord) ? '\\b' : '';
    const regex = new RegExp(`${left}(${escaped})${right}`, 'gi');
    let lastIndex = 0;
    let match;
    while ((match = regex.exec(text)) !== null) {
      if (match.index > lastIndex) {
        container.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
      }
      const mark = document.createElement('mark');
      mark.textContent = match[0];
      container.appendChild(mark);
      lastIndex = regex.lastIndex;
      if (match[0].length === 0) break;
    }
    if (lastIndex < text.length) {
      container.appendChild(document.createTextNode(text.slice(lastIndex)));
    }
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

    activeUploads += 1;
    scanBtn.disabled = true;
    scanStatusText.textContent = 'Uploading file...';

    let finished = false;
    const finishUpload = () => {
      if (finished) return;
      finished = true;
      activeUploads = Math.max(0, activeUploads - 1);
      if (activeUploads === 0) {
        scanBtn.disabled = false;
      }
    };

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
      finishUpload();
      if (xhr.status === 200) {
        const res = JSON.parse(xhr.responseText);
        if (isVideo) {
          onVideoSelectionChanged();
        } else {
          clearScanError();
        }
        input.value = res.path;
        fill.style.width = '100%';
        text.textContent = `✓ Uploaded: ${file.name}`;
        if (activeUploads === 0) {
          scanStatusText.textContent = isVideo ? 'Upload complete. Click Scan for Profanity.' : 'Subtitle uploaded.';
        }
        setTimeout(() => progressBox.classList.add('hidden'), 3500);
      } else {
        text.textContent = 'Upload failed';
        if (activeUploads === 0) scanStatusText.textContent = '';
      }
    };

    xhr.onerror = () => {
      finishUpload();
      text.textContent = 'Upload error';
      if (activeUploads === 0) scanStatusText.textContent = '';
    };

    xhr.onabort = () => {
      finishUpload();
      text.textContent = 'Upload cancelled';
      if (activeUploads === 0) scanStatusText.textContent = '';
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
          if (isVideo) {
            onVideoSelectionChanged();
          } else {
            clearScanError();
          }
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

  videoPathInput.addEventListener('change', () => {
    const currentVal = videoPathInput.value.trim();
    if (currentVal !== lastScannedVideoPath) {
      onVideoSelectionChanged();
    }
  });

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
  const audioRefineToggle = document.getElementById('audioRefineToggle');
  const syncOffsetInput = document.getElementById('syncOffsetInput');
  const syncMinusHalfBtn = document.getElementById('syncMinusHalfBtn');
  const syncMinusTenthBtn = document.getElementById('syncMinusTenthBtn');
  const syncPlusTenthBtn = document.getElementById('syncPlusTenthBtn');
  const syncPlusHalfBtn = document.getElementById('syncPlusHalfBtn');

  function adjustSync(delta) {
    let val = parseFloat(syncOffsetInput.value) || 0.0;
    val = Math.round((val + delta) * 10) / 10;
    syncOffsetInput.value = val.toFixed(1);
    triggerRescanIfActive(true);
  }
  if (syncMinusHalfBtn) syncMinusHalfBtn.addEventListener('click', () => adjustSync(-0.5));
  if (syncMinusTenthBtn) syncMinusTenthBtn.addEventListener('click', () => adjustSync(-0.2));
  if (syncPlusTenthBtn) syncPlusTenthBtn.addEventListener('click', () => adjustSync(+0.2));
  if (syncPlusHalfBtn) syncPlusHalfBtn.addEventListener('click', () => adjustSync(+0.5));
  if (syncOffsetInput) {
    syncOffsetInput.addEventListener('input', () => triggerRescanIfActive(true));
    syncOffsetInput.addEventListener('change', () => triggerRescanIfActive(false));
  }
  if (presetSelect) {
    presetSelect.addEventListener('change', () => triggerRescanIfActive(false));
  }
  if (customWordsInput) {
    customWordsInput.addEventListener('change', () => triggerRescanIfActive(false));
  }

  // Timecode update, error handling, and mute listener cleanup in player
  previewPlayer.addEventListener('timeupdate', () => {
    playerTimecode.textContent = formatSeconds(previewPlayer.currentTime);
  });
  previewPlayer.addEventListener('pause', clearTestMute);
  previewPlayer.addEventListener('ended', clearTestMute);
  previewPlayer.addEventListener('error', () => {
    if (previewPlayer.getAttribute('src') && playerCompatNotice) {
      playerCompatNotice.classList.remove('hidden');
    }
  });
  previewPlayer.addEventListener('loadeddata', () => {
    if (playerCompatNotice) {
      playerCompatNotice.classList.add('hidden');
    }
  });

  if (timingPresetSelect && wordTimingToggle) {
    timingPresetSelect.addEventListener('change', () => {
      wordTimingToggle.checked = (timingPresetSelect.value !== 'segment');
      if (!workspace.classList.contains('hidden') && videoPathInput.value.trim()) {
        scanBtn.click();
      }
    });
    wordTimingToggle.addEventListener('change', () => {
      timingPresetSelect.value = wordTimingToggle.checked ? 'smart' : 'segment';
      if (!workspace.classList.contains('hidden') && videoPathInput.value.trim()) {
        scanBtn.click();
      }
    });
  }
  if (audioRefineToggle) {
    audioRefineToggle.addEventListener('change', () => {
      if (!workspace.classList.contains('hidden') && videoPathInput.value.trim()
          && timingPresetSelect && timingPresetSelect.value !== 'segment') {
        scanBtn.click();
      }
    });
  }

  // Scan Media
  scanBtn.addEventListener('click', async () => {
    clearScanError();
    const videoPath = videoPathInput.value.trim();
    const srtPath = srtPathInput.value.trim();

    if (!videoPath) {
      showScanError('Please select a video file using Browse or Upload first.');
      return;
    }

    scanStatusText.textContent = 'Scanning media & inspecting subtitles...';
    scanBtn.disabled = true;

    // Calculate timing parameters
    const presetVal = timingPresetSelect ? timingPresetSelect.value : 'smart';
    let padBefore = 0.18, padAfter = 0.16, leadIn = 0.10, timingMode = 'word';
    if (presetVal === 'smart') {
      padBefore = 0.18; padAfter = 0.16; leadIn = 0.10; timingMode = 'word';
    } else if (presetVal === 'tight') {
      padBefore = 0.10; padAfter = 0.10; leadIn = 0.05; timingMode = 'word';
    } else if (presetVal === 'standard') {
      padBefore = 0.22; padAfter = 0.20; leadIn = 0.12; timingMode = 'word';
    } else if (presetVal === 'safe') {
      padBefore = 0.32; padAfter = 0.30; leadIn = 0.20; timingMode = 'word';
    } else if (presetVal === 'segment') {
      padBefore = 0.20; padAfter = 0.20; leadIn = 0.0; timingMode = 'segment';
    }
    const audioRefine = (timingMode !== 'segment') && (audioRefineToggle ? audioRefineToggle.checked : true);
    const syncOffset = syncOffsetInput ? (parseFloat(syncOffsetInput.value) || 0.0) : 0.0;

    try {
      const resp = await fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_path: videoPath,
          srt_path: srtPath,
          preset: presetSelect.value,
          custom_words: parseCustomWords(),
          timing_mode: timingMode,
          padding_before: padBefore,
          padding_after: padAfter,
          lead_in: leadIn,
          audio_refine: audioRefine,
          sync_offset: syncOffset,
          sub_lang: subSearchLang ? subSearchLang.value : 'en'
        })
      });

      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || 'Failed to scan media');
      }

      // Populate UI
      clearTestMute();
      detections = (data.detections || []).map(d => ({
        ...d,
        orig_mute_start: d.mute_start,
        orig_mute_end: d.mute_end
      }));
      currentMediaDuration = data.duration || 0;
      outputVideoInput.value = data.suggested_output || '';

      if (data.video_path) {
        videoPathInput.value = data.video_path;
      }
      lastScannedVideoPath = videoPathInput.value.trim();
      if (playerCompatNotice) {
        playerCompatNotice.classList.add('hidden');
      }

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
      previewPlayer.src = `/api/stream?path=${encodeURIComponent(lastScannedVideoPath || videoPath)}`;
      previewPlayer.load();

      // Show Workspace
      workspace.classList.remove('hidden');
      const subNote = data.subtitle_downloaded
        ? ` (auto-downloaded subtitles from ${data.subtitle_provider})`
        : '';
      const exportNote = data.is_uploaded && data.suggested_output
        ? ` Cleaned export will save to ${data.suggested_output}.`
        : '';
      scanStatusText.textContent = `Scan complete: ${detections.length} profanities found${subNote}.${exportNote}`;

      renderTable();
      updateCounters();
    } catch (err) {
      const isSubErr = /(subtitle|srt|vtt)/i.test(err.message);
      showScanError(err.message, isSubErr);
      scanStatusText.textContent = '';
    } finally {
      scanBtn.disabled = activeUploads > 0;
    }
  });

  // Render Table
  function renderTable() {
    detectionsTbody.textContent = '';

    const filtered = detections.filter(d => {
      const matchesTier = (currentFilterTier === 'all') || (d.tier === currentFilterTier);
      const matchesSearch = !searchQuery ||
        d.word.toLowerCase().includes(searchQuery.toLowerCase()) ||
        d.context_text.toLowerCase().includes(searchQuery.toLowerCase());
      return matchesTier && matchesSearch;
    });

    if (filtered.length === 0) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 6;
      td.style.textAlign = 'center';
      td.style.color = 'var(--text-muted)';
      td.style.padding = '24px';
      td.textContent = 'No matching profanities found.';
      tr.appendChild(td);
      detectionsTbody.appendChild(tr);
      return;
    }

    filtered.forEach(d => {
      const tr = document.createElement('tr');
      if (selectedDetection && selectedDetection.id === d.id) {
        tr.style.backgroundColor = 'rgba(56, 189, 248, 0.08)';
      }

      // 1. Checkbox cell
      const tdCheck = document.createElement('td');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'row-checkbox';
      checkbox.dataset.id = String(d.id);
      checkbox.checked = Boolean(d.enabled);
      checkbox.addEventListener('change', (e) => {
        d.enabled = e.target.checked;
        updateCounters();
      });
      tdCheck.appendChild(checkbox);
      tr.appendChild(tdCheck);

      // 2. Time & Nudge cell
      const tdTime = document.createElement('td');
      const timeHeader = document.createElement('div');
      timeHeader.style.display = 'flex';
      timeHeader.style.alignItems = 'center';

      const timeBtn = document.createElement('button');
      timeBtn.className = 'time-btn';
      timeBtn.dataset.time = String(d.mute_start);
      timeBtn.textContent = formatSeconds(d.mute_start);
      timeBtn.addEventListener('click', () => {
        clearTestMute();
        selectedDetection = d;
        previewPlayer.currentTime = Math.max(0, d.mute_start - 0.5);
        playPreviewSafely();
        renderTable();
      });

      const spanBadge = document.createElement('span');
      spanBadge.className = 'mute-span-badge';
      const muteDur = Math.max(0, (Number(d.mute_end) || 0) - (Number(d.mute_start) || 0));
      spanBadge.textContent = `(${muteDur.toFixed(2)}s)`;

      timeHeader.appendChild(timeBtn);
      timeHeader.appendChild(spanBadge);
      tdTime.appendChild(timeHeader);

      const nudgeRow = document.createElement('div');
      nudgeRow.style.marginTop = '4px';
      nudgeRow.style.display = 'flex';
      nudgeRow.style.gap = '2px';

      const earlierBtn = document.createElement('button');
      earlierBtn.className = 'nudge-btn nudge-earlier';
      earlierBtn.title = 'Start mute 0.2s earlier (prevents missing start of word)';
      earlierBtn.textContent = '◀ Earlier';
      earlierBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        d.mute_start = Math.max(0, Math.round((d.mute_start - 0.2) * 100) / 100);
        renderTable();
        updateCounters();
      });

      const laterBtn = document.createElement('button');
      laterBtn.className = 'nudge-btn nudge-later';
      laterBtn.title = 'Extend mute 0.2s later';
      laterBtn.textContent = 'Later ▶';
      laterBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        d.mute_end = Math.round((d.mute_end + 0.2) * 100) / 100;
        renderTable();
        updateCounters();
      });

      const resetBtn = document.createElement('button');
      resetBtn.className = 'nudge-btn nudge-reset';
      resetBtn.title = 'Reset timing to scanned default';
      resetBtn.textContent = '↺';
      resetBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        d.mute_start = d.orig_mute_start;
        d.mute_end = d.orig_mute_end;
        renderTable();
        updateCounters();
      });

      nudgeRow.appendChild(earlierBtn);
      nudgeRow.appendChild(laterBtn);
      nudgeRow.appendChild(resetBtn);
      tdTime.appendChild(nudgeRow);
      tr.appendChild(tdTime);

      // 3. Word badge cell
      const tdWord = document.createElement('td');
      const wordBadge = document.createElement('span');
      const tierClass = ['strict', 'moderate', 'mild', 'custom'].includes(d.tier) ? d.tier : 'info';
      wordBadge.className = `badge badge-${tierClass}`;
      wordBadge.textContent = String(d.word || '');
      tdWord.appendChild(wordBadge);
      tr.appendChild(tdWord);

      // 4. Severity cell
      const tdTier = document.createElement('td');
      const tierSpan = document.createElement('span');
      tierSpan.style.color = 'var(--text-muted)';
      tierSpan.style.textTransform = 'capitalize';
      tierSpan.textContent = String(d.tier || '');
      tdTier.appendChild(tierSpan);
      tr.appendChild(tdTier);

      // 5. Context Dialogue cell
      const tdContext = document.createElement('td');
      appendHighlightedContext(tdContext, d.context_text, d.word);
      tr.appendChild(tdContext);

      // 6. Preview cell
      const tdPreview = document.createElement('td');
      const testBtn = document.createElement('button');
      testBtn.className = 'btn-xs test-mute-btn';
      testBtn.title = 'Listen to this exact word muted in preview player';
      testBtn.textContent = '🎧 Test Mute';
      testBtn.addEventListener('click', () => {
        clearTestMute();
        selectedDetection = d;
        previewPlayer.currentTime = Math.max(0, d.mute_start - 1.0);
        playPreviewSafely();

        activeMuteChecker = () => {
          if (previewPlayer.paused || previewPlayer.ended) {
            clearTestMute();
            return;
          }
          const t = previewPlayer.currentTime;
          previewPlayer.muted = (t >= d.mute_start && t <= d.mute_end);
          if (t > d.mute_end + 1.2) {
            clearTestMute();
          }
        };
        previewPlayer.addEventListener('timeupdate', activeMuteChecker);
        renderTable();
      });
      tdPreview.appendChild(testBtn);
      tr.appendChild(tdPreview);

      detectionsTbody.appendChild(tr);
    });
  }

  function updateCounters() {
    const active = detections.filter(d => d.enabled);
    statCount.textContent = detections.length;
    activeCountSpan.textContent = active.length;
    renderBtnCount.textContent = active.length;

    let totalMuteSecs = 0;
    if (active.length > 0) {
      const sorted = active
        .map(d => [Math.max(0, Number(d.mute_start) || 0), Math.max(0, Number(d.mute_end) || 0)])
        .filter(([s, e]) => e > s)
        .sort((a, b) => a[0] - b[0]);

      if (sorted.length > 0) {
        const merged = [sorted[0].slice()];
        const gapThreshold = 0.25;
        for (let i = 1; i < sorted.length; i++) {
          const [s, e] = sorted[i];
          const last = merged[merged.length - 1];
          if (s <= last[1] + gapThreshold) {
            last[1] = Math.max(last[1], e);
          } else {
            merged.push([s, e]);
          }
        }
        merged.forEach(([s, e]) => {
          totalMuteSecs += (e - s);
        });
      }
    }
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
    clearTestMute();
    if (!selectedDetection && detections.length > 0) {
      selectedDetection = detections[0];
    }
    if (selectedDetection) {
      previewPlayer.currentTime = Math.max(0, selectedDetection.mute_start - 1.0);
      playPreviewSafely();
    }
  });

  // Start Processing
  startProcessBtn.addEventListener('click', async () => {
    clearScanError();
    const videoPath = videoPathInput.value.trim();
    const outputPath = outputVideoInput.value.trim();
    const srtPath = srtPathInput.value.trim();

    if (!outputPath) {
      showScanError('Please specify an output video destination.');
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
          preset: presetSelect.value,
          custom_words: parseCustomWords(),
          center_channel_only: centerOnlyToggle.checked,
          generate_clean_srt: cleanSrtToggle.checked,
          detections: detections
        })
      });

      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || 'Failed to start processing');
      }

      // Show Progress Dialog
      isProcessingJob = true;
      progressTitle.textContent = 'Censoring Media...';
      progressSubtitle.textContent = 'Losslessly copying video stream & processing audio...';
      progressCompletionActions.classList.add('hidden');
      revealFinderBtn.classList.remove('hidden');
      previewCleanedBtn.classList.remove('hidden');
      progressErrorMsg.classList.add('hidden');
      cancelProcessBtn.classList.remove('hidden');
      progressBar.style.width = '0%';
      progressPercent.textContent = '0.0%';
      if (!progressModal.open) {
        progressModal.showModal();
      }

      startProgressPolling();
    } catch (err) {
      showScanError(`Error starting censorship: ${err.message}`);
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
          isProcessingJob = false;
          progressBar.style.width = '100%';
          progressPercent.textContent = '100%';
          progressTitle.textContent = '🎉 Processing Complete!';
          progressSubtitle.textContent = `Saved to: ${data.output_path}`;
          cancelProcessBtn.classList.add('hidden');
          revealFinderBtn.classList.remove('hidden');
          previewCleanedBtn.classList.remove('hidden');
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
            clearTestMute();
            isProcessingJob = false;
            if (progressModal.open) progressModal.close();
            previewPlayer.src = `/api/stream?path=${encodeURIComponent(data.output_path)}`;
            playPreviewSafely();
          };
        } else if (data.status === 'failed') {
          clearInterval(pollInterval);
          isProcessingJob = false;
          progressTitle.textContent = 'Processing Failed';
          progressSubtitle.textContent = data.error || 'An error occurred during FFmpeg execution';
          progressErrorMsg.textContent = data.error || 'Unknown error';
          progressErrorMsg.classList.remove('hidden');
          cancelProcessBtn.classList.add('hidden');
          revealFinderBtn.classList.add('hidden');
          previewCleanedBtn.classList.add('hidden');
          progressCompletionActions.classList.remove('hidden');
        }
      } catch (e) {
        // network glitch
      }
    }, 400);
  }

  progressModal.addEventListener('cancel', (e) => {
    if (isProcessingJob) {
      e.preventDefault();
    }
  });

  cancelProcessBtn.addEventListener('click', async () => {
    if (confirm('Cancel video processing?')) {
      await fetch('/api/cancel', { method: 'POST' });
      clearInterval(pollInterval);
      isProcessingJob = false;
      if (progressModal.open) progressModal.close();
    }
  });

  closeProgressBtn.addEventListener('click', () => {
    isProcessingJob = false;
    if (progressModal.open) progressModal.close();
  });

  // Local File Browser
  async function loadBrowserDir(path) {
    try {
      const resp = await fetch(`/api/browse?dir=${encodeURIComponent(path || '')}`);
      const data = await resp.json();
      browserCurrentDir = data.current_dir;
      browserCurrentPath.textContent = browserCurrentDir;
      browserList.textContent = '';

      if (browserBookmarks && data.bookmarks) {
        browserBookmarks.textContent = '';
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
        const iconSpan = document.createElement('span');
        iconSpan.textContent = item.is_dir ? '📁' : (item.is_video ? '🎬' : '📄');
        const nameSpan = document.createElement('span');
        nameSpan.textContent = String(item.name || '');
        div.appendChild(iconSpan);
        div.appendChild(document.createTextNode(' '));
        div.appendChild(nameSpan);
        div.addEventListener('click', () => {
          if (item.is_dir) {
            loadBrowserDir(item.path);
          } else {
            if (browseTargetInput === videoPathInput) {
              onVideoSelectionChanged();
            } else {
              clearScanError();
            }
            if (browseTargetInput) {
              browseTargetInput.value = item.path;
            }
            if (browserModal.open) browserModal.close();
          }
        });
        browserList.appendChild(div);
      });
    } catch (err) {
      browserList.textContent = '';
      const errDiv = document.createElement('div');
      errDiv.style.padding = '12px';
      errDiv.style.color = 'var(--tier-strict)';
      errDiv.textContent = 'Failed to load directory';
      browserList.appendChild(errDiv);
    }
  }

  browseVideoBtn.addEventListener('click', () => {
    browseTargetInput = videoPathInput;
    if (!browserModal.open) browserModal.showModal();
    loadBrowserDir(browserCurrentDir || '');
  });

  browseSrtBtn.addEventListener('click', () => {
    browseTargetInput = srtPathInput;
    if (!browserModal.open) browserModal.showModal();
    loadBrowserDir(browserCurrentDir || '');
  });

  // Trusted Online Subtitle Search Dialog
  function applyMaskedConfigState(cfg) {
    if (!cfg) return;
    if (subSearchLang && cfg.subtitle_language) {
      const opt = Array.from(subSearchLang.options).find(o => o.value === cfg.subtitle_language);
      if (opt) subSearchLang.value = cfg.subtitle_language;
    }
    if (subOsUserInput) subOsUserInput.value = cfg.opensubtitles_username || '';
    if (subOsApiKeyInput) {
      subOsApiKeyInput.placeholder = cfg.opensubtitles_api_key_from_env
        ? 'Loaded from .env / environment'
        : (cfg.has_opensubtitles_api_key
          ? 'Saved (enter new key to replace)'
          : 'Optional API key');
    }
    if (subOsPassInput) {
      subOsPassInput.placeholder = cfg.opensubtitles_password_from_env
        ? 'Loaded from .env / environment'
        : (cfg.has_opensubtitles_password
          ? 'Saved (enter new password to replace)'
          : 'Optional password');
    }
    if (subDlApiKeyInput) {
      subDlApiKeyInput.placeholder = cfg.subdl_api_key_from_env
        ? 'Loaded from .env / environment'
        : (cfg.has_subdl_api_key
          ? 'Saved (enter new key to replace)'
          : 'Optional API key');
    }
  }

  async function loadSubtitleConfig() {
    try {
      const resp = await fetch('/api/config');
      if (resp.ok) {
        const cfg = await resp.json();
        applyMaskedConfigState(cfg);
      }
    } catch (_err) {
      // Ignore config prefetch errors
    }
  }

  function renderSubtitleResults(candidates) {
    if (!subtitleResultsList) return;
    subtitleResultsList.textContent = '';

    if (!candidates || candidates.length === 0) {
      const emptyLi = document.createElement('li');
      emptyLi.className = 'sub-result-empty';
      emptyLi.textContent = 'No matching subtitles found from trusted providers. Adjust the title/IMDb ID or add optional API credentials below.';
      subtitleResultsList.appendChild(emptyLi);
      return;
    }

    candidates.forEach((cand) => {
      const li = document.createElement('li');
      li.className = 'sub-result-item';

      const infoDiv = document.createElement('div');
      infoDiv.className = 'sub-result-info';

      const releaseDiv = document.createElement('div');
      releaseDiv.className = 'sub-result-release';
      releaseDiv.textContent = cand.release_name || cand.file_name || cand.title || 'Subtitle';
      infoDiv.appendChild(releaseDiv);

      const metaDiv = document.createElement('div');
      metaDiv.className = 'sub-result-meta';

      const providerSpan = document.createElement('span');
      providerSpan.className = 'badge badge-info';
      providerSpan.textContent = cand.provider;
      metaDiv.appendChild(providerSpan);

      if (cand.hash_matched) {
        const hashSpan = document.createElement('span');
        hashSpan.className = 'badge badge-mild';
        hashSpan.textContent = 'HASH MATCH';
        metaDiv.appendChild(hashSpan);
      }

      if (cand.hearing_impaired) {
        const hiSpan = document.createElement('span');
        hiSpan.className = 'badge badge-moderate';
        hiSpan.textContent = 'HI';
        metaDiv.appendChild(hiSpan);
      }

      const detailSpan = document.createElement('span');
      detailSpan.textContent = `Lang: ${cand.language || 'en'} | Downloads: ${cand.downloads || 0} | Score: ${cand.score ?? 0}`;
      metaDiv.appendChild(detailSpan);

      infoDiv.appendChild(metaDiv);
      li.appendChild(infoDiv);

      const useBtn = document.createElement('button');
      useBtn.type = 'button';
      useBtn.className = 'btn-primary btn-sm';
      useBtn.textContent = 'Use Subtitle';
      useBtn.addEventListener('click', async () => {
        useBtn.disabled = true;
        useBtn.textContent = 'Downloading...';
        subSearchStatus.textContent = `Downloading from ${cand.provider}...`;
        try {
          const resp = await fetch('/api/subtitles/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              video_path: videoPathInput.value.trim(),
              provider: cand.provider,
              subtitle_id: cand.subtitle_id,
              title: cand.title,
              language: subSearchLang ? subSearchLang.value : cand.language,
              release_name: cand.release_name,
              file_name: cand.file_name,
              download_url: cand.download_url,
              download_id: cand.download_id
            })
          });
          const dlData = await resp.json();
          if (!resp.ok) {
            throw new Error(dlData.error || 'Subtitle download failed');
          }
          srtPathInput.value = dlData.srt_path;
          scanStatusText.textContent = `Downloaded ${dlData.cue_count} subtitle cues from ${dlData.provider}. Click Scan for Profanity.`;
          subSearchStatus.textContent = '';
          subtitleSearchDialog.close();
        } catch (err) {
          subSearchStatus.textContent = `Error: ${err.message}`;
          useBtn.disabled = false;
          useBtn.textContent = 'Use Subtitle';
        }
      });

      li.appendChild(useBtn);
      subtitleResultsList.appendChild(li);
    });
  }

  async function performSubtitleSearch() {
    if (!runSubSearchBtn) return;
    runSubSearchBtn.disabled = true;
    subSearchStatus.textContent = 'Querying trusted providers...';

    try {
      const resp = await fetch('/api/subtitles/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_path: videoPathInput.value.trim(),
          title: subSearchTitle.value.trim(),
          year: subSearchYear.value.trim(),
          season: subSearchSeason.value.trim(),
          episode: subSearchEpisode.value.trim(),
          imdb_id: subSearchImdb.value.trim(),
          language: subSearchLang ? subSearchLang.value : 'en'
        })
      });
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || 'Subtitle search failed');
      }

      if (data.query) {
        if (!subSearchTitle.value.trim() && data.query.title) subSearchTitle.value = data.query.title;
        if (!subSearchYear.value.trim() && data.query.year) subSearchYear.value = String(data.query.year);
        if (!subSearchSeason.value.trim() && data.query.season !== null && data.query.season !== undefined) {
          subSearchSeason.value = String(data.query.season);
        }
        if (!subSearchEpisode.value.trim() && data.query.episode !== null && data.query.episode !== undefined) {
          subSearchEpisode.value = String(data.query.episode);
        }
        if (!subSearchImdb.value.trim() && data.query.imdb_id) subSearchImdb.value = data.query.imdb_id;
      }

      const candidates = data.candidates || [];
      subSearchStatus.textContent = `Found ${candidates.length} subtitle match(es).`;
      renderSubtitleResults(candidates);
    } catch (err) {
      subSearchStatus.textContent = `Search error: ${err.message}`;
      renderSubtitleResults([]);
    } finally {
      runSubSearchBtn.disabled = false;
    }
  }

  // Fallback for browsers without closedby attribute support (e.g., Safari)
  if (!('closedBy' in HTMLDialogElement.prototype)) {
    const isOutsideDialog = (e, dialog) => {
      const r = dialog.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    };
    document.querySelectorAll('dialog[closedby="any"]').forEach((dialog) => {
      let pressedOutside = false;
      dialog.addEventListener('pointerdown', (e) => {
        pressedOutside = isOutsideDialog(e, dialog);
      });
      dialog.addEventListener('pointerup', (e) => {
        if (pressedOutside && isOutsideDialog(e, dialog)) {
          dialog.close();
        }
        pressedOutside = false;
      });
    });
  }

  if (searchOnlineSubsBtn && subtitleSearchDialog) {
    searchOnlineSubsBtn.addEventListener('click', async () => {
      await loadSubtitleConfig();
      subtitleResultsList.textContent = '';
      subSearchStatus.textContent = '';
      subKeysStatus.textContent = '';
      subtitleSearchDialog.showModal();
      if (videoPathInput.value.trim() || subSearchTitle.value.trim() || subSearchImdb.value.trim()) {
        performSubtitleSearch();
      } else if (subSearchTitle) {
        subSearchTitle.focus();
      }
    });
  }

  if (subtitleSearchForm) {
    subtitleSearchForm.addEventListener('submit', (e) => {
      e.preventDefault();
      performSubtitleSearch();
    });
  }

  if (saveSubKeysBtn) {
    saveSubKeysBtn.addEventListener('click', async () => {
      const payload = {
        subtitle_language: subSearchLang ? subSearchLang.value : 'en',
        opensubtitles_username: subOsUserInput ? subOsUserInput.value.trim() : ''
      };
      if (subOsApiKeyInput && subOsApiKeyInput.value.trim()) {
        payload.opensubtitles_api_key = subOsApiKeyInput.value.trim();
      }
      if (subOsPassInput && subOsPassInput.value) {
        payload.opensubtitles_password = subOsPassInput.value;
      }
      if (subDlApiKeyInput && subDlApiKeyInput.value.trim()) {
        payload.subdl_api_key = subDlApiKeyInput.value.trim();
      }

      subKeysStatus.textContent = 'Saving...';
      try {
        const resp = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Save failed');
        if (subOsApiKeyInput) subOsApiKeyInput.value = '';
        if (subOsPassInput) subOsPassInput.value = '';
        if (subDlApiKeyInput) subDlApiKeyInput.value = '';
        applyMaskedConfigState(data.config);
        subKeysStatus.textContent = 'Provider settings saved.';
      } catch (err) {
        subKeysStatus.textContent = `Save error: ${err.message}`;
      }
    });
  }

  async function loadHealthStatus() {
    try {
      const resp = await fetch('/api/health');
      if (!resp.ok) return;
      const health = await resp.json();
      if (revealFinderBtn) {
        if (health.platform === 'darwin') {
          revealFinderBtn.textContent = '📂 Reveal in Finder';
        } else if (health.platform === 'win32') {
          revealFinderBtn.textContent = '📂 Show in Explorer';
        } else {
          revealFinderBtn.textContent = '📂 Show in Folder';
        }
      }
      if (!health.ffmpeg_ok && ffmpegWarningBanner && ffmpegWarningText) {
        const hint = health.install_hint ? ` Install it with: ${health.install_hint}` : '';
        ffmpegWarningText.textContent = `FFmpeg and ffprobe are required to scan and export videos.${hint}`;
        ffmpegWarningBanner.classList.remove('hidden');
      }
    } catch (_err) {
      // Ignore health check prefetch errors
    }
  }
  loadHealthStatus();

  if (stopStudioBtn) {
    stopStudioBtn.addEventListener('click', async () => {
      if (isProcessingJob && !confirm('A video is currently processing. Stop Movie Censor Studio anyway?')) {
        return;
      }
      stopStudioBtn.disabled = true;
      try {
        await fetch('/api/shutdown', { method: 'POST' });
      } catch (_err) {
        // Server may close connection immediately on shutdown
      }
      if (pollInterval) clearInterval(pollInterval);
      isProcessingJob = false;
      scanBtn.disabled = true;
      startProcessBtn.disabled = true;
      if (progressModal && progressModal.open) progressModal.close();
      if (browserModal && browserModal.open) browserModal.close();
      if (subtitleSearchDialog && subtitleSearchDialog.open) subtitleSearchDialog.close();
      showScanError('Movie Censor Studio has stopped. You can safely close this browser tab.');
      scanStatusText.textContent = 'Server stopped.';
    });
  }
});
