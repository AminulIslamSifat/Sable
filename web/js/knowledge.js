/**
 * knowledge.js — Knowledge Base panel for the activity rail.
 *
 * Three modes: Graph (Cytoscape force-directed), Cards (masonry grid), Search.
 * Sidebar with category filters + stats. Detail panel slides up on node/card click.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  /* ── State ── */
  let isOpen = false;
  let currentMode = 'graph'; // graph | cards | search
  let entries = [];           // all memory entries (flattened)
  let protectedEntries = [];
  let activeCategories = new Set(['semantic', 'episodic', 'procedural', 'ephemeral', 'protected']);
  let cy = null;              // Cytoscape instance
  let selectedNodeId = null;
  let toastTimer = null;
  let searchDebounce = null;
  let nodeFilterText = '';
  let _cachedGraphData = null; // { nodes, edges, filterKey } — skip rebuild when unchanged

  /* ── Stopwords for keyword extraction ── */
  const STOPWORDS = new Set([
    'the','a','an','is','are','was','were','be','been','being','have','has','had',
    'do','does','did','will','would','could','should','may','might','shall','can',
    'to','of','in','for','on','with','at','by','from','as','into','through','during',
    'before','after','above','below','between','out','off','over','under','again',
    'further','then','once','here','there','when','where','why','how','all','each',
    'every','both','few','more','most','other','some','such','no','nor','not','only',
    'own','same','so','than','too','very','just','because','but','and','or','if',
    'while','about','up','it','its','i','me','my','we','our','you','your','he',
    'she','they','them','this','that','these','those','what','which','who','whom',
    // Tech noise words — too common across memories to be meaningful
    'path','file','code','fix','bug','error','new','old','use','used','using',
    'set','get','add','run','call','check','make','work','works','working',
    'now','also','even','still','need','needs','like','one','two','first',
    'true','false','null','none','default','value','values','type','types',
    'data','name','names','line','lines','text','string','strings','list',
    'found','return','returns','result','results','based','using','via',
    'must','should','when','any','all','each','per','etc','max','min',
    'server','client','api','url','http','json','config','log','logs',
  ]);

  /* ── Helpers ── */
  function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  function timeAgo(iso) {
    if (!iso) return '';
    const d = new Date(typeof iso === 'string' ? iso.replace(' ', 'T') + 'Z' : iso);
    const now = new Date();
    const diff = Math.floor((now - d) / 1000);
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
    return d.toLocaleDateString();
  }

  function extractKeywords(text, maxKw = 8) {
    if (!text) return [];
    const words = text.toLowerCase()
      .replace(/[^a-z0-9\s-]/g, ' ')
      .split(/\s+/)
      .filter(w => w.length > 2 && !STOPWORDS.has(w));
    // Deduplicate and take top N
    const seen = new Set();
    const result = [];
    for (const w of words) {
      if (!seen.has(w)) {
        seen.add(w);
        result.push(w);
      }
      if (result.length >= maxKw) break;
    }
    return result;
  }

  function getCategoryColor(cat) {
    const style = getComputedStyle(document.documentElement);
    switch (cat) {
      case 'semantic': return style.getPropertyValue('--accent').trim() || '#9a7d4a';
      case 'episodic': return style.getPropertyValue('--ok').trim() || '#6fcf97';
      case 'procedural': return style.getPropertyValue('--info').trim() || '#56b6c2';
      case 'ephemeral': return '#e5a84a';
      case 'protected': return style.getPropertyValue('--accent-text').trim() || '#c4a66b';
      default: return style.getPropertyValue('--muted').trim() || '#85858c';
    }
  }

  function getCategoryIcon(cat) {
    switch (cat) {
      case 'semantic': return '📌';
      case 'episodic': return '🎓';
      case 'procedural': return '⚙️';
      case 'ephemeral': return '⏳';
      case 'protected': return '🔒';
      default: return '💭';
    }
  }

  /* ── Data loading ── */
  async function loadEntries() {
    try {
      const [memRes, protRes] = await Promise.all([
        fetch('/api/settings/memory'),
        fetch('/api/settings/memory/protected'),
      ]);

      const memData = memRes.ok ? await memRes.json() : { memory: {} };
      const protData = protRes.ok ? await protRes.json() : { protected: [] };

      const mem = memData.memory || {};
      entries = [];
      _cachedGraphData = null; // invalidate graph cache on data reload

      for (const cat of ['semantic', 'episodic', 'procedural', 'ephemeral']) {
        const list = mem[cat] || [];
        for (const e of list) {
          entries.push({
            key: e.key || '',
            value: e.value || '',
            category: cat,
            trigger: e.trigger || null,
            keywords: e.keywords || [],
            expires_at: e.expires_at || null,
            created_at: e.created_at || null,
            updated_at: e.updated_at || null,
          });
        }
      }

      protectedEntries = (protData.protected || []).map(e => ({
        key: e.key || '',
        value: e.value || '',
        category: 'protected',
        expires_at: null,
        created_at: e.created_at || null,
        updated_at: e.updated_at || null,
      }));

      updateStats();
      // Only render if already open and visible (avoid rendering on hidden container)
      if (isOpen) {
        const container = $('kbGraphCanvas');
        const rect = container?.getBoundingClientRect();
        if (rect && rect.width > 0 && rect.height > 0) {
          renderCurrentMode();
        }
        // Otherwise the triple-rAF in openKnowledge will handle it
      }
    } catch (e) {
      console.error('KB: Failed to load entries:', e);
    }
  }

  function getAllEntries() {
    return [...entries, ...protectedEntries];
  }

  function getFilteredEntries() {
    let filtered = getAllEntries().filter(e => activeCategories.has(e.category));
    if (nodeFilterText) {
      const q = nodeFilterText.toLowerCase();
      filtered = filtered.filter(e =>
        (e.key || '').toLowerCase().includes(q) ||
        (e.value || '').toLowerCase().includes(q)
      );
    }
    return filtered;
  }

  function updateStats() {
    const all = getAllEntries();
    const counts = { semantic: 0, episodic: 0, procedural: 0, ephemeral: 0, protected: 0 };
    for (const e of all) {
      if (counts[e.category] !== undefined) counts[e.category]++;
    }

    $('kbCountSemantic').textContent = counts.semantic;
    $('kbCountEpisodic').textContent = counts.episodic;
    $('kbCountProcedural').textContent = counts.procedural;
    $('kbCountEphemeral').textContent = counts.ephemeral;
    $('kbCountProtected').textContent = counts.protected;
    $('kbStatTotal').textContent = all.length;

    const syncDot = $('kbSyncDot');
    const syncLabel = $('kbSyncLabel');
    if (all.length > 0) {
      syncDot.classList.remove('stale');
      syncLabel.textContent = 'Synced just now';
    } else {
      syncLabel.textContent = 'No entries';
    }
  }

  /* ── Mode switching ── */
  function setMode(mode) {
    currentMode = mode;

    // Update tab UI
    document.querySelectorAll('.kb-mode-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.kbMode === mode);
    });

    // Show/hide views
    $('kbGraphWrap').style.display = mode === 'graph' ? '' : 'none';
    $('kbCardsView').classList.toggle('hidden', mode !== 'cards');
    $('kbSearchView').classList.toggle('hidden', mode !== 'search');

    // Close detail panel on mode switch
    closeDetailPanel();

    renderCurrentMode();
  }

  function renderCurrentMode() {
    if (currentMode === 'graph') renderGraph();
    else if (currentMode === 'cards') renderCards();
    // Search renders on input, not on mode switch
  }

  /* ── Graph rendering (Cytoscape.js) ── */
  async function renderGraph() {
    const container = $('kbGraphCanvas');
    const loading = $('kbGraphLoading');
    const empty = $('kbGraphEmpty');

    if (!container) return;

    const filtered = getFilteredEntries();

    if (filtered.length === 0) {
      loading.classList.add('hidden');
      empty.classList.remove('hidden');
      if (cy) { cy.destroy(); cy = null; }
      return;
    }

    empty.classList.add('hidden');
    loading.classList.remove('hidden');

    // ── Build graph data (cached to avoid O(n²) rebuild on every render) ──
    const filterKey = filtered.map(e => `${e.category}:${e.key}`).join('\x00');
    let nodes, edges;

    if (_cachedGraphData && _cachedGraphData.filterKey === filterKey) {
      nodes = _cachedGraphData.nodes;
      edges = _cachedGraphData.edges;
    } else {
      nodes = filtered.map((e, i) => ({
        data: {
          id: `n${i}`,
          label: (e.key || '').slice(0, 30),
          category: e.category,
          fullKey: e.key,
          fullValue: e.value,
          keywords: extractKeywords(`${e.key} ${e.value}`),
          color: getCategoryColor(e.category),
          isProtected: e.category === 'protected',
          isEphemeral: e.category === 'ephemeral',
          entryIndex: i,
        }
      }));

      // Build edges using inverted keyword index — O(n*k) instead of O(n²*k)
      const kwIndex = new Map(); // keyword -> [nodeIndex]
      for (let i = 0; i < nodes.length; i++) {
        for (const kw of nodes[i].data.keywords) {
          if (!kwIndex.has(kw)) kwIndex.set(kw, []);
          kwIndex.get(kw).push(i);
        }
      }

      // Count shared keywords per pair via co-occurrence
      const pairCounts = new Map(); // "i-j" -> count
      for (const indices of kwIndex.values()) {
        for (let a = 0; a < indices.length; a++) {
          for (let b = a + 1; b < indices.length; b++) {
            const lo = indices[a], hi = indices[b];
            const key = `${lo}-${hi}`;
            pairCounts.set(key, (pairCounts.get(key) || 0) + 1);
          }
        }
      }

      edges = [];
      for (const [key, count] of pairCounts) {
        if (count >= 2) {
          const [si, ti] = key.split('-').map(Number);
          edges.push({
            data: {
              source: nodes[si].data.id,
              target: nodes[ti].data.id,
              weight: Math.min(count, 3),
            }
          });
        }
      }

      // Uniform small circles — Obsidian-style
      for (const n of nodes) n.data.size = 8;

      _cachedGraphData = { nodes, edges, filterKey };
    }

    // Load Cytoscape dynamically if not loaded
    if (typeof cytoscape === 'undefined') {
      try {
        await loadCytoscape();
      } catch (e) {
        console.error('KB: Failed to load Cytoscape:', e);
        loading.innerHTML = '<span style="color:var(--danger)">Failed to load graph library</span>';
        return;
      }
    }

    // Verify container has dimensions (must be visible)
    const rect = container.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) {
      console.warn('KB: Graph container has zero dimensions, deferring...');
      loading.classList.add('hidden');
      // Retry after layout settles
      setTimeout(() => renderGraph(), 200);
      return;
    }

    // Destroy old instance
    if (cy) { cy.destroy(); cy = null; }

    loading.classList.add('hidden');

    // Read CSS vars for styling
    const style = getComputedStyle(document.documentElement);
    const borderColor = style.getPropertyValue('--border').trim() || '#2a2720';
    const textColor = style.getPropertyValue('--text-dim').trim() || '#c4c4c8';
    const accentColor = style.getPropertyValue('--accent').trim() || '#9a7d4a';
    const accentDim = style.getPropertyValue('--accent-dim').trim() || '#9a7d4a0e';

    cy = cytoscape({
      container,
      elements: [...nodes, ...edges],
      style: [
        {
          selector: 'node',
          style: {
            'width': 8,
            'height': 8,
            'shape': 'ellipse',
            'background-color': 'data(color)',
            'background-opacity': 0.85,
            'border-width': 0,
            'label': '',
            'transition-property': 'background-opacity, width, height',
            'transition-duration': '0.2s',
          }
        },
        {
          selector: 'node.hover',
          style: {
            'width': 12,
            'height': 12,
            'background-opacity': 1,
            'label': 'data(label)',
            'text-valign': 'bottom',
            'text-margin-y': 6,
            'font-size': '4px',
            'color': '#ffffff',
            'text-opacity': 1,
            'font-family': "'Maple Mono', monospace",
            'text-outline-width': 1,
            'text-outline-color': '#0f0e17',
            'text-max-width': '140px',
            'text-wrap': 'ellipsis',
            'z-index': 999,
          }
        },
        {
          selector: 'node:selected',
          style: {
            'width': 12,
            'height': 12,
            'background-opacity': 1,
            'label': 'data(label)',
            'text-valign': 'bottom',
            'text-margin-y': 6,
            'font-size': '4px',
            'color': '#ffffff',
            'text-opacity': 1,
            'font-family': "'Maple Mono', monospace",
            'text-outline-width': 2,
            'text-outline-color': '#0f0e17',
            'text-max-width': '140px',
            'text-wrap': 'ellipsis',
            'z-index': 999,
          }
        },
        {
          selector: 'node.faded',
          style: {
            'opacity': 0.2,
          }
        },
        {
          selector: 'edge',
          style: {
            'width': 1,
            'line-color': borderColor,
            'opacity': 0.25,
            'curve-style': 'bezier',
            'transition-property': 'opacity, line-color',
            'transition-duration': '0.2s',
          }
        },
        {
          selector: 'edge.highlighted',
          style: {
            'line-color': accentColor,
            'opacity': 0.6,
            'width': 1.5,
          }
        },
        {
          selector: 'edge.faded',
          style: {
            'opacity': 0.04,
          }
        },
      ],
      layout: { name: 'preset' },
      minZoom: 0.1,
      maxZoom: 8,
      wheelSensitivity: 0.5,
      boxSelectionEnabled: false,
    });

    // ── cose-bilkent layout — fast force-directed, no clustering overhead ──
    const layout = cy.layout({
      name: 'cose-bilkent',
      animate: 'end',
      animationDuration: 400,
      fit: true,
      padding: 60,
      randomize: false,
      nodeRepulsion: 1500,
      idealEdgeLength: 30,
      edgeElasticity: 0.1,
      numIter: 100,
      tile: true,
      tilingPaddingVertical: 10,
      tilingPaddingHorizontal: 10,
      gravityRangeCompound: 1.5,
      gravityCompound: 1.0,
      gravityRange: 3.8,
    });

    layout.run();

    // ── Interactions ──
    cy.on('tap', 'node', (evt) => {
      const node = evt.target;
      selectNode(node);
    });

    cy.on('tap', (evt) => {
      if (evt.target === cy) {
        deselectNode();
      }
    });

    cy.on('mouseover', 'node', (evt) => {
      const node = evt.target;
      node.addClass('hover');
      // Highlight connected edges and dim others
      const neighborhood = node.closedNeighborhood();
      cy.elements().addClass('faded');
      neighborhood.removeClass('faded');
      neighborhood.connectedEdges().addClass('highlighted');
    });

    cy.on('mouseout', 'node', () => {
      cy.elements().removeClass('faded highlighted hover');
    });

    // Store entry data on nodes for detail panel
    cy.nodes().forEach((node, i) => {
      node.data('entry', filtered[i]);
    });

    // ── Zoom-based label visibility, dot brightness & screen-space sizing ──
    const LABEL_ZOOM_THRESHOLD = 3.0;
    const MIN_SCREEN_PX = 4;   // minimum node size in screen pixels (Obsidian-like)
    const BASE_NODE_PX = 8;    // natural node size at zoom=1
    let _lastShowLabels = null;
    let _lastZoomBucket = -1;

    function updateZoomEffects() {
      const z = cy.zoom();
      const showLabels = z >= LABEL_ZOOM_THRESHOLD;
      // Bucket zoom to avoid per-frame style updates
      const zoomBucket = Math.round(z * 20);
      if (showLabels === _lastShowLabels && zoomBucket === _lastZoomBucket) return;
      _lastShowLabels = showLabels;
      _lastZoomBucket = zoomBucket;

      // Screen-space compensation: keep nodes at least MIN_SCREEN_PX on screen
      // At zoom z, a graph-space size of s renders as s*z screen pixels.
      // We want max(BASE_NODE_PX, MIN_SCREEN_PX / z) so dots never vanish.
      const compensatedSize = Math.max(BASE_NODE_PX, MIN_SCREEN_PX / z);
      const t = Math.min(Math.max((z - 0.1) / (LABEL_ZOOM_THRESHOLD - 0.1), 0), 1);
      const opacity = 1.0 - t * 0.15;

      cy.batch(() => {
        cy.nodes().forEach(n => {
          if (n.hasClass('hover') || n.selected()) return;
          const base = { 'background-opacity': opacity, 'width': compensatedSize, 'height': compensatedSize };
          if (showLabels) {
            n.style(Object.assign(base, {
              'label': n.data('label'),
              'font-size': '4px',
              'color': '#ffffff',
              'text-opacity': 1,
              'text-outline-width': 1,
              'text-outline-color': '#0f0e17',
            }));
          } else {
            n.removeStyle();
            n.style(Object.assign(base, { 'label': '' }));
          }
        });
      });
    }
    cy.on('zoom', updateZoomEffects);
    // Wait for layout + fit to fully complete before first check
    cy.ready(() => {
      setTimeout(updateZoomEffects, 100);
    });
  }

  function selectNode(node) {
    cy.elements().removeClass('selected');
    node.select();
    selectedNodeId = node.id();
    const entry = node.data('entry');
    if (entry) showDetailPanel(entry);
  }

  function deselectNode() {
    if (cy) cy.elements().removeClass('selected');
    selectedNodeId = null;
    closeDetailPanel();
  }

  /* ── Cards rendering ── */
  function renderCards() {
    const grid = $('kbCardsGrid');
    if (!grid) return;

    const filtered = getFilteredEntries();
    grid.innerHTML = '';

    if (filtered.length === 0) {
      grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:var(--sp-8);color:var(--muted);font-size:13px;">No entries match current filters</div>';
      return;
    }

    filtered.forEach((e, i) => {
      const card = document.createElement('div');
      card.className = `kb-card ${e.category}`;
      card.style.animationDelay = `${Math.min(i * 30, 300)}ms`;
      card.innerHTML = `
        <div class="kb-card-header">
          <span class="kb-card-icon">${getCategoryIcon(e.category)}</span>
          <span class="kb-card-key">${escHtml(e.key)}</span>
        </div>
        <div class="kb-card-value">${escHtml(e.value)}</div>
        <div class="kb-card-footer">
          <div class="kb-card-footer-left">
            <span class="kb-card-category">${e.category}</span>
            <span class="kb-card-time">${timeAgo(e.updated_at || e.created_at)}</span>
          </div>
          <div class="kb-card-actions">
            <button class="kb-card-action-btn" data-action="edit" title="Edit">✏️</button>
            <button class="kb-card-action-btn danger" data-action="delete" title="Delete">🗑</button>
          </div>
        </div>
      `;

      card.addEventListener('click', (ev) => {
        if (ev.target.closest('[data-action]')) {
          const action = ev.target.closest('[data-action]').dataset.action;
          if (action === 'delete') deleteEntry(e);
          else if (action === 'edit') showDetailPanel(e, true);
          return;
        }
        showDetailPanel(e);
      });

      grid.appendChild(card);
    });
  }

  /* ── Search ── */
  async function doSearch(query) {
    const resultsEl = $('kbSearchResults');
    if (!resultsEl) return;

    if (!query.trim()) {
      resultsEl.innerHTML = '<div style="text-align:center;padding:var(--sp-8);color:var(--muted);font-size:13px;">Type to search your knowledge base</div>';
      return;
    }

    resultsEl.innerHTML = '<div style="text-align:center;padding:var(--sp-8);color:var(--muted);font-size:13px;">Searching...</div>';

    try {
      const res = await fetch('/api/settings/memory-search/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, top_k: 20 }),
      });

      if (!res.ok) {
        // Fallback to client-side filter
        renderClientSearch(query);
        return;
      }

      const data = await res.json();
      const results = data.results || [];

      if (results.length === 0) {
        resultsEl.innerHTML = '<div style="text-align:center;padding:var(--sp-8);color:var(--muted);font-size:13px;">No matches found</div>';
        return;
      }

      resultsEl.innerHTML = '';
      results.forEach(r => {
        const score = r.score || 0;
        const scoreClass = score >= 0.7 ? 'high' : score >= 0.4 ? 'mid' : 'low';
        const el = document.createElement('div');
        el.className = 'kb-search-result';
        el.innerHTML = `
          <div class="kb-search-score ${scoreClass}">${score.toFixed(2)}</div>
          <div class="kb-search-result-body">
            <div class="kb-search-result-key">${escHtml(r.key || '')}</div>
            <div class="kb-search-result-snippet">${escHtml((r.value || '').slice(0, 200))}</div>
            <div class="kb-search-result-meta">
              <span>${r.category || 'unknown'}</span>
              <span>·</span>
              <span>${timeAgo(r.updated_at || r.created_at)}</span>
            </div>
          </div>
        `;
        el.addEventListener('click', () => {
          showDetailPanel({
            key: r.key,
            value: r.value,
            category: r.category || 'semantic',
            created_at: r.created_at,
            updated_at: r.updated_at,
          });
        });
        resultsEl.appendChild(el);
      });
    } catch {
      renderClientSearch(query);
    }
  }

  function renderClientSearch(query) {
    const resultsEl = $('kbSearchResults');
    const q = query.toLowerCase();
    const filtered = getAllEntries().filter(e =>
      (e.key || '').toLowerCase().includes(q) ||
      (e.value || '').toLowerCase().includes(q)
    );

    if (filtered.length === 0) {
      resultsEl.innerHTML = '<div style="text-align:center;padding:var(--sp-8);color:var(--muted);font-size:13px;">No matches found</div>';
      return;
    }

    resultsEl.innerHTML = '';
    filtered.forEach(e => {
      const el = document.createElement('div');
      el.className = 'kb-search-result';
      el.innerHTML = `
        <div class="kb-search-score mid">—</div>
        <div class="kb-search-result-body">
          <div class="kb-search-result-key">${escHtml(e.key)}</div>
          <div class="kb-search-result-snippet">${escHtml((e.value || '').slice(0, 200))}</div>
          <div class="kb-search-result-meta">
            <span>${e.category}</span>
            <span>·</span>
            <span>${timeAgo(e.updated_at || e.created_at)}</span>
          </div>
        </div>
      `;
      el.addEventListener('click', () => showDetailPanel(e));
      resultsEl.appendChild(el);
    });
  }

  /* ── Detail panel ── */
  function showDetailPanel(entry, editMode = false) {
    const panel = $('kbDetailPanel');
    if (!panel) return;

    $('kbDetailTitle').textContent = entry.key || '(untitled)';
    const badge = $('kbDetailBadge');
    badge.textContent = entry.category || 'semantic';
    badge.className = `kb-detail-badge ${entry.category || 'semantic'}`;
    $('kbDetailTime').textContent = timeAgo(entry.updated_at || entry.created_at);
    $('kbDetailValue').textContent = entry.value || '';

    // Show procedural-specific fields
    const procFields = $('kbDetailProcFields');
    if (procFields) {
      if (entry.category === 'procedural' && (entry.trigger || (entry.keywords && entry.keywords.length))) {
        let html = '';
        if (entry.trigger) html += `<div class="kb-proc-field"><span class="kb-proc-label">Trigger:</span> ${escHtml(entry.trigger)}</div>`;
        if (entry.keywords && entry.keywords.length) html += `<div class="kb-proc-field"><span class="kb-proc-label">Keywords:</span> ${entry.keywords.map(k => `<span class="kb-proc-kw">${escHtml(k)}</span>`).join(' ')}</div>`;
        procFields.innerHTML = html;
        procFields.style.display = '';
      } else {
        procFields.style.display = 'none';
      }
    }

    // Connected memories (find entries sharing keywords)
    const chips = $('kbDetailChips');
    chips.innerHTML = '';
    const myKw = extractKeywords(`${entry.key} ${entry.value}`);
    const all = getAllEntries();
    const connected = all.filter(e => e.key !== entry.key).filter(e => {
      const otherKw = extractKeywords(`${e.key} ${e.value}`);
      return myKw.some(k => otherKw.includes(k));
    }).slice(0, 10);

    if (connected.length === 0) {
      $('kbDetailConnections').style.display = 'none';
    } else {
      $('kbDetailConnections').style.display = '';
      connected.forEach(c => {
        const chip = document.createElement('span');
        chip.className = 'kb-detail-chip';
        chip.textContent = c.key;
        chip.addEventListener('click', () => {
          showDetailPanel(c);
          // If in graph mode, pan to that node
          if (currentMode === 'graph' && cy) {
            const node = cy.nodes().find(n => n.data('fullKey') === c.key);
            if (node) {
              cy.animate({ center: { eles: node }, duration: 400 });
              selectNode(node);
            }
          }
        });
        chips.appendChild(chip);
      });
    }

    // Wire edit/delete
    $('kbDetailEdit').onclick = () => showToast('Edit mode coming soon');
    $('kbDetailDelete').onclick = () => deleteEntry(entry);

    panel.classList.add('open');
  }

  function closeDetailPanel() {
    const panel = $('kbDetailPanel');
    if (panel) panel.classList.remove('open');
  }

  /* ── CRUD ── */
  async function deleteEntry(entry) {
    const cat = entry.category;
    const key = entry.key;

    try {
      const url = cat === 'protected'
        ? '/api/settings/memory/protected'
        : '/api/settings/memory';
      const res = await fetch(url, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category: cat, key }),
      });

      if (res.ok) {
        showToast(`Deleted "${key}"`, () => {
          // Undo: re-add (simplified)
          showToast('Undo not yet implemented');
        });
        await loadEntries();
      } else {
        showToast('Failed to delete');
      }
    } catch {
      showToast('Error deleting entry');
    }
  }

  async function addEntry(key, value, category) {
    if (!key.trim() || !value.trim()) return;

    try {
      if (category === 'protected') {
        // Fetch existing, append, save
        const res = await fetch('/api/settings/memory/protected');
        const data = res.ok ? await res.json() : { protected: [] };
        const list = data.protected || [];
        list.push({ key: key.trim(), value: value.trim() });
        await fetch('/api/settings/memory/protected', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ protected: list }),
        });
      } else {
        const res = await fetch('/api/settings/memory');
        const data = res.ok ? await res.json() : { memory: {} };
        const mem = data.memory || {};
        if (!mem[category]) mem[category] = [];
        mem[category].push({ key: key.trim(), value: value.trim() });
        await fetch('/api/settings/memory', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ memory: mem }),
        });
      }

      // Reset form
      $('kbAddKey').value = '';
      $('kbAddValue').value = '';
      $('kbAddForm').classList.remove('open');

      await loadEntries();
      showToast(`Added "${key}"`);
    } catch (e) {
      console.error('KB: Add failed:', e);
      showToast('Failed to add entry');
    }
  }

  /* ── Toast ── */
  function showToast(msg, undoFn) {
    const toast = $('kbToast');
    const msgEl = $('kbToastMsg');
    const undoBtn = $('kbToastUndo');
    if (!toast || !msgEl) return;

    msgEl.textContent = msg;
    undoBtn.style.display = undoFn ? '' : 'none';
    undoBtn.onclick = undoFn || (() => {});

    toast.classList.add('visible');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toast.classList.remove('visible');
    }, 5000);
  }

  /* ── Cytoscape loader ── */
  function loadCytoscape() {
    return new Promise((resolve, reject) => {
      if (typeof cytoscape !== 'undefined') { resolve(); return; }
      // Should already be loaded via <script> tag in index.html
      reject(new Error('Cytoscape not available — check /static/vendor/cytoscape.min.js'));
    });
  }

  /* ── Open / Close ── */
  function openKnowledge() {
    if (isOpen) return;
    isOpen = true;

    // Host sidebar content in left sidebar
    const sidebarEl = $('kbSidebarContent');
    if (sidebarEl && window.sidebarHost?.host) {
      sidebarEl.classList.remove('hidden');
      window.sidebarHost.host('kbSidebarContent');
    }

    // Hide other main-area views
    const chatEl = $('chat');
    const inputEl = $('inputArea');
    if (chatEl) chatEl.classList.add('hidden');
    if (inputEl) inputEl.classList.add('hidden');
    ['searchView', 'researchView', 'imageView', 'promptgenView', 'dashboardView', 'calendarView'].forEach(id => {
      const el = $(id);
      if (el) { el.classList.add('hidden'); el.style.display = ''; }
    });

    document.body.classList.add('knowledge-open');
    const view = $('knowledgeView');
    if (view) view.classList.remove('hidden');

    loadEntries();

    // Graph needs a visible container to measure — defer render
    // Use triple rAF to ensure sidebar hosting + CSS transitions are complete
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          renderCurrentMode();
        });
      });
    });
  }

  function closeKnowledge() {
    if (!isOpen) return;
    isOpen = false;

    // Only unhost if we're actually the hosted panel
    if (window.sidebarHost?.getCurrent?.() === 'kbSidebarContent') {
      window.sidebarHost.unhost();
    }
    const sidebarEl = $('kbSidebarContent');
    if (sidebarEl) sidebarEl.classList.add('hidden');

    document.body.classList.remove('knowledge-open');
    const view = $('knowledgeView');
    if (view) view.classList.add('hidden');
    // Restore chat
    const chatEl = $('chat');
    const inputEl = $('inputArea');
    if (chatEl) chatEl.classList.remove('hidden');
    if (inputEl) inputEl.classList.remove('hidden');

    closeDetailPanel();
    if (cy) { cy.destroy(); cy = null; }
  }

  /* ── Init ── */
  function init() {
    // Mode tabs
    document.querySelectorAll('.kb-mode-tab').forEach(tab => {
      tab.addEventListener('click', () => setMode(tab.dataset.kbMode));
    });

    // Category filters
    document.querySelectorAll('.kb-filter-item').forEach(item => {
      item.addEventListener('click', () => {
        const cat = item.dataset.kbCat;
        item.classList.toggle('checked');
        if (activeCategories.has(cat)) activeCategories.delete(cat);
        else activeCategories.add(cat);
        renderCurrentMode();
      });
    });

    // Node filter input
    const nodeFilter = $('kbNodeFilter');
    if (nodeFilter) {
      nodeFilter.addEventListener('input', () => {
        nodeFilterText = nodeFilter.value;
        renderCurrentMode();
      });
    }

    // Search input with debounce
    const searchInput = $('kbSearchInput');
    if (searchInput) {
      searchInput.addEventListener('input', () => {
        if (searchDebounce) clearTimeout(searchDebounce);
        searchDebounce = setTimeout(() => doSearch(searchInput.value), 300);
      });
    }

    // ⌘K shortcut
    window.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        if (isOpen) {
          e.preventDefault();
          setMode('search');
          setTimeout(() => searchInput?.focus(), 100);
        }
      }
      if (e.key === 'Escape' && isOpen) {
        closeDetailPanel();
      }
    });

    // Zoom controls
    $('kbZoomIn')?.addEventListener('click', () => {
      if (!cy) return;
      cy.zoom(cy.zoom() * 1.3);
      cy.center();
    });
    $('kbZoomOut')?.addEventListener('click', () => {
      if (!cy) return;
      cy.zoom(cy.zoom() / 1.3);
      cy.center();
    });
    $('kbZoomFit')?.addEventListener('click', () => {
      if (!cy) return;
      cy.fit(undefined, 40);
    });

    // Detail panel close
    $('kbDetailClose')?.addEventListener('click', closeDetailPanel);

    // FAB
    $('kbFab')?.addEventListener('click', () => {
      const form = $('kbAddForm');
      if (form) {
        form.classList.toggle('open');
        if (form.classList.contains('open')) $('kbAddKey')?.focus();
      }
    });

    // Add form
    $('kbAddCancel')?.addEventListener('click', () => {
      $('kbAddForm')?.classList.remove('open');
    });
    $('kbAddSave')?.addEventListener('click', () => {
      const key = $('kbAddKey')?.value || '';
      const value = $('kbAddValue')?.value || '';
      const category = $('kbAddCategory')?.value || 'semantic';
      addEntry(key, value, category);
    });

    // Empty state add button
    $('kbEmptyAddBtn')?.addEventListener('click', () => {
      $('kbAddForm')?.classList.add('open');
      $('kbAddKey')?.focus();
    });

    // Reindex button
    $('kbReindexBtn')?.addEventListener('click', async () => {
      const btn = $('kbReindexBtn');
      btn?.classList.add('spinning');
      try {
        await fetch('/api/settings/memory-search/refresh-cache', { method: 'POST' });
        showToast('Embeddings refreshed');
        await loadEntries();
      } catch {
        showToast('Re-index failed');
      } finally {
        btn?.classList.remove('spinning');
      }
    });

    // Listen for rail-switch to toggle knowledge base
    window.addEventListener('rail-switch', (e) => {
      const target = e.detail?.target;
      if (target === 'knowledge') {
        if (isOpen) closeKnowledge();
        else openKnowledge();
      } else if (isOpen) {
        // Close KB when switching to another panel
        closeKnowledge();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
