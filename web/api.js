const $ = selector => document.querySelector(selector);

const queryToken = new URLSearchParams(location.search).get('token') || '';
let accessToken = queryToken || localStorage.getItem('cameraDebugAccessToken') || '';

if (queryToken) {
  localStorage.setItem('cameraDebugAccessToken', queryToken);
  const cleanUrl = new URL(location.href);
  cleanUrl.searchParams.delete('token');
  history.replaceState(null, '', cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
}

export function getAccessToken() {
  return accessToken;
}

export function showAccessToken(message = '') {
  const dialog = $('#accessTokenDialog');
  dialog.classList.remove('hidden');
  $('#accessTokenError').textContent = message;
  $('#accessTokenInput').value = '';
  setTimeout(() => $('#accessTokenInput').focus(), 0);
}

export function hideAccessToken() {
  $('#accessTokenDialog').classList.add('hidden');
  $('#accessTokenError').textContent = '';
}

export async function api(path, options = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...(accessToken ? {'X-Access-Token': accessToken} : {}),
    ...(options.headers || {}),
  };
  let response;
  try {
    response = await fetch(path, {...options, headers});
  } catch (cause) {
    const error = Error('无法连接 Camera Debug 服务');
    error.code = 'network_error';
    error.cause = cause;
    throw error;
  }
  const type = response.headers.get('content-type') || '';
  const text = await response.text();
  let data = {};
  if (text) {
    if (type.includes('application/json')) {
      try {
        data = JSON.parse(text);
      } catch {
        data = {error: {code: 'invalid_response', message: '服务返回了无效 JSON'}};
      }
    } else {
      data = {error: {code: 'http_error', message: text.slice(0, 300) || `HTTP ${response.status}`}};
    }
  }
  if (!response.ok) {
    const payload = data.error;
    const error = Error(typeof payload === 'object' ? payload.message : payload || `HTTP ${response.status}`);
    error.code = typeof payload === 'object' ? payload.code : undefined;
    error.details = typeof payload === 'object' ? payload.details : undefined;
    if (response.status === 401 || error.code === 'unauthorized') showAccessToken('令牌无效，请重新输入');
    throw error;
  }
  return data;
}

export function initAuth({onAuthenticated, notify}) {
  $('#accessTokenForm').onsubmit = async event => {
    event.preventDefault();
    accessToken = $('#accessTokenInput').value.trim();
    if (!accessToken) return;
    localStorage.setItem('cameraDebugAccessToken', accessToken);
    try {
      await onAuthenticated();
      notify('访问令牌已保存');
    } catch (error) {
      if (error.code !== 'unauthorized') $('#accessTokenError').textContent = error.message;
    }
  };
  $('#forgetAccessToken').onclick = () => {
    accessToken = '';
    localStorage.removeItem('cameraDebugAccessToken');
    showAccessToken('已清除保存的令牌');
  };
}
