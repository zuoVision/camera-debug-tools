const $ = selector => document.querySelector(selector);

export function createRegisterDecoder({api, escapeHtml}) {
  let loaded = false;

  function bitFieldName(bitIndex, fields) {
    const field = fields.find(item => {
      const range = String(item.bits).split(':').map(Number);
      const msb = range[0], lsb = range.length > 1 ? range[1] : range[0];
      return bitIndex >= lsb && bitIndex <= msb;
    });
    return field?.id || 'Reserved';
  }

  function bits(binary, fields) {
    return [...binary].map((bit, index) => {
      const bitIndex = binary.length-index-1, name = bitFieldName(bitIndex, fields);
      return `<span><small>${bitIndex}</small><em title="${escapeHtml(name)}">${escapeHtml(name)}</em><b class="${bit==='1'?'on':''}">${bit}</b></span>`;
    }).join('');
  }

  function render(result) {
    $('#registerResult').innerHTML = `<div class="register-result-head"><div><code>${escapeHtml(result.register.address)}</code><h2>${escapeHtml(result.register.name)}</h2><p>${escapeHtml(result.register.description)}</p></div><strong class="register-overall ${escapeHtml(result.status.level)}">${escapeHtml(result.status.summary)}</strong></div><div class="register-raw"><div><strong>${escapeHtml(result.value.hex)}</strong><div>十进制 ${result.value.decimal}</div></div><div class="register-bits">${bits(result.value.binary, result.fields)}</div></div><div class="register-field-list">${result.fields.map(field=>`<div class="register-field-row"><b>${escapeHtml(field.id)} <code>[${escapeHtml(field.bits)}]</code></b><code>${field.value}</code><span>${escapeHtml(field.meaning)}</span><strong class="state-${escapeHtml(field.status)}">${{normal:'正常',warning:'关注',error:'异常',unknown:'未知'}[field.status]||field.status}</strong></div>`).join('')}</div>`;
    $('#registerResult').classList.remove('hidden');
  }

  async function decode(event) {
    event.preventDefault();
    $('#registerQueryError').classList.add('hidden');
    try {
      render(await api('/api/registers/decode', {method:'POST', body:JSON.stringify({device:$('#registerDevice').value, register:$('#registerAddressInput').value.trim(), value:$('#registerValueInput').value.trim()})}));
    } catch (error) {
      $('#registerResult').classList.add('hidden');
      $('#registerQueryError').textContent = error.message;
      $('#registerQueryError').classList.remove('hidden');
    }
  }

  async function load() {
    if (loaded) return;
    const result = await api('/api/registers/devices');
    $('#registerDevice').innerHTML = result.devices.map(device=>`<option value="${escapeHtml(device.model)}">${escapeHtml(device.model)}（${device.registerCount} 个寄存器索引）</option>`).join('');
    loaded = true;
  }

  $('#registerQuery').onsubmit = decode;
  return {load};
}
