/* study-cover.js — Cover Page tab.
 *
 * Composes an assignment/lab-report cover from remembered values and renders it
 * to a real A4 PDF via POST /api/study/cover/generate.
 *
 * Linkage rules (mirrored server-side in server/api/routes/study_cover.py):
 *   - course number  <-> course title   (both remembered together)
 *   - course         ->  teacher        (auto-loads the course's last teacher;
 *                                        the user may override, and the override
 *                                        becomes that course's new default)
 *   - university     <-> motto          (remembered as one institution record)
 *   - student / teacher profile / doc kind+number / dates are singletons:
 *     only the last value is kept, because a cover carries exactly one of each.
 *
 * Re-render discipline: typing must NEVER rebuild the form (that would blur the
 * input and lose the caret). Inputs mutate `state` in place, repaint only the
 * preview pane, and schedule a debounced save. Only a tab switch or an explicit
 * reload rebuilds the form.
 *
 * Exposes window.studyCover = { renderTab, renderSidebar, handleClick, reload,
 *                               isLoaded, bind }.
 */
(function () {
  'use strict';

  const DESIGNS = [
    { id: 'classic', label: 'Classic', hint: 'Navy band \u00b7 gold frame' },
    { id: 'elegant', label: 'Elegant', hint: 'Cream paper \u00b7 navy banners' },
  ];

  const FIELDS = [
    'student.name', 'student.roll', 'student.section', 'student.session',
    'student.department',
    'teacher_profile.designation', 'teacher_profile.dept',
    'doc.kind', 'doc.number',
    'dates.given', 'dates.due',
    'university.name', 'university.motto', 'university.department',
    'course.no', 'course.title',
  ];

  let state = null;          // server-shaped composer state
  let loaded = false;
  let loading = false;
  let saving = false;
  let dirty = false;
  let saveTimer = null;
  let status = { kind: '', text: '' };
  let lastFile = null;       // {name, url, file} from the last generate
  let generating = false;

  // ── helpers ──────────────────────────────────────────────────────────────

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function token() {
    try { return localStorage.getItem('sable_token') || ''; } catch { return ''; }
  }

  function toast(msg) {
    if (window.studyMode && window.studyMode.toast) return window.studyMode.toast(msg);
    if (typeof window.showToast === 'function') return window.showToast(msg);
    console.log('[study-cover]', msg);
  }

  async function api(path, opts) {
    const t = token();
    const res = await fetch(path, {
      ...(opts || {}),
      headers: {
        'Content-Type': 'application/json',
        ...(t ? { Authorization: 'Bearer ' + t } : {}),
        ...((opts && opts.headers) || {}),
      },
    });
    if (!res.ok) {
      let detail = res.status + ' ' + res.statusText;
      try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch { /* not json */ }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }

  function blank() {
    return {
      version: 1,
      universities: [],
      university: { name: '', motto: '', department: '' },
      courses: [],
      course: { no: '', title: '' },
      student: { name: '', roll: '', section: '', session: '' },
      teacher_profile: { designation: '', dept: '' },
      doc: { kind: 'Assignment', number: '01', topic: '' },
      dates: { given: '', due: '' },
      design: 'classic',
    };
  }

  function get(path) {
    return path.split('.').reduce((o, k) => (o == null ? '' : o[k]), state) || '';
  }

  function set(path, value) {
    const parts = path.split('.');
    let o = state;
    for (let i = 0; i < parts.length - 1; i += 1) o = o[parts[i]];
    o[parts[parts.length - 1]] = value;
  }

  function norm(s) {
    return String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  }

  /** The teacher remembered for the currently selected course. */
  function currentTeacherName() {
    const no = norm(get('course.no'));
    if (!no) return '';
    const rec = (state.courses || []).find((c) => norm(c.no) === no);
    return (rec && rec.last_teacher) || '';
  }

  function currentCourse() {
    const no = norm(get('course.no'));
    return (state.courses || []).find((c) => norm(c.no) === no) || null;
  }

  // ── data ─────────────────────────────────────────────────────────────────

  async function reload() {
    loading = true;
    try {
      const data = await api('/api/study/cover');
      state = data || blank();
      loaded = true;
      dirty = false;
      status = { kind: '', text: '' };
    } catch (err) {
      state = state || blank();
      loaded = true;
      status = { kind: 'error', text: 'Load failed: ' + err.message };
    } finally {
      loading = false;
    }
    if (window.studyMode && window.studyMode.getTab() === 'cover') {
      window.studyMode.refreshTab();
    }
  }

  /** Push the current state to the server and adopt whatever it returns. */
  async function save() {
    if (saving) return;
    saving = true;
    status = { kind: '', text: 'Saving\u2026' };
    paintStatus();
    try {
      const payload = {
        university: state.university,
        course: state.course,
        teacher: { name: currentTeacherName() },
        student: state.student,
        teacher_profile: state.teacher_profile,
        doc: state.doc,
        dates: state.dates,
        design: state.design,
      };
      const fresh = await api('/api/study/cover', {
        method: 'PUT', body: JSON.stringify(payload),
      });
      // Adopt the server's merged view (grows the rosters) but keep what the
      // user is currently typing — the server never echoes a half-typed field
      // back with different content, so this is safe.
      const keep = state;
      state = fresh || keep;
      refreshDatalists();
      dirty = false;
      status = { kind: 'saved', text: 'Saved' };
    } catch (err) {
      status = { kind: 'error', text: 'Save failed: ' + err.message };
    } finally {
      saving = false;
      paintStatus();
    }
  }

  function scheduleSave() {
    dirty = true;
    status = { kind: '', text: 'Unsaved changes' };
    paintStatus();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 700);
  }

  // ── form ─────────────────────────────────────────────────────────────────

  function datalist(id, values) {
    return '<datalist id="' + id + '">' +
      values.map((v) => '<option value="' + esc(v) + '"></option>').join('') +
      '</datalist>';
  }

  function combo(id, listId, value, placeholder) {
    return '<div class="sc-combo">' +
      '<input class="sc-input" id="' + id + '" list="' + listId + '" ' +
      'value="' + esc(value) + '" placeholder="' + esc(placeholder || '') + '" ' +
      'autocomplete="off" spellcheck="false">' +
      '<button class="sc-combo-btn" type="button" data-sc-pick="' + listId + '" ' +
      'title="Show saved"><i data-lucide="chevron-down" class="icon-lucide"></i></button>' +
      '</div>';
  }

  function field(label, inner, hint) {
    return '<div class="sc-field"><label>' + esc(label) + '</label>' + inner +
      (hint ? '<span class="hint">' + esc(hint) + '</span>' : '') + '</div>';
  }

  /** Design picker — rendered above the form so the choice is unmissable. */
  function renderDesignPicker() {
    const cur = state.design || 'classic';
    return '<div class="sc-card">' +
      '<div class="sc-card-head"><i data-lucide="palette" class="icon-lucide"></i>' +
      '<span class="sc-card-title">Design</span></div>' +
      '<div class="sc-card-body"><div class="sc-designs">' +
        DESIGNS.map((d) =>
          '<button type="button" class="sc-design' + (cur === d.id ? ' active' : '') +
            '" data-sc-design="' + d.id + '">' +
            '<span class="sc-design-name">' + esc(d.label) + '</span>' +
            '<span class="sc-design-hint">' + esc(d.hint) + '</span>' +
          '</button>').join('') +
      '</div></div></div>';
  }

  function renderForm() {
    const uniNames = (state.universities || []).map((u) => u.name);
    const courseNos = (state.courses || []).map((c) => c.no);
    const courseTitles = (state.courses || []).map((c) => c.title).filter(Boolean);
    const rec = currentCourse();
    const teachers = (rec && rec.teachers) || [];

    return '' +
      datalist('scUniList', uniNames) +
      datalist('scCourseList', courseNos) +
      datalist('scCourseTitleList', courseTitles) +
      datalist('scTeacherList', teachers) +

      '<div class="sc-card">' +
        '<div class="sc-card-head"><i data-lucide="building-2" class="icon-lucide"></i>' +
        '<span class="sc-card-title">Institution</span></div>' +
        '<div class="sc-card-body">' +
          field('University name',
            combo('scUniName', 'scUniList', get('university.name'),
              'Rajshahi University of Engineering & Technology')) +
          '<div class="sc-card-body two" style="padding:0;gap:12px">' +
            field('Motto',
              '<input class="sc-input" id="scUniMotto" value="' + esc(get('university.motto')) +
              '" placeholder="Heaven\u2019s Light is Our Guide" autocomplete="off">') +
            field('Department',
              '<input class="sc-input" id="scUniDept" value="' + esc(get('university.department')) +
              '" placeholder="Department of Computer Science & Engineering" autocomplete="off">') +
          '</div>' +
        '</div>' +
      '</div>' +

      '<div class="sc-card">' +
        '<div class="sc-card-head"><i data-lucide="book-open" class="icon-lucide"></i>' +
        '<span class="sc-card-title">Course</span></div>' +
        '<div class="sc-card-body">' +
          '<div class="sc-card-body two" style="padding:0;gap:12px">' +
            field('Course no.',
              combo('scCourseNo', 'scCourseList', get('course.no'), 'CSE - 2103')) +
          field('Course title',
            combo('scCourseTitle', 'scCourseTitleList', get('course.title'),
                'Digital Logic Design')) +
          '</div>' +
          field('Assignment topic',
            '<input class="sc-input" id="scDocTopic" value="' + esc(get('doc.topic')) +
            '" placeholder="Rising Oil Prices and Mobile Carrier Charges" ' +
            'autocomplete="off">',
            'Shown on the Elegant design. Falls back to the course title.') +
          field('Teacher',
            combo('scTeacher', 'scTeacherList', currentTeacherName(), 'Md. Zahirul Islam'),
            'Saved per course. Picking a new one becomes this course\u2019s default.') +
        '</div>' +
      '</div>' +

      '<div class="sc-card">' +
        '<div class="sc-card-head"><i data-lucide="user-round" class="icon-lucide"></i>' +
        '<span class="sc-card-title">Student</span></div>' +
        '<div class="sc-card-body">' +
          field('Full name',
            '<input class="sc-input" id="scStudentName" value="' + esc(get('student.name')) +
            '" placeholder="Md Aminul Islam Sifat" autocomplete="off">') +
          '<div class="sc-card-body three" style="padding:0;gap:12px">' +
            field('Roll', '<input class="sc-input" id="scStudentRoll" value="' +
              esc(get('student.roll')) + '" placeholder="2403123" autocomplete="off">') +
            field('Section', '<input class="sc-input" id="scStudentSection" value="' +
              esc(get('student.section')) + '" placeholder="C" autocomplete="off">') +
            field('Session', '<input class="sc-input" id="scStudentSession" value="' +
              esc(get('student.session')) + '" placeholder="2024-25" autocomplete="off">') +
          '</div>' +
          field('Department', '<input class="sc-input" id="scStudentDept" value="' +
            esc(get('student.department')) +
            '" placeholder="Computer Science & Engineering" autocomplete="off">',
            'Defaults to the institution department when left blank.') +
        '</div>' +
      '</div>' +

      '<div class="sc-card">' +
        '<div class="sc-card-head"><i data-lucide="user-check" class="icon-lucide"></i>' +
        '<span class="sc-card-title">Teacher details</span></div>' +
        '<div class="sc-card-body two">' +
          field('Designation', '<input class="sc-input" id="scTeacherDesig" value="' +
            esc(get('teacher_profile.designation')) +
            '" placeholder="Assistant Professor" autocomplete="off">') +
          field('Department', '<input class="sc-input" id="scTeacherDept" value="' +
            esc(get('teacher_profile.dept')) + '" placeholder="CSE, RUET" autocomplete="off">') +
        '</div>' +
      '</div>' +

      '<div class="sc-card">' +
        '<div class="sc-card-head"><i data-lucide="file-text" class="icon-lucide"></i>' +
        '<span class="sc-card-title">Document & dates</span></div>' +
        '<div class="sc-card-body">' +
          '<div class="sc-card-body two" style="padding:0;gap:12px">' +
            field('Document kind',
              '<select class="sc-select" id="scDocKind">' +
                ['Assignment', 'Lab Report', 'Experiment', 'Project', 'Report']
                  .map((k) => '<option' + (get('doc.kind') === k ? ' selected' : '') + '>' +
                    esc(k) + '</option>').join('') +
              '</select>') +
            field('Number', '<input class="sc-input" id="scDocNumber" value="' +
              esc(get('doc.number')) + '" placeholder="01" autocomplete="off">') +
          '</div>' +
          '<div class="sc-card-body two" style="padding:0;gap:12px">' +
            field('Date of ' + (get('doc.kind') || 'assignment').toLowerCase(),
              '<input class="sc-input" id="scDateGiven" value="' + esc(get('dates.given')) +
              '" placeholder="7 September 2026" autocomplete="off">') +
            field('Date of submission',
              '<input class="sc-input" id="scDateDue" value="' + esc(get('dates.due')) +
              '" placeholder="3 October 2026" autocomplete="off">') +
          '</div>' +
        '</div>' +
      '</div>';
  }

  function renderActions() {
    return '<div class="sc-actions">' +
      '<span class="sc-status" id="scStatus"></span>' +
      '<div class="sc-actions-row">' +
        '<button class="sc-btn ghost" type="button" data-sc-action="reset">' +
          '<i data-lucide="eraser" class="icon-lucide"></i> Clear form</button>' +
        '<button class="sc-btn primary" type="button" data-sc-action="generate"' +
          (generating ? ' disabled' : '') + '>' +
          '<i data-lucide="' + (generating ? 'loader' : 'file-down') + '" class="icon-lucide"></i> ' +
          (generating ? 'Rendering\u2026' : 'Generate PDF') + '</button>' +
      '</div>' +
      '</div>';
  }

  // ── live A4 preview ──────────────────────────────────────────────────────

  function ph(value, fallback) {
    const v = String(value == null ? '' : value).trim();
    return v ? esc(v) : '<span class="ph">' + esc(fallback) + '</span>';
  }

  function previewHtml() {
    if ((state.design || 'classic') === 'elegant') return elegantPreviewHtml();
    return classicPreviewHtml();
  }

  /** Live replica of the elegant (cream / navy banner) template. */
  function elegantPreviewHtml() {
    const kind = get('doc.kind') || 'Assignment';
    const number = get('doc.number');
    const rec = currentCourse();
    const teacher = currentTeacherName() || (rec && rec.teachers[0]) || '';
    const banner = (kind.toUpperCase() +
      ((number && number !== '01') ? '  ' + number : ''));

    return '' +
      '<div class="cvp cvp-el">' +
        '<div class="cvp-el-frame"></div>' +
        '<div class="cvp-el-dia tl"></div><div class="cvp-el-dia tr"></div>' +
        '<div class="cvp-el-dia bl"></div><div class="cvp-el-dia br"></div>' +
        '<div class="cvp-el-body">' +
          '<div class="cvp-el-motto">' +
            ph(get('university.motto') || 'Heaven\u2019s Light is Our Guide') +
          '</div>' +
          '<div class="cvp-el-mastdia"></div>' +
          '<div class="cvp-el-uni">' + ph(get('university.name'), 'University Name') + '</div>' +
          '<div class="cvp-el-dept">' + ph(get('university.department'), 'Department') + '</div>' +
          '<div class="cvp-el-rule"></div>' +
          '<div class="cvp-el-banner">' + esc(banner) + '</div>' +
          '<div class="cvp-el-bannerrule"></div>' +
          '<div class="cvp-el-tlabel">' + esc(kind) + ' Topic</div>' +
          '<div class="cvp-el-topic">' +
            ph(get('doc.topic') || get('course.title'), 'Topic') + '</div>' +
          '<div class="cvp-el-orn"><span class="ln"></span>' +
            '<span class="gem"></span><span class="ln"></span></div>' +
          '<div class="cvp-el-course">' +
            '<div><div class="k">Course No</div><div class="v">' +
              ph(get('course.no'), '\u2014') + '</div></div>' +
            '<div class="vline"></div>' +
            '<div><div class="k">Course Title</div><div class="v">' +
              ph(get('course.title'), '\u2014') + '</div></div>' +
          '</div>' +
          '<div class="cvp-el-people">' +
            '<div><div class="ph2">Submitted By</div>' +
              '<div class="pn">' + ph(get('student.name'), '\u2014') + '</div>' +
              '<div class="pr"><b>Roll:</b> ' + ph(get('student.roll'), '') + '</div>' +
              '<div class="pr"><b>Section:</b> ' + ph(get('student.section'), '') + '</div>' +
              '<div class="pb"><b>Department:</b><br>' +
                ph(get('student.department') || get('university.department'), '\u2014') + '</div>' +
              '<div class="pr" style="margin-top:10px"><b>Session:</b> ' +
                ph(get('student.session'), '') + '</div>' +
            '</div>' +
            '<div class="vline"></div>' +
            '<div><div class="ph2">Submitted To</div>' +
              '<div class="pn">' + ph(teacher, '\u2014') + '</div>' +
              '<div class="prole">' + ph(get('teacher_profile.designation'), '') + '</div>' +
              '<div class="pb">' + ph(get('teacher_profile.dept'), '\u2014') + '</div>' +
              '<div class="pb">' + ph(get('university.name'), '\u2014') + '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="cvp-el-foot">' +
          '<div class="rule"></div>' +
          '<div class="txt">' + ph(get('university.department'), 'Department') + '</div>' +
        '</div>' +
      '</div>';
  }

  /** Live replica of the classic (navy band / gold frame) template. */
  function classicPreviewHtml() {
    const kind = get('doc.kind') || 'Assignment';
    const number = get('doc.number') || '01';
    const rec = currentCourse();
    const teacher = currentTeacherName() || (rec && rec.teachers[0]) || '';

    return '' +
      '<div class="cvp">' +
        '<div class="cvp-frame"></div>' +
        '<div class="cvp-corner tl"></div><div class="cvp-corner tr"></div>' +
        '<div class="cvp-corner bl"></div><div class="cvp-corner br"></div>' +
        '<div class="cvp-header">' +
          '<div class="cvp-motto">' +
            ph(get('university.motto') || 'Heaven\u2019s Light is Our Guide') +
          '</div>' +
          '<div class="cvp-uni">' + ph(get('university.name'), 'University Name') + '</div>' +
          '<div class="cvp-dept">' + ph(get('university.department'), 'Department') + '</div>' +
        '</div>' +
        '<div class="cvp-body">' +
          '<div class="cvp-course">' +
            '<div><div class="k">Course No.</div><div class="v">' +
              ph(get('course.no'), '\u2014') + '</div></div>' +
            '<div><div class="k">Course Title</div><div class="v">' +
              ph(get('course.title'), '\u2014') + '</div></div>' +
          '</div>' +
          '<div class="cvp-titleblock">' +
            '<div class="cvp-num">' + esc(kind) + ' No. ' + esc(number) + '</div>' +
            '<div class="cvp-tsep"></div>' +
            '<div class="cvp-ttl">' + ph(get('course.title'), '\u2014') + '</div>' +
          '</div>' +
          '<div class="cvp-people">' +
            '<div class="cvp-col"><h4>Submitted By</h4>' +
              '<div class="cvp-name">' + ph(get('student.name'), '\u2014') + '</div>' +
              '<div class="cvp-row"><span class="lbl">Roll</span><span class="val">' +
                ph(get('student.roll'), '\u2014') + '</span></div>' +
              '<div class="cvp-row"><span class="lbl">Section</span><span class="val">' +
                ph(get('student.section'), '\u2014') + '</span></div>' +
              '<div class="cvp-row"><span class="lbl">Session</span><span class="val">' +
                ph(get('student.session'), '\u2014') + '</span></div>' +
            '</div>' +
            '<div class="cvp-col"><h4>Submitted To</h4>' +
              '<div class="cvp-name">' + ph(teacher, '\u2014') + '</div>' +
              '<div class="cvp-row"><span class="lbl">Designation</span><span class="val">' +
                ph(get('teacher_profile.designation'), '\u2014') + '</span></div>' +
              '<div class="cvp-row"><span class="lbl">Dept.</span><span class="val">' +
                ph(get('teacher_profile.dept'), '\u2014') + '</span></div>' +
            '</div>' +
          '</div>' +
          '<div class="cvp-orn"><span class="ln"></span><span class="gem"></span>' +
            '<span class="ln"></span></div>' +
        '</div>' +
        '<div class="cvp-footer">' +
          '<div class="cvp-dates">' +
            '<div class="item"><div class="lab">Date of ' + esc(kind) + '</div>' +
              '<div class="val">' + ph(get('dates.given'), '\u2014') + '</div></div>' +
            '<div class="item"><div class="lab">Date of Submission</div>' +
              '<div class="val">' + ph(get('dates.due'), '\u2014') + '</div></div>' +
          '</div>' +
          '<div class="cvp-seal">\u2014 Submitted in partial fulfillment of the ' +
            'requirements of ' + ph(get('course.no'), 'the course') + ' \u2014</div>' +
        '</div>' +
      '</div>';
  }

  function renderPreview() {
    return '<div class="sc-preview-col">' +
      '<div class="sc-preview-label"><i data-lucide="eye" class="icon-lucide"></i> ' +
        'Live preview \u00b7 A4</div>' +
      '<div class="sc-preview-frame"><div class="sc-preview-clip" id="scPreviewClip">' +
        '<div class="sc-preview-scaler" id="scPreviewScaler">' +
          '<div id="scPreviewInner">' + previewHtml() + '</div>' +
        '</div>' +
      '</div></div>' +
      renderActions() +
      '<div id="scLastFile"></div>' +
      '</div>';
  }

  /** Repaint only the preview pane (never the form). */
  function paintPreview() {
    const inner = document.getElementById('scPreviewInner');
    if (inner) inner.innerHTML = previewHtml();
    fitPreview();
  }

  /**
   * Scale the 794x1123 replica to fit its column.
   *
   * Fits BOTH axes: width decides the base scale, but the height budget
   * (viewport minus everything above and below the clip) caps it. Without the
   * height cap a full-width column produces an ~850px-tall preview, which
   * pushes the Generate/Clear buttons below the fold — exactly where they are
   * least useful.
   */
  function fitPreview() {
    const clip = document.getElementById('scPreviewClip');
    const scaler = document.getElementById('scPreviewScaler');
    if (!clip || !scaler) return;

    const w = clip.clientWidth || 320;

    // Everything below the preview (action bar, last-file card) has to stay
    // on screen, so subtract its measured height from the budget.
    let below = 0;
    const frame = clip.parentElement;
    const col = frame ? frame.parentElement : null;
    if (col) {
      const kids = Array.from(col.children);
      const idx = kids.indexOf(frame);
      for (let i = idx + 1; i < kids.length; i += 1) {
        below += kids[i].getBoundingClientRect().height;
      }
    }
    const gapBudget = 24;                 // column gaps + breathing room
    const top = clip.getBoundingClientRect().top;
    const availH = Math.max(
      240,                                // never shrink below legibility
      window.innerHeight - top - below - gapBudget
    );

    const s = Math.min(w / 794, availH / 1123);
    scaler.style.transform = 'scale(' + s + ')';
    clip.style.height = Math.round(1123 * s) + 'px';
  }

  function paintStatus() {
    const el = document.getElementById('scStatus');
    if (!el) return;
    el.className = 'sc-status' + (status.kind ? ' ' + status.kind : '');
    el.textContent = status.text || '';
  }

  /**
   * URL for a generated PDF, carrying the auth token.
   *
   * The file route is a plain GET reached by a normal navigation, which cannot
   * set an Authorization header — so the token rides in the query string,
   * exactly like study thumbnails and the Drive OAuth start URL do.
   */
  function fileUrl(name) {
    const t = token();
    const base = '/api/study/cover/file/' + encodeURIComponent(name);
    return t ? base + '?token=' + encodeURIComponent(t) : base;
  }

  /** Save the generated PDF to the user's Downloads folder. */
  function triggerDownload(name) {
    const a = document.createElement('a');
    a.href = fileUrl(name);
    a.download = name;
    a.rel = 'noopener';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    // Give the navigation a beat to start before tearing the node down.
    setTimeout(() => a.remove(), 1000);
  }

  function paintLastFile() {
    const el = document.getElementById('scLastFile');
    if (!el) return;
    if (!lastFile) { el.innerHTML = ''; fitPreview(); return; }
    el.innerHTML =
      '<div class="sc-card" style="margin-top:2px">' +
        '<div class="sc-card-body" style="display:flex;align-items:center;gap:10px">' +
          '<i data-lucide="check-circle-2" class="icon-lucide" ' +
            'style="width:16px;height:16px;color:#3fb950;flex:0 0 auto"></i>' +
          '<span style="font-size:12px;min-width:0;overflow:hidden;' +
            'text-overflow:ellipsis;white-space:nowrap" title="' +
            esc(lastFile.name) + '">' + esc(lastFile.name) + '</span>' +
          '<button class="sc-btn ghost" type="button" style="margin-left:auto;' +
            'flex:0 0 auto" data-sc-download="' + esc(lastFile.name) + '">' +
            '<i data-lucide="download" class="icon-lucide"></i> Save</button>' +
        '</div>' +
      '</div>';
    if (window.lucide) window.lucide.createIcons();
    // The card just changed the column's height, so the preview's height
    // budget moved with it. Re-fit or the buttons drift off screen.
    fitPreview();
  }

  /** Refresh only the <datalist> option sets, leaving the inputs untouched. */
  function refreshDatalists() {
    const put = (id, values) => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = values.map((v) => '<option value="' + esc(v) + '"></option>').join('');
    };
    put('scUniList', (state.universities || []).map((u) => u.name));
    put('scCourseList', (state.courses || []).map((c) => c.no));
    put('scCourseTitleList', (state.courses || []).map((c) => c.title).filter(Boolean));
    const rec = currentCourse();
    put('scTeacherList', (rec && rec.teachers) || []);
  }

  // ── tab surface ──────────────────────────────────────────────────────────

  function renderTab() {
    if (!state) state = blank();
    return '<div class="sc-wrap">' +
      '<div class="sc-form">' + renderDesignPicker() + renderForm() + '</div>' +
      renderPreview() +
      '</div>';
  }

  function renderSidebar() {
    return '<div class="study-sidebar-section">' +
      '<div class="study-sidebar-label">Cover page</div>' +
      '<button class="study-sidebar-item" data-sc-action="generate">' +
        '<i data-lucide="file-down" class="icon-lucide"></i> Generate PDF</button>' +
      '<button class="study-sidebar-item" data-sc-action="reload">' +
        '<i data-lucide="refresh-cw" class="icon-lucide"></i> Reload saved</button>' +
      '<button class="study-sidebar-item" data-sc-action="reset">' +
        '<i data-lucide="eraser" class="icon-lucide"></i> Clear form</button>' +
      '</div>' +
      '<div class="study-sidebar-section">' +
        '<div class="study-sidebar-label">Remembered</div>' +
        '<div class="study-sidebar-item" style="cursor:default">' +
          '<i data-lucide="building-2" class="icon-lucide"></i> ' +
          ((state && state.universities) ? state.universities.length : 0) + ' institutions</div>' +
        '<div class="study-sidebar-item" style="cursor:default">' +
          '<i data-lucide="book-open" class="icon-lucide"></i> ' +
          ((state && state.courses) ? state.courses.length : 0) + ' courses</div>' +
      '</div>';
  }

  // ── wiring (called after the tab is painted) ─────────────────────────────

  let bound = false;

  function bind() {
    const content = document.getElementById('studyContent');
    if (!content) return;
    // Paint first, fit LAST: fitPreview measures the action bar and any
    // last-file card, so those must already be in the DOM with their real
    // heights before the height budget is computed.
    paintStatus();
    paintLastFile();
    fitPreview();

    // Inputs are inside freshly-built DOM, so bind once per render pass.
    content.querySelectorAll('input[id^="sc"], select[id^="sc"]').forEach((el) => {
      if (el.dataset.scBound) return;
      el.dataset.scBound = '1';
      const evt = el.tagName === 'SELECT' ? 'change' : 'input';
      el.addEventListener(evt, onFieldInput);
      if (el.tagName !== 'SELECT') el.addEventListener('blur', onFieldBlur);
    });
    bound = true;
  }

  const ID_MAP = {
    scUniName: 'university.name',
    scUniMotto: 'university.motto',
    scUniDept: 'university.department',
    scCourseNo: 'course.no',
    scCourseTitle: 'course.title',
    scStudentName: 'student.name',
    scStudentRoll: 'student.roll',
    scStudentSection: 'student.section',
    scStudentSession: 'student.session',
    scTeacherDesig: 'teacher_profile.designation',
    scTeacherDept: 'teacher_profile.dept',
    scDocKind: 'doc.kind',
    scDocNumber: 'doc.number',
    scDocTopic: 'doc.topic',
    scStudentDept: 'student.department',
    scDateGiven: 'dates.given',
    scDateDue: 'dates.due',
  };

  function onFieldInput(e) {
    const el = e.target;
    if (!el || !el.id) return;
    const id = el.id;
    const value = el.value;

    if (id === 'scTeacher') {
      // teacher is derived: it is not a stored field, it is the course's default
      setCourseTeacher(value);
      paintPreview();
      scheduleSave();
      return;
    }

    const path = ID_MAP[id];
    if (!path) return;
    set(path, value);

    if (id === 'scCourseNo') {
      // Course no changed -> adopt that course's title + teacher.
      const rec = (state.courses || []).find((c) => norm(c.no) === norm(value));
      if (rec) {
        if (rec.title) {
          set('course.title', rec.title);
          const t = document.getElementById('scCourseTitle');
          if (t && !t.value) t.value = rec.title;
        }
        const teach = document.getElementById('scTeacher');
        if (teach) teach.value = rec.last_teacher || '';
      }
      refreshDatalists();
    }

    if (id === 'scCourseTitle' && value) {
      // Keep the course record's title in step as the user types it.
      const rec = currentCourse();
      if (rec) rec.title = value;
    }

    if (id === 'scDocKind') {
      // The date label follows the document kind.
      window.studyMode.refreshTab();
      return;
    }

    paintPreview();
    scheduleSave();
  }

  function onFieldBlur(e) {
    const el = e.target;
    if (el && el.id === 'scTeacher') return;
    // A typed value that is not yet a roster entry should be persisted now.
    if (dirty) save();
  }

  /** Override the selected course's remembered teacher (creating it if new). */
  function setCourseTeacher(name) {
    const no = get('course.no');
    if (!no) return;
    let rec = currentCourse();
    if (!rec) {
      rec = { no: no, title: get('course.title'), teachers: [], last_teacher: '',
              last_dates: { given: '', due: '' } };
      state.courses.push(rec);
    }
    const nm = String(name || '').trim();
    rec.last_teacher = nm;
    if (nm && !rec.teachers.some((t) => norm(t) === norm(nm))) rec.teachers.push(nm);
    refreshDatalists();
  }

  // ── actions ──────────────────────────────────────────────────────────────

  function clearForm() {
    const blankState = blank();
    // Keep the rosters (they are the user's history); clear only the selections.
    blankState.universities = (state && state.universities) || [];
    blankState.courses = (state && state.courses) || [];
    state = blankState;
    lastFile = null;
    status = { kind: '', text: '' };
    window.studyMode.refreshTab();
  }

  async function generate() {
    if (generating) return;
    generating = true;
    status = { kind: '', text: 'Rendering PDF\u2026' };
    window.studyMode.refreshTab();
    try {
      const payload = {
        university: state.university,
        course: state.course,
        teacher: { name: currentTeacherName() },
        student: state.student,
        teacher_profile: state.teacher_profile,
        doc: state.doc,
        dates: state.dates,
        design: state.design,
      };
      const res = await api('/api/study/cover/generate', {
        method: 'POST', body: JSON.stringify(payload),
      });
      if (res && res.state) state = res.state;
      lastFile = { name: res.name, url: res.url, file: res.file };
      status = { kind: 'saved', text: 'PDF ready — saved to Downloads' };
      // Hand the file straight to the browser. The route sets
      // Content-Disposition: attachment, so this saves rather than navigates.
      triggerDownload(res.name);
      toast('Cover PDF generated and downloaded');
    } catch (err) {
      status = { kind: 'error', text: 'Generate failed: ' + err.message };
      toast('Generate failed: ' + err.message);
    } finally {
      generating = false;
      window.studyMode.refreshTab();
    }
  }

  function handleClick(e) {
    const act = e.target.closest('[data-sc-action]');
    if (act) {
      const what = act.dataset.scAction;
      if (what === 'generate') { generate(); return true; }
      if (what === 'reset') { clearForm(); return true; }
      if (what === 'reload') { reload(); return true; }
    }
    const dz = e.target.closest('[data-sc-design]');
    if (dz) {
      const next = dz.dataset.scDesign;
      if (DESIGNS.some((d) => d.id === next) && next !== state.design) {
        state.design = next;
        // Repaint the whole tab: the picker's active state AND the preview
        // layout both change, and the form itself is unaffected by design.
        window.studyMode.refreshTab();
        scheduleSave();
      }
      return true;
    }
    const dl = e.target.closest('[data-sc-download]');
    if (dl) {
      triggerDownload(dl.dataset.scDownload);
      return true;
    }
    const pick = e.target.closest('[data-sc-pick]');
    if (pick) {
      // Nudge the browser into showing the datalist for its paired input.
      const listId = pick.dataset.scPick;
      const INPUT_FOR_LIST = {
        scUniList: 'scUniName',
        scCourseList: 'scCourseNo',
        scCourseTitleList: 'scCourseTitle',
        scTeacherList: 'scTeacher',
      };
      const input = document.getElementById(INPUT_FOR_LIST[listId] || '');
      if (input) {
        input.focus();
        try { input.showPicker(); } catch { /* not supported */ }
      }
      return true;
    }
    return false;
  }

  window.addEventListener('resize', () => {
    if (window.studyMode && window.studyMode.getTab() === 'cover') fitPreview();
  });

  window.studyCover = {
    renderTab,
    renderSidebar,
    handleClick,
    bind,
    reload,
    isLoaded: () => loaded,
    isLoading: () => loading,
  };
})();
