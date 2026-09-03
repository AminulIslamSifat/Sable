/* ── OCR Panel (Main Area + Sidebar Recent Results) ── */
/* Depends on: sidebarHost, escHtml */
(function () {
  'use strict';

  let isOpen = false;
  let sidebarLoaded = false;
  let currentTab = 'single';

  function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
  }

  /* ── Helpers ── */
  function hideOtherViews() {
    const chat = document.getElementById('chat');
    if (chat) chat.classList.add('hidden');
    const inputArea = document.getElementById('inputArea');
    if (inputArea) inputArea.classList.add('hidden');
    ['searchView', 'dashboardView', 'knowledgeView', 'researchView', 'imageView', 'promptgenView', 'calendarView'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.add('hidden');
    });
    const searchEl = document.getElementById('searchView');
    if (searchEl) searchEl.style.display = '';
  }

  function showChat() {
    const chat = document.getElementById('chat');
    if (chat) chat.classList.remove('hidden');
    const inputArea = document.getElementById('inputArea');
    if (inputArea) inputArea.classList.remove('hidden');
  }

  function formatSize(bytes) {
    if (!bytes) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }

  async function copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      return true;
    }
  }

  function downloadAsText(text, filename) {
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'ocr-result.txt';
    a.click();
    URL.revokeObjectURL(url);
  }

  /* ── Tab Rendering ── */
  function renderSingleTab(container) {
    container.innerHTML = `
      <div class="ocr-upload-zone" id="ocrSingleZone">
        <div class="ocr-zone-icon"><i data-lucide="image"></i></div>
        <div class="ocr-zone-text">Drop an image here or click to browse</div>
        <div class="ocr-zone-hint">Supports PNG, JPG, JPEG, WEBP, BMP, GIF, TIFF</div>
        <input type="file" id="ocrSingleInput" accept=".png,.jpg,.jpeg,.webp,.bmp,.gif,.tiff" hidden>
      </div>
      <div class="ocr-preview-area hidden" id="ocrSinglePreview">
        <img id="ocrSingleThumb" class="ocr-thumb" alt="Preview">
        <div class="ocr-file-info" id="ocrSingleInfo"></div>
        <button class="ocr-remove-btn" id="ocrSingleRemove" title="Remove"><i data-lucide="x"></i></button>
      </div>
      <button class="ocr-action-btn" id="ocrSingleRecognize" disabled>Recognize Text</button>
      <div class="ocr-progress hidden" id="ocrSingleProgress">
        <div class="ocr-progress-bar"><div class="ocr-progress-fill" id="ocrSingleFill"></div></div>
        <div class="ocr-progress-text" id="ocrSingleStatus">Processing…</div>
      </div>
      <div class="ocr-results hidden" id="ocrSingleResults">
        <div class="ocr-results-header">
          <span class="ocr-results-title">Extracted Text</span>
          <div class="ocr-results-actions">
            <button class="ocr-copy-btn" id="ocrSingleCopy" title="Copy"><i data-lucide="copy"></i></button>
            <button class="ocr-download-btn" id="ocrSingleDownload" title="Download"><i data-lucide="download"></i></button>
          </div>
        </div>
        <textarea class="ocr-textarea" id="ocrSingleText" readonly></textarea>
      </div>
    `;
    if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });

    let selectedFile = null;
    const zone = container.querySelector('#ocrSingleZone');
    const input = container.querySelector('#ocrSingleInput');
    const preview = container.querySelector('#ocrSinglePreview');
    const thumb = container.querySelector('#ocrSingleThumb');
    const info = container.querySelector('#ocrSingleInfo');
    const removeBtn = container.querySelector('#ocrSingleRemove');
    const recognizeBtn = container.querySelector('#ocrSingleRecognize');
    const progress = container.querySelector('#ocrSingleProgress');
    const fill = container.querySelector('#ocrSingleFill');
    const status = container.querySelector('#ocrSingleStatus');
    const results = container.querySelector('#ocrSingleResults');
    const textarea = container.querySelector('#ocrSingleText');
    const copyBtn = container.querySelector('#ocrSingleCopy');
    const downloadBtn = container.querySelector('#ocrSingleDownload');

    function setFile(file) {
      selectedFile = file;
      zone.classList.add('hidden');
      preview.classList.remove('hidden');
      thumb.src = URL.createObjectURL(file);
      info.textContent = `${file.name} · ${formatSize(file.size)}`;
      recognizeBtn.disabled = false;
      results.classList.add('hidden');
      progress.classList.add('hidden');
    }

    function clearFile() {
      selectedFile = null;
      zone.classList.remove('hidden');
      preview.classList.add('hidden');
      recognizeBtn.disabled = true;
      results.classList.add('hidden');
      progress.classList.add('hidden');
    }

    zone.addEventListener('click', () => input.click());
    zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('ocr-zone-drag'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('ocr-zone-drag'));
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      zone.classList.remove('ocr-zone-drag');
      if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
    });
    input.addEventListener('change', () => { if (input.files.length) setFile(input.files[0]); });
    removeBtn.addEventListener('click', clearFile);

    recognizeBtn.addEventListener('click', async () => {
      if (!selectedFile) return;
      recognizeBtn.disabled = true;
      progress.classList.remove('hidden');
      results.classList.add('hidden');
      fill.style.width = '30%';
      status.textContent = 'Uploading & recognizing…';

      const form = new FormData();
      form.append('file', selectedFile);

      try {
        fill.style.width = '60%';
        const res = await fetch('/api/ocr/recognize', { method: 'POST', body: form });
        fill.style.width = '90%';
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(err.detail || 'OCR failed');
        }
        const data = await res.json();
        fill.style.width = '100%';
        status.textContent = `Done in ${data.duration_sec || '?'}s`;
        textarea.value = data.full_text || '';
        results.classList.remove('hidden');
      } catch (err) {
        status.textContent = `Error: ${err.message}`;
        fill.style.width = '0%';
      } finally {
        recognizeBtn.disabled = false;
      }
    });

    copyBtn.addEventListener('click', async () => {
      await copyToClipboard(textarea.value);
      copyBtn.title = 'Copied!';
      setTimeout(() => { copyBtn.title = 'Copy'; }, 1500);
    });

    downloadBtn.addEventListener('click', () => {
      downloadAsText(textarea.value, (selectedFile?.name || 'ocr') + '.txt');
    });
  }

  function renderBatchTab(container) {
    container.innerHTML = `
      <div class="ocr-upload-zone" id="ocrBatchZone">
        <div class="ocr-zone-icon"><i data-lucide="images"></i></div>
        <div class="ocr-zone-text">Drop images here or click to browse</div>
        <div class="ocr-zone-hint">Multiple files supported</div>
        <input type="file" id="ocrBatchInput" accept=".png,.jpg,.jpeg,.webp,.bmp,.gif,.tiff" multiple hidden>
      </div>
      <div class="ocr-file-list" id="ocrBatchList"></div>
      <button class="ocr-action-btn" id="ocrBatchRecognize" disabled>Recognize All</button>
      <div class="ocr-progress hidden" id="ocrBatchProgress">
        <div class="ocr-progress-bar"><div class="ocr-progress-fill" id="ocrBatchFill"></div></div>
        <div class="ocr-progress-text" id="ocrBatchStatus">Processing…</div>
      </div>
      <div class="ocr-batch-results hidden" id="ocrBatchResults"></div>
    `;
    if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });

    let files = [];
    const zone = container.querySelector('#ocrBatchZone');
    const input = container.querySelector('#ocrBatchInput');
    const list = container.querySelector('#ocrBatchList');
    const recognizeBtn = container.querySelector('#ocrBatchRecognize');
    const progress = container.querySelector('#ocrBatchProgress');
    const fill = container.querySelector('#ocrBatchFill');
    const statusEl = container.querySelector('#ocrBatchStatus');
    const resultsDiv = container.querySelector('#ocrBatchResults');

    function renderList() {
      list.innerHTML = '';
      files.forEach((f, i) => {
        const item = document.createElement('div');
        item.className = 'ocr-file-item';
        item.innerHTML = `
          <img class="ocr-file-thumb" src="${URL.createObjectURL(f)}" alt="">
          <span class="ocr-file-name">${escHtml(f.name)}</span>
          <span class="ocr-file-size">${formatSize(f.size)}</span>
          <button class="ocr-file-remove" data-idx="${i}"><i data-lucide="x"></i></button>
        `;
        list.appendChild(item);
      });
      if (window.lucide) lucide.createIcons({ nodes: list.querySelectorAll('[data-lucide]') });
      list.querySelectorAll('.ocr-file-remove').forEach(btn => {
        btn.addEventListener('click', () => {
          files.splice(parseInt(btn.dataset.idx), 1);
          renderList();
          recognizeBtn.disabled = files.length === 0;
        });
      });
      recognizeBtn.disabled = files.length === 0;
    }

    function addFiles(newFiles) {
      for (const f of newFiles) files.push(f);
      renderList();
    }

    zone.addEventListener('click', () => input.click());
    zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('ocr-zone-drag'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('ocr-zone-drag'));
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      zone.classList.remove('ocr-zone-drag');
      addFiles(e.dataTransfer.files);
    });
    input.addEventListener('change', () => { addFiles(input.files); input.value = ''; });

    recognizeBtn.addEventListener('click', async () => {
      if (!files.length) return;
      recognizeBtn.disabled = true;
      progress.classList.remove('hidden');
      resultsDiv.classList.add('hidden');
      resultsDiv.innerHTML = '';

      const form = new FormData();
      files.forEach(f => form.append('files', f));

      fill.style.width = '5%';
      statusEl.textContent = `Uploading ${files.length} files…`;

      try {
        const res = await fetch('/api/ocr/stream', { method: 'POST', body: form });
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(err.detail || 'Batch OCR failed');
        }

        fill.style.width = '10%';
        statusEl.textContent = `Processing 0/${files.length} files…`;

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let results = null;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const msg = JSON.parse(line.slice(6));
              if (msg.type === 'progress') {
                const pct = Math.round(10 + (msg.files_completed / msg.files_total) * 85);
                fill.style.width = `${pct}%`;
                let statusTxt = `Processing ${msg.files_completed}/${msg.files_total} files`;
                if (msg.pages_total) {
                  statusTxt += ` — ${msg.source}: ${msg.pages_completed}/${msg.pages_total} pages`;
                }
                statusEl.textContent = statusTxt;
              } else if (msg.type === 'complete') {
                results = msg.results;
              }
            } catch {}
          }
        }

        if (!results) throw new Error('Stream ended without results');

        fill.style.width = '100%';
        statusEl.textContent = `Done — ${results.length} files processed`;

        resultsDiv.innerHTML = '';
        results.forEach((r, i) => {
          const card = document.createElement('div');
          card.className = 'ocr-batch-card';
          const text = r.error ? `Error: ${r.error}` : (r.full_text || '');
          const fname = r.source_filename || files[i]?.name || `File ${i + 1}`;
          const pageCount = r.page_count ? ` (${r.page_count} pages)` : '';
          card.innerHTML = `
            <div class="ocr-batch-card-header" data-idx="${i}">
              <span class="ocr-batch-card-name">${escHtml(fname)}${pageCount}</span>
              <span class="ocr-batch-card-toggle">▼</span>
            </div>
            <div class="ocr-batch-card-body hidden" id="ocrBatchBody${i}">
              <textarea class="ocr-textarea" readonly>${escHtml(text)}</textarea>
              <div class="ocr-results-actions">
                <button class="ocr-copy-btn" data-copy="${i}" title="Copy"><i data-lucide="copy"></i></button>
                <button class="ocr-download-btn" data-dl="${i}" title="Download"><i data-lucide="download"></i></button>
              </div>
            </div>
          `;
          resultsDiv.appendChild(card);
        });

        if (window.lucide) lucide.createIcons({ nodes: resultsDiv.querySelectorAll('[data-lucide]') });

        resultsDiv.querySelectorAll('.ocr-batch-card-header').forEach(hdr => {
          hdr.addEventListener('click', () => {
            const idx = hdr.dataset.idx;
            const body = resultsDiv.querySelector(`#ocrBatchBody${idx}`);
            if (body) body.classList.toggle('hidden');
            const toggle = hdr.querySelector('.ocr-batch-card-toggle');
            if (toggle) toggle.textContent = body?.classList.contains('hidden') ? '▼' : '▲';
          });
        });

        resultsDiv.querySelectorAll('[data-copy]').forEach(btn => {
          btn.addEventListener('click', async () => {
            const idx = btn.dataset.copy;
            const ta = resultsDiv.querySelector(`#ocrBatchBody${idx} textarea`);
            if (ta) await copyToClipboard(ta.value);
          });
        });

        resultsDiv.querySelectorAll('[data-dl]').forEach(btn => {
          btn.addEventListener('click', () => {
            const idx = btn.dataset.dl;
            const ta = resultsDiv.querySelector(`#ocrBatchBody${idx} textarea`);
            const name = results[idx]?.source_filename || `file${idx}`;
            if (ta) downloadAsText(ta.value, name + '.txt');
          });
        });

        resultsDiv.classList.remove('hidden');
      } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
        fill.style.width = '0%';
      } finally {
        recognizeBtn.disabled = false;
      }
    });
  }

  function renderPdfTab(container) {
    container.innerHTML = `
      <div class="ocr-upload-zone" id="ocrPdfZone">
        <div class="ocr-zone-icon"><i data-lucide="file-text"></i></div>
        <div class="ocr-zone-text">Drop a PDF here or click to browse</div>
        <div class="ocr-zone-hint">Each page will be recognized separately</div>
        <input type="file" id="ocrPdfInput" accept=".pdf" hidden>
      </div>
      <div class="ocr-preview-area hidden" id="ocrPdfPreview">
        <div class="ocr-pdf-info" id="ocrPdfInfo"></div>
        <button class="ocr-remove-btn" id="ocrPdfRemove" title="Remove"><i data-lucide="x"></i></button>
      </div>
      <button class="ocr-action-btn" id="ocrPdfRecognize" disabled>Recognize All Pages</button>
      <div class="ocr-progress hidden" id="ocrPdfProgress">
        <div class="ocr-progress-bar"><div class="ocr-progress-fill" id="ocrPdfFill"></div></div>
        <div class="ocr-progress-text" id="ocrPdfStatus">Processing…</div>
      </div>
      <div class="ocr-results hidden" id="ocrPdfResults">
        <div class="ocr-results-header">
          <span class="ocr-results-title">Extracted Text</span>
          <div class="ocr-results-actions">
            <button class="ocr-copy-btn" id="ocrPdfCopy" title="Copy"><i data-lucide="copy"></i></button>
            <button class="ocr-download-btn" id="ocrPdfDownload" title="Download"><i data-lucide="download"></i></button>
          </div>
        </div>
        <textarea class="ocr-textarea" id="ocrPdfText" readonly></textarea>
      </div>
    `;
    if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });

    let selectedFile = null;
    const zone = container.querySelector('#ocrPdfZone');
    const input = container.querySelector('#ocrPdfInput');
    const preview = container.querySelector('#ocrPdfPreview');
    const info = container.querySelector('#ocrPdfInfo');
    const removeBtn = container.querySelector('#ocrPdfRemove');
    const recognizeBtn = container.querySelector('#ocrPdfRecognize');
    const progress = container.querySelector('#ocrPdfProgress');
    const fill = container.querySelector('#ocrPdfFill');
    const statusEl = container.querySelector('#ocrPdfStatus');
    const results = container.querySelector('#ocrPdfResults');
    const textarea = container.querySelector('#ocrPdfText');
    const copyBtn = container.querySelector('#ocrPdfCopy');
    const downloadBtn = container.querySelector('#ocrPdfDownload');

    function setFile(file) {
      selectedFile = file;
      zone.classList.add('hidden');
      preview.classList.remove('hidden');
      info.textContent = `${file.name} · ${formatSize(file.size)}`;
      recognizeBtn.disabled = false;
      results.classList.add('hidden');
      progress.classList.add('hidden');
    }

    function clearFile() {
      selectedFile = null;
      zone.classList.remove('hidden');
      preview.classList.add('hidden');
      recognizeBtn.disabled = true;
      results.classList.add('hidden');
      progress.classList.add('hidden');
    }

    zone.addEventListener('click', () => input.click());
    zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('ocr-zone-drag'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('ocr-zone-drag'));
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      zone.classList.remove('ocr-zone-drag');
      if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
    });
    input.addEventListener('change', () => { if (input.files.length) setFile(input.files[0]); });
    removeBtn.addEventListener('click', clearFile);

    recognizeBtn.addEventListener('click', async () => {
      if (!selectedFile) return;
      recognizeBtn.disabled = true;
      progress.classList.remove('hidden');
      results.classList.add('hidden');
      fill.style.width = '20%';
      statusEl.textContent = 'Uploading PDF & processing pages…';

      const form = new FormData();
      form.append('file', selectedFile);

      try {
        fill.style.width = '50%';
        const res = await fetch('/api/ocr/recognize', { method: 'POST', body: form });
        fill.style.width = '90%';
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(err.detail || 'PDF OCR failed');
        }
        const data = await res.json();
        fill.style.width = '100%';
        statusEl.textContent = `Done — ${data.page_count || '?'} pages in ${data.duration_sec || '?'}s`;
        textarea.value = data.full_text || '';
        results.classList.remove('hidden');
      } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
        fill.style.width = '0%';
      } finally {
        recognizeBtn.disabled = false;
      }
    });

    copyBtn.addEventListener('click', async () => {
      await copyToClipboard(textarea.value);
      copyBtn.title = 'Copied!';
      setTimeout(() => { copyBtn.title = 'Copy'; }, 1500);
    });

    downloadBtn.addEventListener('click', () => {
      downloadAsText(textarea.value, (selectedFile?.name || 'ocr') + '.txt');
    });
  }

  function renderMultiPdfTab(container) {
    container.innerHTML = `
      <div class="ocr-upload-zone" id="ocrMultiPdfZone">
        <div class="ocr-zone-icon"><i data-lucide="files"></i></div>
        <div class="ocr-zone-text">Drop PDFs here or click to browse</div>
        <div class="ocr-zone-hint">Multiple PDFs supported</div>
        <input type="file" id="ocrMultiPdfInput" accept=".pdf" multiple hidden>
      </div>
      <div class="ocr-file-list" id="ocrMultiPdfList"></div>
      <button class="ocr-action-btn" id="ocrMultiPdfRecognize" disabled>Process All PDFs</button>
      <div class="ocr-progress hidden" id="ocrMultiPdfProgress">
        <div class="ocr-progress-bar"><div class="ocr-progress-fill" id="ocrMultiPdfFill"></div></div>
        <div class="ocr-progress-text" id="ocrMultiPdfStatus">Processing…</div>
      </div>
      <div class="ocr-batch-results hidden" id="ocrMultiPdfResults"></div>
    `;
    if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });

    let files = [];
    const zone = container.querySelector('#ocrMultiPdfZone');
    const input = container.querySelector('#ocrMultiPdfInput');
    const list = container.querySelector('#ocrMultiPdfList');
    const recognizeBtn = container.querySelector('#ocrMultiPdfRecognize');
    const progress = container.querySelector('#ocrMultiPdfProgress');
    const fill = container.querySelector('#ocrMultiPdfFill');
    const statusEl = container.querySelector('#ocrMultiPdfStatus');
    const resultsDiv = container.querySelector('#ocrMultiPdfResults');

    function renderList() {
      list.innerHTML = '';
      files.forEach((f, i) => {
        const item = document.createElement('div');
        item.className = 'ocr-file-item';
        item.innerHTML = `
          <div class="ocr-file-icon"><i data-lucide="file-text"></i></div>
          <span class="ocr-file-name">${escHtml(f.name)}</span>
          <span class="ocr-file-size">${formatSize(f.size)}</span>
          <button class="ocr-file-remove" data-idx="${i}"><i data-lucide="x"></i></button>
        `;
        list.appendChild(item);
      });
      if (window.lucide) lucide.createIcons({ nodes: list.querySelectorAll('[data-lucide]') });
      list.querySelectorAll('.ocr-file-remove').forEach(btn => {
        btn.addEventListener('click', () => {
          files.splice(parseInt(btn.dataset.idx), 1);
          renderList();
          recognizeBtn.disabled = files.length === 0;
        });
      });
      recognizeBtn.disabled = files.length === 0;
    }

    function addFiles(newFiles) {
      for (const f of newFiles) files.push(f);
      renderList();
    }

    zone.addEventListener('click', () => input.click());
    zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('ocr-zone-drag'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('ocr-zone-drag'));
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      zone.classList.remove('ocr-zone-drag');
      addFiles(e.dataTransfer.files);
    });
    input.addEventListener('change', () => { addFiles(input.files); input.value = ''; });

    recognizeBtn.addEventListener('click', async () => {
      if (!files.length) return;
      recognizeBtn.disabled = true;
      progress.classList.remove('hidden');
      resultsDiv.classList.add('hidden');
      resultsDiv.innerHTML = '';

      // Use SSE streaming for parallel processing with real-time progress
      const form = new FormData();
      files.forEach(f => form.append('files', f));

      fill.style.width = '5%';
      statusEl.textContent = `Uploading ${files.length} PDFs…`;

      let allResults = null;

      try {
        const res = await fetch('/api/ocr/stream', { method: 'POST', body: form });
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(err.detail || 'Multi-PDF OCR failed');
        }

        fill.style.width = '10%';
        statusEl.textContent = `Processing 0/${files.length} PDFs…`;

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const msg = JSON.parse(line.slice(6));
              if (msg.type === 'progress') {
                const pct = Math.round(10 + (msg.files_completed / msg.files_total) * 85);
                fill.style.width = `${pct}%`;
                let statusTxt = `Processing ${msg.files_completed}/${msg.files_total} PDFs`;
                if (msg.pages_total) {
                  statusTxt += ` — ${msg.source}: ${msg.pages_completed}/${msg.pages_total} pages`;
                }
                statusEl.textContent = statusTxt;
              } else if (msg.type === 'complete') {
                allResults = msg.results;
              }
            } catch {}
          }
        }

        if (!allResults) throw new Error('Stream ended without results');
      } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
        fill.style.width = '0%';
        recognizeBtn.disabled = false;
        return;
      }

      fill.style.width = '100%';
      statusEl.textContent = `Done — ${files.length} PDFs processed`;

      resultsDiv.innerHTML = '';
      allResults.forEach((r, i) => {
        const card = document.createElement('div');
        card.className = 'ocr-batch-card';
        const text = r.error ? `Error: ${r.error}` : (r.full_text || '');
        const fname = r.source_filename || files[i]?.name || `PDF ${i + 1}`;
        const pageCount = r.page_count ? ` (${r.page_count} pages)` : '';
        card.innerHTML = `
          <div class="ocr-batch-card-header" data-idx="${i}">
            <span class="ocr-batch-card-name">${escHtml(fname)}${pageCount}</span>
            <span class="ocr-batch-card-toggle">▼</span>
          </div>
          <div class="ocr-batch-card-body hidden" id="ocrMultiPdfBody${i}">
            <textarea class="ocr-textarea" readonly>${escHtml(text)}</textarea>
            <div class="ocr-results-actions">
              <button class="ocr-copy-btn" data-copy="${i}" title="Copy"><i data-lucide="copy"></i></button>
              <button class="ocr-download-btn" data-dl="${i}" title="Download"><i data-lucide="download"></i></button>
            </div>
          </div>
        `;
        resultsDiv.appendChild(card);
      });

      if (window.lucide) lucide.createIcons({ nodes: resultsDiv.querySelectorAll('[data-lucide]') });

      resultsDiv.querySelectorAll('.ocr-batch-card-header').forEach(hdr => {
        hdr.addEventListener('click', () => {
          const idx = hdr.dataset.idx;
          const body = resultsDiv.querySelector(`#ocrMultiPdfBody${idx}`);
          if (body) body.classList.toggle('hidden');
          const toggle = hdr.querySelector('.ocr-batch-card-toggle');
          if (toggle) toggle.textContent = body?.classList.contains('hidden') ? '▼' : '▲';
        });
      });

      resultsDiv.querySelectorAll('[data-copy]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const idx = btn.dataset.copy;
          const ta = resultsDiv.querySelector(`#ocrMultiPdfBody${idx} textarea`);
          if (ta) await copyToClipboard(ta.value);
        });
      });

      resultsDiv.querySelectorAll('[data-dl]').forEach(btn => {
        btn.addEventListener('click', () => {
          const idx = btn.dataset.dl;
          const ta = resultsDiv.querySelector(`#ocrMultiPdfBody${idx} textarea`);
          const name = allResults[idx]?.source_filename || `pdf${idx}`;
          if (ta) downloadAsText(ta.value, name + '.txt');
        });
      });

      resultsDiv.classList.remove('hidden');
      recognizeBtn.disabled = false;
    });
  }

  /* ── Main Panel Renderer ── */
  function renderOcrPanel(container) {
    container.innerHTML = `
      <div class="ocr-panel">
        <div class="ocr-panel-header">
          <h2 class="ocr-panel-title"><i data-lucide="scan-text" class="icon-lucide"></i> OCR Text Recognition</h2>
        </div>
        <div class="kb-mode-tabs ocr-tabs">
          <button class="kb-mode-tab${currentTab === 'single' ? ' active' : ''}" data-ocr-tab="single">Single Image</button>
          <button class="kb-mode-tab${currentTab === 'batch' ? ' active' : ''}" data-ocr-tab="batch">Batch Images</button>
          <button class="kb-mode-tab${currentTab === 'pdf' ? ' active' : ''}" data-ocr-tab="pdf">PDF</button>
          <button class="kb-mode-tab${currentTab === 'multipdf' ? ' active' : ''}" data-ocr-tab="multipdf">Multi-PDF</button>
        </div>
        <div class="ocr-tab-content" id="ocrTabContent"></div>
      </div>
    `;
    if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });

    const content = container.querySelector('#ocrTabContent');
    const tabs = container.querySelectorAll('[data-ocr-tab]');

    function switchTab(tab) {
      currentTab = tab;
      tabs.forEach(t => t.classList.toggle('active', t.dataset.ocrTab === tab));
      content.innerHTML = '';
      if (tab === 'single') renderSingleTab(content);
      else if (tab === 'batch') renderBatchTab(content);
      else if (tab === 'pdf') renderPdfTab(content);
      else if (tab === 'multipdf') renderMultiPdfTab(content);
    }

    tabs.forEach(t => {
      t.addEventListener('click', () => switchTab(t.dataset.ocrTab));
    });

    switchTab(currentTab);
  }

  /* ── Main Area View ── */
  function openOcrView() {
    const view = document.getElementById('ocrView');
    if (!view) return;
    hideOtherViews();
    document.body.classList.add('ocr-open');
    view.classList.remove('hidden');
    // Only render once — preserve DOM/state across tab switches
    if (!view.querySelector('.ocr-panel')) {
      renderOcrPanel(view);
    }
    isOpen = true;
  }

  function closeOcrView() {
    if (!isOpen) return;
    document.body.classList.remove('ocr-open');
    const view = document.getElementById('ocrView');
    if (view) view.classList.add('hidden');
    showChat();
    isOpen = false;
  }

  /* ── Rail-switch handler ── */
  window.addEventListener('rail-switch', (e) => {
    const target = e.detail?.target;
    if (target === 'ocr') {
      if (isOpen) {
        closeOcrView();
      } else {
        openOcrView();
      }
    } else if (isOpen) {
      closeOcrView();
    }
  });

  /* ── Init ── */
  function init() {
    // No sidebar widget needed for OCR (no recent results API)
    // Panel is self-contained in main area
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
