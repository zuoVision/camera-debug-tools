const $ = selector => document.querySelector(selector);

function downloadText(name, content, type = 'application/json') {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([content], {type}));
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

export function createDiagnostics({api, escapeHtml, notify}) {
  const TIMELINE_LIMIT = 100;
  let jobs = [];

  function renderJobs() {
    const filter = $('#jobStatusFilter').value;
    const visible = jobs.filter(job => {
      if (!filter) return true;
      if (filter === 'active') return ['queued', 'running', 'stopping'].includes(job.status);
      if (filter === 'failed') return ['failed', 'timed_out'].includes(job.status);
      return job.status === filter;
    });
    $('#jobHistorySummary').textContent = `显示 ${visible.length} / ${jobs.length} 条任务`;
    $('#jobHistory').innerHTML = visible.map(job => `<div class="edge-item"><span>${escapeHtml(job.name)}<small>${new Date(job.createdAt).toLocaleString()} · ${escapeHtml(job.kind)} · ${job.durationMs} ms · exit=${job.exitCode ?? '--'}</small></span><b class="job-status ${escapeHtml(job.status)}">${escapeHtml(job.status)}</b></div>`).join('') || '<span class="no-params">当前筛选条件下没有任务</span>';
  }

  function unwrapSession(payload) {
    return payload?.session ?? payload?.diagnosticSession ?? payload ?? null;
  }

  function formatTime(value) {
    if (!value) return '--';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function renderSession(payload) {
    const session = unwrapSession(payload);
    const exists = Boolean(session && (session.id || session.name || session.status || session.startedAt));
    const status = exists ? String(session.status || (session.endedAt ? 'ended' : 'running')).toLowerCase() : 'idle';
    const running = ['active', 'running', 'recording'].includes(status);
    const statusLabels = {active: '记录中', running: '记录中', recording: '记录中', ended: '已结束', completed: '已结束', idle: '未开始'};
    $('#diagnosticSessionStatus').className = `session-status ${escapeHtml(status)}`;
    $('#diagnosticSessionStatus').textContent = statusLabels[status] || status;
    $('#diagnosticSessionName').disabled = running;
    $('#startDiagnosticSession').disabled = running;
    $('#endDiagnosticSession').disabled = !running;
    $('#clearDiagnosticSession').disabled = !exists || running;
    $('#exportSessionJson').disabled = !exists;
    $('#exportSessionMarkdown').disabled = !exists;
    if (!exists) {
      $('#diagnosticSessionInfo').innerHTML = '<dt>状态</dt><dd>当前没有诊断会话</dd>';
      $('#diagnosticSessionTimeline').innerHTML = '<span class="no-params">暂无事件</span>';
      return;
    }
    if (!running && session.name) $('#diagnosticSessionName').value = session.name;
    const events = Array.isArray(session.timeline) ? session.timeline : (Array.isArray(session.events) ? session.events : []);
    $('#diagnosticSessionInfo').innerHTML = `<dt>名称</dt><dd>${escapeHtml(session.name || '未命名会话')}</dd><dt>开始时间</dt><dd>${escapeHtml(formatTime(session.startedAt || session.createdAt))}</dd><dt>结束时间</dt><dd>${escapeHtml(formatTime(session.endedAt))}</dd><dt>事件数量</dt><dd>${events.length}</dd>`;
    $('#diagnosticSessionTimeline').innerHTML = events.slice(-TIMELINE_LIMIT).reverse().map(event => {
      const kind = event.type || event.kind || event.category || 'event';
      const message = event.message || event.summary || event.name || kind;
      const detail = event.details == null ? '' : (typeof event.details === 'string' ? event.details : JSON.stringify(event.details));
      return `<div class="session-event"><time>${escapeHtml(formatTime(event.time || event.timestamp || event.createdAt))}</time><span><b>${escapeHtml(kind)}</b>${escapeHtml(message)}${detail ? `<small>${escapeHtml(detail)}</small>` : ''}</span></div>`;
    }).join('') || '<span class="no-params">暂无事件</span>';
  }

  async function loadSession() {
    try {
      renderSession(await api('/api/diagnostic-session'));
    } catch (error) {
      if (error.code === 'not_found') renderSession(null);
      else throw error;
    }
  }

  async function load() {
    try {
      const [diagnostics, history] = await Promise.all([api('/api/diagnostics'), api('/api/jobs'), loadSession()]);
      $('#diagnosticInfo').innerHTML = `<dt>版本</dt><dd>${escapeHtml(diagnostics.version)}</dd><dt>运行时间</dt><dd>${Math.floor(diagnostics.uptimeMs / 1000)} 秒</dd><dt>平台</dt><dd>${escapeHtml(diagnostics.profile)}</dd><dt>Transport</dt><dd>${escapeHtml(diagnostics.transport)}</dd><dt>监控状态</dt><dd>${diagnostics.monitorPaused ? '已暂停' : '运行中'} · ${diagnostics.runningMetrics} 个采集中</dd><dt>活动任务</dt><dd>${diagnostics.activeJobs}</dd><dt>终端会话</dt><dd>${diagnostics.terminalSessions}</dd>`;
      $('#recentErrors').innerHTML = (diagnostics.recentErrors || []).slice().reverse().map(error => `<div class="edge-item"><span>${new Date(error.time).toLocaleString()} · ${escapeHtml(error.area)}<small>${escapeHtml(error.code)} · ${escapeHtml(error.message)}</small></span></div>`).join('') || '<span class="no-params">暂无错误</span>';
      jobs = history.jobs || [];
      renderJobs();
    } catch (error) {
      notify(error.message);
    }
  }

  async function sessionAction(path, body, successMessage) {
    try {
      const result = await api(path, {method: 'POST', body: JSON.stringify(body || {})});
      renderSession(result);
      notify(successMessage);
    } catch (error) {
      notify(error.message);
    }
  }

  async function exportSession(format) {
    try {
      const report = await api(`/api/diagnostic-session/report?format=${encodeURIComponent(format)}`);
      const content = typeof report === 'string' ? report : report.content;
      if (typeof content !== 'string') throw Error('服务未返回可导出的报告内容');
      downloadText(`camera-debug-session.${format === 'markdown' ? 'md' : 'json'}`, content, format === 'markdown' ? 'text/markdown' : 'application/json');
    } catch (error) {
      notify('导出失败：' + error.message);
    }
  }

  async function exportReport(format) {
    try {
      const report = await api(`/api/diagnostics/report?format=${format}`);
      downloadText(`camera-debug-report.${format === 'markdown' ? 'md' : 'json'}`, report.content, format === 'markdown' ? 'text/markdown' : 'application/json');
    } catch (error) {
      notify(error.message);
    }
  }

  $('#refreshJobs').onclick = load;
  $('#jobStatusFilter').onchange = renderJobs;
  $('#clearJobs').onclick = async () => {
    if (!confirm('确定清空所有已结束任务吗？运行中的任务不会受影响。')) return;
    try {
      const result = await api('/api/jobs/clear', {method: 'POST', body: '{}'});
      await load();
      notify(`已清空 ${result.cleared ?? 0} 条任务记录`);
    } catch (error) {
      notify('清空失败：' + error.message);
    }
  };
  $('#copyDiagnostics').onclick = async () => {
    try {
      const report = await api('/api/diagnostics/report?format=markdown');
      await navigator.clipboard.writeText(report.content);
      notify('诊断摘要已复制');
    } catch (error) {
      notify('复制失败：' + error.message);
    }
  };
  $('#exportJson').onclick = () => exportReport('json');
  $('#exportMarkdown').onclick = () => exportReport('markdown');
  $('#startDiagnosticSession').onclick = () => {
    const name = $('#diagnosticSessionName').value.trim();
    if (!name) {
      notify('请输入诊断会话名称');
      $('#diagnosticSessionName').focus();
      return;
    }
    sessionAction('/api/diagnostic-session/start', {name}, '诊断会话已开始');
  };
  $('#endDiagnosticSession').onclick = () => sessionAction('/api/diagnostic-session/end', {}, '诊断会话已结束');
  $('#clearDiagnosticSession').onclick = () => {
    if (!confirm('确定清除当前诊断会话及其时间线吗？')) return;
    sessionAction('/api/diagnostic-session/clear', {}, '诊断会话已清除');
  };
  $('#exportSessionJson').onclick = () => exportSession('json');
  $('#exportSessionMarkdown').onclick = () => exportSession('markdown');

  return {load};
}
