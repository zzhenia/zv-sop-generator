/* SOP Generator — Frontend */

let config = { users: [], statuses: [], parent_folders: {} };
let sopData = { title: '', markdown: '', html_preview: '', metadata: {} };
let nextId = '---';

// ── API helper ──────────────────────────────────────────────────────────────

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.status === 204 ? null : res.json();
}

// ── Toast notifications ─────────────────────────────────────────────────────

function toast(message, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast toast--${type}`;
  el.innerHTML = message;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

// ── Initialisation ──────────────────────────────────────────────────────────

async function init() {
  try {
    config = await api('GET', '/api/config');
  } catch (e) {
    toast('Failed to load config: ' + e.message, 'error');
    return;
  }

  // Apply branding
  applyBranding(config);

  // Populate user datalists
  populateDatalist('author-list', config.users);
  populateDatalist('approver-list', config.users);
  populateDatalist('owner-list', config.users);

  // Populate status dropdown
  populateSelect('status', config.statuses.map(s => ({ key: s, display: s })), 'key', 'display');
  document.getElementById('status').value = 'DRAFT';

  // Populate parent folders in publish modal
  const folderSelect = document.getElementById('parent-folder');
  for (const [label, id] of Object.entries(config.parent_folders)) {
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = label;
    folderSelect.appendChild(opt);
  }

  // Fetch next SOP ID
  try {
    const data = await api('GET', '/api/next-id');
    nextId = data.next_id;
    document.getElementById('sop-id-badge').textContent = nextId;
  } catch (e) {
    document.getElementById('sop-id-badge').textContent = 'PI-SOP-???';
  }

  // Add first Loom field
  addLoomField();

  // Set up drop zone
  setupDropZone();
}

function populateSelect(id, items, valueKey, labelKey) {
  const select = document.getElementById(id);
  select.innerHTML = '<option value="">-- Select --</option>';
  for (const item of items) {
    const opt = document.createElement('option');
    opt.value = typeof item === 'object' ? item[valueKey] : item;
    opt.textContent = typeof item === 'object' ? item[labelKey] : item;
    select.appendChild(opt);
  }
}

function populateDatalist(datalistId, users) {
  const dl = document.getElementById(datalistId);
  dl.innerHTML = '';
  for (const user of users) {
    const opt = document.createElement('option');
    opt.value = user.display;
    dl.appendChild(opt);
  }
}

function applyBranding(config) {
  const brand = config.brand || {};

  // Header background
  const header = document.getElementById('app-header');
  if (brand.header_color) header.style.background = brand.header_color;

  // Logo
  const logo = document.getElementById('app-logo');
  if (brand.logo) {
    logo.src = brand.logo;
    logo.alt = brand.org || '';
    logo.style.display = '';
  } else {
    logo.style.display = 'none';
  }

  // Title
  const titleEl = document.getElementById('app-title');
  titleEl.textContent = [brand.org, brand.title].filter(Boolean).join(' — ') || 'SOP Generator';
  document.title = titleEl.textContent;

  // Publish modal target label
  const targetLabel = document.getElementById('publish-target-label');
  if (targetLabel) targetLabel.textContent = `Target: ${brand.org || config.mode}`;
}

// ── Loom fields ─────────────────────────────────────────────────────────────

let loomCount = 0;

function addLoomField() {
  if (loomCount >= 5) return;
  loomCount++;
  const container = document.getElementById('loom-fields');
  const row = document.createElement('div');
  row.className = 'loom-row';
  row.innerHTML = `
    <input type="text" placeholder="https://www.loom.com/share/..." class="loom-url">
    <span class="loom-status"></span>
    <button class="btn btn-sm" data-action="remove-loom-field" title="Remove">&times;</button>
  `;
  container.appendChild(row);
}

function removeLoomField(target) {
  const row = target.closest('.loom-row');
  if (row) {
    row.remove();
    loomCount--;
  }
}

async function fetchAllLoom() {
  const fields = document.querySelectorAll('.loom-url');
  const textarea = document.getElementById('raw-text');
  let fetched = 0;

  const promises = Array.from(fields).map(async (input) => {
    const url = input.value.trim();
    if (!url) return;
    const status = input.parentElement.querySelector('.loom-status');
    status.textContent = '...';

    try {
      const data = await api('POST', '/api/fetch-loom', { url });
      textarea.value += `\n\n--- Loom Transcript (${data.video_id}) ---\n\n${data.transcript}`;
      status.textContent = 'OK';
      status.style.color = '#006644';
      fetched++;
    } catch (e) {
      status.textContent = 'ERR';
      status.style.color = '#bf2600';
      toast('Loom fetch failed: ' + e.message, 'error');
    }
  });

  await Promise.all(promises);
  if (fetched > 0) toast(`Fetched ${fetched} transcript(s).`, 'success');
}

// ── File upload ─────────────────────────────────────────────────────────────

function setupDropZone() {
  const zone = document.getElementById('drop-zone');
  const input = document.getElementById('file-input');

  zone.addEventListener('click', () => input.click());
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    handleFiles(e.dataTransfer.files);
  });
  input.addEventListener('change', () => { handleFiles(input.files); input.value = ''; });
}

async function handleFiles(fileList) {
  const textarea = document.getElementById('raw-text');
  const chipContainer = document.getElementById('file-list');

  for (const file of fileList) {
    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await fetch('/api/upload', { method: 'POST', body: formData });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || res.statusText);
      }
      const data = await res.json();
      textarea.value += `\n\n--- File: ${data.filename} ---\n\n${data.text}`;

      // Add chip
      const chip = document.createElement('span');
      chip.className = 'file-chip';
      chip.innerHTML = `${data.filename} <button data-action="remove-file">&times;</button>`;
      chipContainer.appendChild(chip);
    } catch (e) {
      toast('File upload failed: ' + e.message, 'error');
    }
  }
}

// ── Pre-fill metadata ───────────────────────────────────────────────────────

async function handlePrefill() {
  const rawText = document.getElementById('raw-text').value.trim();
  if (!rawText) return toast('Add some input text first (paste, upload, or fetch Loom transcripts).', 'error');

  const btn = document.getElementById('prefill-btn');
  btn.classList.add('loading');
  btn.textContent = 'Extracting...';

  try {
    const data = await api('POST', '/api/extract-metadata', { raw_text: rawText });

    if (data.title) document.getElementById('sop-title').value = data.title;
    if (data.tools_required) document.getElementById('tools-required').value = data.tools_required;

    // Set user fields (Claude returns display names or @handles)
    for (const field of ['author', 'approver', 'owner']) {
      if (data[field]) {
        const name = data[field].replace(/^@/, '');
        // Try exact match first, then partial match
        const exact = config.users.find(u => u.display.toLowerCase() === name.toLowerCase());
        const partial = config.users.find(u => u.display.toLowerCase().includes(name.toLowerCase()));
        document.getElementById(field).value = exact ? exact.display : (partial ? partial.display : name);
      }
    }

    toast('Metadata extracted and fields updated.', 'success');
  } catch (e) {
    toast('Pre-fill failed: ' + e.message, 'error');
  } finally {
    btn.classList.remove('loading');
    btn.textContent = 'Pre-fill Metadata from Input';
  }
}

// ── Generate SOP ────────────────────────────────────────────────────────────

async function handleGenerate() {
  const title = document.getElementById('sop-title').value.trim();
  const author = document.getElementById('author').value;
  const approver = document.getElementById('approver').value;
  const owner = document.getElementById('owner').value;
  const tools = document.getElementById('tools-required').value.trim();
  const status = document.getElementById('status').value || 'DRAFT';
  const rawText = document.getElementById('raw-text').value.trim();

  if (!title) return toast('Title is required.', 'error');
  if (!author) return toast('Author is required.', 'error');
  if (!rawText) return toast('Please provide some input text.', 'error');

  const btn = document.getElementById('generate-btn');
  btn.classList.add('loading');
  btn.textContent = 'Generating...';

  try {
    sopData = await api('POST', '/api/generate', {
      title,
      sop_id: nextId,
      author,
      approver: approver || '',
      owner: owner || '',
      tools_required: tools,
      status,
      raw_text: rawText,
    });

    // Render preview
    document.getElementById('preview-container').innerHTML = sopData.html_preview;

    // Enable action buttons
    document.getElementById('btn-dl-md').disabled = false;
    document.getElementById('btn-dl-pdf').disabled = false;
    document.getElementById('btn-publish').disabled = false;

    toast('SOP generated successfully.', 'success');
  } catch (e) {
    toast('Generation failed: ' + e.message, 'error');
  } finally {
    btn.classList.remove('loading');
    btn.textContent = 'Generate SOP';
  }
}

// ── Downloads ───────────────────────────────────────────────────────────────

async function handleDownloadMd() {
  if (!sopData.markdown) return;
  const slug = sopData.title.replace(/\[SOP\]\s*/i, '').replace(/\s+/g, '-').toLowerCase();
  const filename = `${nextId}-${slug}.md`;

  const res = await fetch('/api/download/md', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ markdown: sopData.markdown, filename }),
  });
  downloadBlob(await res.blob(), filename);
}

async function handleDownloadPdf() {
  if (!sopData.html_preview) return;
  const slug = sopData.title.replace(/\[SOP\]\s*/i, '').replace(/\s+/g, '-').toLowerCase();
  const filename = `${nextId}-${slug}.pdf`;

  const btn = document.getElementById('btn-dl-pdf');
  btn.classList.add('loading');
  try {
    const res = await fetch('/api/download/pdf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ html: sopData.html_preview, filename }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    downloadBlob(await res.blob(), filename);
  } catch (e) {
    toast('PDF download failed: ' + e.message, 'error');
  } finally {
    btn.classList.remove('loading');
  }
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Publish ─────────────────────────────────────────────────────────────────

function showPublishModal() {
  document.getElementById('publish-modal').classList.add('visible');
}

function hidePublishModal() {
  document.getElementById('publish-modal').classList.remove('visible');
}

async function handlePublish() {
  const parentId = document.getElementById('parent-folder').value;
  if (!parentId) return toast('Select a parent folder.', 'error');

  const btn = document.querySelector('[data-action="confirm-publish"]');
  btn.classList.add('loading');

  try {
    const publishAsDraft = document.getElementById('publish-as-draft').checked;

    // Rebuild metadata from current form state (user may have changed fields after generation)
    const currentMetadata = {
      ...sopData.metadata,
      author: document.getElementById('author').value.trim(),
      approver: document.getElementById('approver').value.trim(),
      owner: document.getElementById('owner').value.trim(),
      status: document.getElementById('status').value || 'DRAFT',
    };

    const result = await api('POST', '/api/publish', {
      title: sopData.title,
      markdown: sopData.markdown,
      metadata: currentMetadata,
      parent_id: parentId,
      publish_as_draft: publishAsDraft,
    });
    hidePublishModal();
    toast(`Published! <a href="${result.page_url}" target="_blank">Open in Confluence</a>`, 'success');

    // Refresh the next SOP ID since we just used the current one
    try {
      const data = await api('GET', '/api/next-id');
      nextId = data.next_id;
      document.getElementById('sop-id-badge').textContent = nextId;
    } catch (e) { /* ignore */ }
  } catch (e) {
    toast('Publish failed: ' + e.message, 'error');
  } finally {
    btn.classList.remove('loading');
  }
}

// ── Event delegation ────────────────────────────────────────────────────────

document.body.addEventListener('click', async (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;

  switch (el.dataset.action) {
    case 'prefill-fields':  await handlePrefill(); break;
    case 'generate-sop':    await handleGenerate(); break;
    case 'fetch-all-loom':  await fetchAllLoom(); break;
    case 'add-loom-field':  addLoomField(); break;
    case 'remove-loom-field': removeLoomField(el); break;
    case 'remove-file':     el.closest('.file-chip')?.remove(); break;
    case 'download-md':     await handleDownloadMd(); break;
    case 'download-pdf':    await handleDownloadPdf(); break;
    case 'publish':         showPublishModal(); break;
    case 'confirm-publish': await handlePublish(); break;
    case 'cancel-publish':  hidePublishModal(); break;
  }
});

// ── Boot ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', init);
