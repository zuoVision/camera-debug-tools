const clone = value => JSON.parse(JSON.stringify(value));

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
  }
  return value;
}

function equal(a, b) {
  return JSON.stringify(stable(a)) === JSON.stringify(stable(b));
}

function collectChanges(before, after, path = '', changes = [], limit = 80) {
  if (changes.length >= limit || equal(before, after)) return changes;
  const beforeObject = before && typeof before === 'object';
  const afterObject = after && typeof after === 'object';
  if (!beforeObject || !afterObject || Array.isArray(before) !== Array.isArray(after)) {
    changes.push({path: path || '/', kind: before === undefined ? '新增' : after === undefined ? '删除' : '修改'});
    return changes;
  }
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  for (const key of keys) {
    const child = `${path}/${key}`;
    if (!(key in before)) changes.push({path: child, kind: '新增'});
    else if (!(key in after)) changes.push({path: child, kind: '删除'});
    else collectChanges(before[key], after[key], child, changes, limit);
    if (changes.length >= limit) break;
  }
  return changes;
}

function normalizeErrors(error) {
  const details = error?.details;
  const values = Array.isArray(details) ? details : Array.isArray(details?.errors) ? details.errors : details ? [details] : [];
  return values.map(item => typeof item === 'string'
    ? {path: '', message: item}
    : {path: item.path || item.field || '', message: item.message || item.error || error.message});
}

export function createConfigEditor({api, toast, escapeHtml}) {
  const textarea = document.querySelector('#configPreview');
  const saveButton = document.querySelector('#saveConfig');
  let original = {};
  let parseError = '';

  document.body.insertAdjacentHTML('beforeend', `
    <div id="configChangeDialog" class="config-dialog hidden" role="dialog" aria-modal="true" aria-labelledby="configChangeTitle">
      <div class="config-dialog-card"><h2 id="configChangeTitle">保存配置修改</h2><p id="configChangeSummary"></p>
      <div id="configChangeList" class="config-change-list"></div><div class="config-dialog-actions">
      <button id="cancelConfigSave" class="secondary">继续编辑</button><button id="confirmConfigSave">确认保存</button></div></div>
    </div>`);
  const status = document.createElement('div');
  status.id = 'configEditStatus';
  status.className = 'config-edit-status';
  textarea.before(status);
  const errors = document.createElement('div');
  errors.id = 'configFieldErrors';
  errors.className = 'config-field-errors hidden';
  textarea.after(errors);

  function parse() {
    try { parseError = ''; return JSON.parse(textarea.value); }
    catch (error) { parseError = error.message; return null; }
  }

  function isDirty() {
    const current = parse();
    return current === null ? textarea.value.trim() !== JSON.stringify(original, null, 2).trim() : !equal(original, current);
  }

  function refresh() {
    const dirty = isDirty();
    status.className = `config-edit-status ${dirty ? 'dirty' : ''} ${parseError ? 'invalid' : ''}`;
    status.textContent = parseError ? `JSON 格式错误：${parseError}` : dirty ? '● 有未保存修改' : '✓ 已与磁盘配置同步';
    saveButton.disabled = !dirty || Boolean(parseError);
    return dirty;
  }

  function setOriginal(value) {
    original = clone(value);
    textarea.value = JSON.stringify(value, null, 2);
    clearErrors();
    refresh();
  }

  function restore() {
    textarea.value = JSON.stringify(original, null, 2);
    clearErrors();
    refresh();
    toast('已恢复到本次加载时的配置');
  }

  function confirmDiscard(action = '离开当前页面') {
    return !isDirty() || window.confirm(`当前配置有未保存修改，${action}将丢失这些内容。确定继续吗？`);
  }

  function locatePath(path) {
    const key = String(path).split(/[/.\[\]]/).filter(Boolean).pop()?.replace(/~1/g, '/').replace(/~0/g, '~');
    if (!key) return;
    const pattern = new RegExp(`"${key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"\\s*:`);
    const match = pattern.exec(textarea.value);
    if (match) {
      textarea.focus();
      textarea.setSelectionRange(match.index, match.index + match[0].length);
      const line = textarea.value.slice(0, match.index).split('\n').length;
      textarea.scrollTop = Math.max(0, (line - 5) * 19);
    }
  }

  function clearErrors() {
    errors.classList.add('hidden');
    errors.innerHTML = '';
  }

  function showErrors(error) {
    const items = normalizeErrors(error);
    if (!items.length) return;
    errors.classList.remove('hidden');
    errors.innerHTML = `<strong>请修正以下字段</strong>${items.map((item, index) =>
      `<button type="button" data-error-index="${index}"><code>${escapeHtml(item.path || '配置')}</code><span>${escapeHtml(item.message)}</span></button>`).join('')}`;
    errors.querySelectorAll('button').forEach(button => button.onclick = () => locatePath(items[Number(button.dataset.errorIndex)].path));
  }

  async function persist(edited) {
    try {
      clearErrors();
      await api('/api/config/save', {method: 'POST', body: JSON.stringify({config: edited})});
      return true;
    } catch (error) {
      showErrors(error);
      toast('保存失败：' + error.message);
      return false;
    }
  }

  async function previewSave(onSaved) {
    const edited = parse();
    refresh();
    if (!edited) { toast('JSON 格式错误：' + parseError); return; }
    try {
      clearErrors();
      await api('/api/config/validate', {method: 'POST', body: JSON.stringify({config: edited})});
    } catch (error) {
      showErrors(error);
      toast('配置校验失败：' + error.message);
      return;
    }
    const changes = collectChanges(original, edited);
    if (!changes.length) { toast('配置没有变化'); return; }
    document.querySelector('#configChangeSummary').textContent = `共检测到 ${changes.length}${changes.length >= 80 ? '+' : ''} 处字段变化。保存后配置将立即生效。`;
    document.querySelector('#configChangeList').innerHTML = changes.map(change =>
      `<div><span class="change-kind ${change.kind}">${change.kind}</span><code>${escapeHtml(change.path)}</code></div>`).join('');
    const dialog = document.querySelector('#configChangeDialog');
    dialog.classList.remove('hidden');
    document.querySelector('#cancelConfigSave').onclick = () => dialog.classList.add('hidden');
    document.querySelector('#confirmConfigSave').onclick = async () => {
      const button = document.querySelector('#confirmConfigSave');
      button.disabled = true;
      const saved = await persist(edited);
      button.disabled = false;
      if (!saved) { dialog.classList.add('hidden'); return; }
      dialog.classList.add('hidden');
      await onSaved();
      toast('平台配置已保存并生效');
    };
  }

  textarea.addEventListener('input', refresh);
  window.addEventListener('beforeunload', event => {
    if (!isDirty()) return;
    event.preventDefault();
    event.returnValue = '';
  });

  return {setOriginal, restore, refresh, isDirty, confirmDiscard, previewSave, showErrors};
}
