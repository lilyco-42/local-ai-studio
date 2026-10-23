'use strict';

const I18N = {
  zh: {
    appName: 'Local AI Studio',
    tagline: '本地跑，不上传',
    bootError: '找不到 Tauri IPC：这个页面只能在外壳窗口里用，按钮已被禁用。',
    tabChat: '对话',
    tabSubtitle: '字幕',
    tabOcr: '文字识别',
    tabImage: '生图',
    idle: '未启动',
    running: '运行中',
    notInstalled: '运行组件未安装',
    notInstalledHint: '引擎负载还没装进数据目录；点“安装运行组件”按清单落盘并逐文件校验。',
    bundledHint: '这个安装包自带运行组件：点“安装运行组件”直接落进数据目录，不联网，逐文件校验。',
    remoteHint: '数据目录里配置了远程源：点“安装运行组件”按清单下载并逐文件校验。',
    payloadUnconfigured: '数据目录里没有 payload-source.json，这个安装包也没带内置运行组件：没有可用的首装来源。',
    installPayload: '安装运行组件',
    installBusy: '安装运行组件… {seen}/{total}',
    unavailable: '本平台暂不提供',
    unavailableHint: '上游没有发布这一平台的引擎；这台机器只能装合同里有的能力。',
    oneShot: '一次性进程',
    startEngine: '启动引擎',
    stopEngine: '停止',
    send: '发送',
    chatPlaceholder: '给本地模型发一句话…',
    subtitlePick: '音频或视频',
    subtitleLang: '语言',
    transcribe: '生成 SRT',
    ocrImage: '图片',
    ocrDet: '检测模型',
    ocrRec: '识别模型',
    ocrDict: '字典',
    runOcr: '识别',
    ocrHint: 'OCR 载荷尚未随包安装时，这里会明确报 ocr-manifest 缺口。',
    imagePrompt: '提示词',
    steps: '步数',
    size: '尺寸',
    generate: '出图',
    dataDir: '数据目录',
    payloadHint: '首装来源：安装包自带运行组件时直接落盘；数据根里的 payload-source.json 可覆盖为远程源，两者都逐文件校验。',
    needFile: '还没有选文件。',
    engineError: '引擎错误',
    emptyReply: '模型没有返回内容。',
    hwAccel: '加速推理',
    hwCpu: 'CPU 推理',
    hwCores: '核',
    hwUnsupported: '当前引擎没有后端',
    hwNone: '未检测到 GPU/NPU',
    starterTitle: '随身入门模型',
    starterNeed: '还没下载（约 {mib} MiB）。下载后先按 sha256 校验，再交给对话引擎。',
    starterReady: '已就绪（{mib} MiB）。启动对话引擎就会用它。',
    starterBusy: '正在下载并校验…支持断点续传，约 {mib} MiB。',
    starterInstall: '下载入门模型',
    starterMeta: '{id} · {license} · 建议内存 ≥ {ram} MiB',
    hubTitle: '可选：接入 lain42.top 算力中心',
    hubHint: '只保存开关和一条命令；外壳不自动连接、不开入站端口。积分与结算都在服务端，客户端不碰支付。',
    hubEnable: '启用（仅保存配置）',
    hubUser: 'SSH 用户名（可留空）',
    hubKey: '私钥路径',
    hubSave: '保存',
    hubSaved: '已保存；尚未连接。',
    hubNode: '节点 ID'
  },
  en: {
    appName: 'Local AI Studio',
    tagline: 'runs locally, never uploaded',
    bootError: 'Tauri IPC is unavailable: this page only works inside the shell window, so controls are disabled.',
    tabChat: 'Chat',
    tabSubtitle: 'Subtitles',
    tabOcr: 'OCR',
    tabImage: 'Images',
    idle: 'stopped',
    running: 'running',
    notInstalled: 'runtime not installed',
    notInstalledHint: 'The engine payload is not in the data folder yet; Install runtime writes it and verifies every file.',
    bundledHint: 'This installer carries the runtime: Install runtime copies it into the data folder, offline, verifying every file.',
    remoteHint: 'A remote source is configured in the data folder: Install runtime downloads it and verifies every file.',
    payloadUnconfigured: 'No payload-source.json in the data folder, and this installer carries no bundled runtime: there is no first-install source.',
    installPayload: 'Install runtime',
    installBusy: 'Installing runtime… {seen}/{total}',
    unavailable: 'not available on this platform',
    unavailableHint: 'Upstream publishes no engine for this platform; only capabilities in the contract can be installed here.',
    oneShot: 'one-shot process',
    startEngine: 'Start engine',
    stopEngine: 'Stop',
    send: 'Send',
    chatPlaceholder: 'Say something to the local model…',
    subtitlePick: 'Audio or video',
    subtitleLang: 'Language',
    transcribe: 'Make SRT',
    ocrImage: 'Image',
    ocrDet: 'Detection model',
    ocrRec: 'Recognition model',
    ocrDict: 'Dictionary',
    runOcr: 'Recognize',
    ocrHint: 'If the OCR payload is not installed yet, this reports the ocr-manifest gap explicitly.',
    imagePrompt: 'Prompt',
    steps: 'Steps',
    size: 'Size',
    generate: 'Generate',
    dataDir: 'Data dir',
    payloadHint: 'First-install source: an installer-bundled runtime copies straight in; payload-source.json in the data folder overrides it with a remote source. Both verify every file.',
    needFile: 'No file selected yet.',
    engineError: 'Engine error',
    emptyReply: 'The model returned no content.',
    hwAccel: 'accelerated',
    hwCpu: 'CPU',
    hwCores: 'cores',
    hwUnsupported: 'no backend in this engine',
    hwNone: 'no GPU/NPU detected',
    starterTitle: 'Pocket starter model',
    starterNeed: 'Not downloaded yet (about {mib} MiB). It is checked against sha256 before the chat engine uses it.',
    starterReady: 'Ready ({mib} MiB). Starting the chat engine will use it.',
    starterBusy: 'Downloading and verifying… resumable, about {mib} MiB.',
    starterInstall: 'Download starter model',
    starterMeta: '{id} · {license} · needs ≥ {ram} MiB RAM',
    hubTitle: 'Optional: join the lain42.top compute hub',
    hubHint: 'Only a toggle and a command are saved. The shell never connects on its own and opens no inbound port. Credits and payouts live server-side; the client never touches payments.',
    hubEnable: 'Enable (saves config only)',
    hubUser: 'SSH user (optional)',
    hubKey: 'Private key path',
    hubSave: 'Save',
    hubSaved: 'Saved; not connected.',
    hubNode: 'Node ID'
  }
};

const TAB_CAP = { chat: '对话', subtitle: '字幕', ocr: '文字识别', image: '生图' };
const SERVER_TABS = new Set(['chat', 'subtitle', 'image']);

const el = (id) => document.getElementById(id);
const tauri = window.__TAURI__;
const invoke = tauri && tauri.core && tauri.core.invoke;

let lang = loadLang();
let activeTab = 'chat';
let busy = false;
let statusRows = [];
let unavailable = new Set();
let history = [];
let payloadSource = null;
let payloadSourceError = null;
let hardware = null;
let starter = null;
let hubState = null;

function loadLang() {
  try {
    const saved = window.localStorage.getItem('lai-lang');
    return I18N[saved] ? saved : 'zh';
  } catch (_e) {
    return 'zh';
  }
}

function saveLang() {
  try {
    window.localStorage.setItem('lai-lang', lang);
  } catch (_e) {
    /* 存储不可用不影响界面 */
  }
}

function t(key) {
  return (I18N[lang] && I18N[lang][key]) || I18N.zh[key] || key;
}

function applyLang() {
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
  document.title = t('appName');
  document.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-ph]').forEach((node) => {
    node.setAttribute('placeholder', t(node.dataset.i18nPh));
  });
  el('lang').textContent = lang === 'zh' ? 'EN' : '中';
  renderStatus();
  renderHardware();
  renderStarter();
  renderHub();
}

function selectTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.tab').forEach((node) => {
    node.classList.toggle('active', node.dataset.tab === tab);
  });
  document.querySelectorAll('.panel').forEach((node) => {
    node.classList.toggle('active', node.id === 'panel-' + tab);
  });
  el('enginebar').hidden = !SERVER_TABS.has(tab);
  renderStatus();
}

async function run(task) {
  if (busy) return;
  busy = true;
  setButtonsDisabled(true);
  try {
    await task();
  } catch (err) {
    showError(String(err));
  } finally {
    busy = false;
    setButtonsDisabled(false);
    refreshStatus();
  }
}

function setButtonsDisabled(value) {
  document.querySelectorAll('main button, #engine-start, #engine-stop, #payload-install, #starter-install, #hub-save').forEach((node) => {
    node.disabled = value || unavailable.has(node.dataset.capability);
  });
}

function showError(message) {
  if (activeTab === 'chat') {
    appendMessage('error', message);
  } else if (activeTab === 'subtitle') {
    const out = el('sub-out');
    out.hidden = false;
    out.textContent = message;
  } else if (activeTab === 'ocr') {
    const pre = document.createElement('pre');
    pre.textContent = message;
    el('ocr-out').prepend(pre);
  } else {
    el('img-note').textContent = message;
  }
  el('engine-state').textContent = message;
}

function appendMessage(role, text) {
  const node = document.createElement('div');
  node.className = 'msg ' + role;
  node.textContent = text;
  el('messages').appendChild(node);
  el('messages').scrollTop = el('messages').scrollHeight;
}

function extractContent(response) {
  const choice = response && response.choices && response.choices[0];
  const content = choice && choice.message && choice.message.content;
  if (typeof content === 'string' && content.trim() !== '') return content;
  if (Array.isArray(content)) {
    const joined = content.map((part) => (part && (part.text || part.content)) || '').join('');
    if (joined.trim() !== '') return joined;
  }
  return t('emptyReply');
}

async function refreshStatus() {
  if (!invoke) return;
  try {
    statusRows = await invoke('status');
  } catch (err) {
    el('engine-chip').textContent = String(err);
    return;
  }
  try {
    payloadSource = await invoke('payload_source');
    payloadSourceError = null;
  } catch (err) {
    payloadSource = null;
    payloadSourceError = String(err);
  }
  try {
    starter = await invoke('starter_status');
  } catch (_err) {
    starter = null;
  }
  unavailable = new Set(statusRows
    .filter((row) => row.available === false)
    .map((row) => row.capability));
  document.querySelectorAll('[data-capability]').forEach((node) => {
    node.disabled = busy || unavailable.has(node.dataset.capability);
  });
  renderStatus();
  renderStarter();
}

function renderHardware() {
  const box = el('env');
  if (!hardware) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const backend = hardware.backend || {};
  const chip = el('env-backend');
  chip.textContent = (backend.accelerated ? t('hwAccel') : t('hwCpu'))
    + ' · ' + (backend.label || 'CPU');
  chip.title = backend.reason || '';

  const bits = [];
  const cpu = hardware.cpu || {};
  if (cpu.brand) bits.push(cpu.brand);
  if (cpu.cores) bits.push(cpu.cores + ' ' + t('hwCores'));
  if (hardware.ram_mib) bits.push((hardware.ram_mib / 1024).toFixed(1) + ' GiB');
  bits.push(hardware.os + '/' + hardware.arch);
  el('env-cpu').textContent = bits.join(' · ');

  const accel = el('env-accel');
  accel.textContent = '';
  const rows = hardware.accelerators || [];
  if (rows.length === 0) {
    const none = document.createElement('span');
    none.className = 'chip';
    none.textContent = t('hwNone');
    accel.appendChild(none);
    return;
  }
  rows.slice(0, 4).forEach((row) => {
    const node = document.createElement('span');
    node.className = 'chip' + (row.usable ? ' ok' : '');
    let text = (row.kind === 'npu' ? 'NPU' : 'GPU') + ' · ' + row.name;
    if (row.backend) text += ' · ' + row.backend.toUpperCase();
    if (!row.usable) text += ' · ' + t('hwUnsupported');
    node.textContent = text;
    node.title = row.note || '';
    accel.appendChild(node);
  });
}

function renderStarter() {
  const box = el('starter');
  box.hidden = !starter;
  if (!starter) return;
  const mib = starter.size_mib || Math.round((starter.bytes || 0) / 1048576);
  const button = el('starter-install');
  const state = el('starter-state');
  el('starter-meta').textContent = t('starterMeta')
    .replace('{id}', starter.id || '')
    .replace('{license}', starter.license || '')
    .replace('{ram}', starter.ram_mib_min || 0);
  if (starter.installed) {
    state.textContent = t('starterReady').replace('{mib}', mib);
    button.hidden = true;
  } else {
    state.textContent = t('starterNeed').replace('{mib}', mib);
    button.hidden = false;
    button.disabled = busy;
  }
}

function renderHub() {
  if (!hubState) return;
  el('hub-enabled').checked = hubState.enabled === true;
  el('hub-user').value = hubState.ssh_user || '';
  el('hub-key').value = hubState.ssh_key || '';
  const command = el('hub-command');
  command.hidden = !hubState.ssh_command;
  command.textContent = hubState.ssh_command || '';
  el('hub-node').textContent = hubState.node_id
    ? t('hubNode') + ': ' + hubState.node_id
    : '';
}

function renderStatus() {
  const cap = TAB_CAP[activeTab];
  const row = statusRows.find((item) => item.capability === cap);
  const installButton = el('payload-install');
  installButton.hidden = true;
  if (row && row.available === false) {
    el('engine-chip').textContent = cap + ' · ' + t('unavailable');
    el('engine-state').textContent = cap + ' · ' + t('unavailableHint');
    el('engine-start').disabled = true;
    el('engine-stop').disabled = true;
    return;
  }
  if (activeTab === 'ocr') {
    el('engine-chip').textContent = cap + ' · ' + t('oneShot');
    return;
  }
  if (row && row.installed === false) {
    el('engine-chip').textContent = cap + ' · ' + t('notInstalled');
    const hint = payloadSource
      ? t(payloadSource.mode === 'bundled' ? 'bundledHint' : 'remoteHint')
      : (payloadSourceError || t('payloadUnconfigured'));
    el('engine-state').textContent = cap + ' · ' + hint;
    installButton.hidden = !payloadSource;
    if (!busy) {
      installButton.disabled = !payloadSource;
      el('engine-start').disabled = row.running === true;
      el('engine-stop').disabled = row.running !== true;
    }
    return;
  }  if (!row) {
    el('engine-chip').textContent = cap + ' · ' + t('idle');
    el('engine-state').textContent = cap + ' · ' + t('idle');
    return;
  }
  const state = row.running ? t('running') + ' (:' + row.port + ')' : t('idle');
  el('engine-chip').textContent = cap + ' · ' + state;
  el('engine-state').textContent = cap + ' · ' + state;
  if (!busy) {
    el('engine-start').disabled = row.running;
    el('engine-stop').disabled = !row.running;
  }
}

async function startEngine() {
  const capability = TAB_CAP[activeTab];
  await run(async () => {
    await invoke('start_engine', { capability });
  });
}

async function stopEngine() {
  const capability = TAB_CAP[activeTab];
  await run(async () => {
    await invoke('stop_engine', { capability });
  });
}

function listenPayloadProgress() {
  const event = tauri && tauri.event;
  if (!event || typeof event.listen !== 'function') return;
  event.listen('payload-progress', (message) => {
    const payload = message && message.payload;
    if (!payload) return;
    el('engine-state').textContent = t('installBusy')
      .replace('{seen}', payload.seen)
      .replace('{total}', payload.total);
  }).catch(() => {
    /* 拿不到进度通道不影响安装本身 */
  });
}

async function installPayload() {
  if (!payloadSource) return;
  await run(async () => {
    await invoke('install_payload');
  });
}

async function installStarter() {
  if (!starter) return;
  await run(async () => {
    const mib = starter.size_mib || Math.round((starter.bytes || 0) / 1048576);
    el('starter-state').textContent = t('starterBusy').replace('{mib}', mib);
    starter = await invoke('starter_install');
  });
}

async function saveHub() {
  const enabled = el('hub-enabled').checked;
  const sshUser = el('hub-user').value.trim();
  const sshKey = el('hub-key').value.trim();
  el('hub-state').textContent = '';
  try {
    hubState = await invoke('hub_save', { enabled, sshUser, sshKey });
    renderHub();
    el('hub-state').textContent = t('hubSaved');
  } catch (err) {
    el('hub-state').textContent = String(err);
  }
}

async function readBytes(input, required) {
  const file = input.files && input.files[0];
  if (!file) {
    if (required) throw new Error(t('needFile'));
    return null;
  }
  const buffer = new Uint8Array(await file.arrayBuffer());
  return {
    name: file.name,
    type: file.type || 'application/octet-stream',
    bytes: Array.from(buffer)
  };
}

async function sendChat() {
  const text = el('chat-input').value.trim();
  if (text === '') return;
  el('chat-input').value = '';
  const message = { role: 'user', content: text };
  history.push(message);
  appendMessage('user', text);
  await run(async () => {
    const response = await invoke('chat', { body: { messages: history } });
    const content = extractContent(response);
    history.push({ role: 'assistant', content });
    appendMessage('assistant', content);
  });
}

async function makeSrt() {
  const language = el('sub-lang').value || null;
  await run(async () => {
    const file = await readBytes(el('sub-file'), true);
    const srt = await invoke('transcribe', {
      filename: file.name,
      contentType: file.type,
      audio: file.bytes,
      language
    });
    const out = el('sub-out');
    out.hidden = false;
    out.textContent = srt;
  });
}

async function runOcr() {
  await run(async () => {
    const image = await readBytes(el('ocr-image'), true);
    const det = await readBytes(el('ocr-det'), true);
    const rec = await readBytes(el('ocr-rec'), true);
    const dict = await readBytes(el('ocr-dict'), true);
    const rows = await invoke('ocr', {
      name: image.name,
      image: image.bytes,
      det: det.bytes,
      rec: rec.bytes,
      dict: dict.bytes
    });
    const out = el('ocr-out');
    out.textContent = '';
    rows.forEach((row) => {
      const pre = document.createElement('pre');
      pre.textContent = row.file + '\n' + row.text;
      out.appendChild(pre);
    });
  });
}

async function generateImage() {
  const prompt = el('img-prompt').value.trim();
  if (prompt === '') return;
  const steps = Number(el('img-steps').value) || 3;
  const size = Number(el('img-size').value) || 256;
  await run(async () => {
    const out = await invoke('image', {
      prompt,
      steps,
      width: size,
      height: size
    });
    const img = el('img-out');
    img.src = out.data;
    img.hidden = false;
    el('img-note').textContent = out.path;
  });
}

function wire() {
  document.querySelectorAll('.tab').forEach((node) => {
    node.addEventListener('click', () => selectTab(node.dataset.tab));
  });
  el('lang').addEventListener('click', () => {
    lang = lang === 'zh' ? 'en' : 'zh';
    saveLang();
    applyLang();
  });
  el('payload-install').addEventListener('click', installPayload);
  el('starter-install').addEventListener('click', installStarter);
  el('hub-save').addEventListener('click', saveHub);
  el('engine-start').addEventListener('click', startEngine);
  el('engine-stop').addEventListener('click', stopEngine);
  el('chat-send').addEventListener('click', sendChat);
  el('chat-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      sendChat();
    }
  });
  el('sub-run').addEventListener('click', makeSrt);
  el('ocr-run').addEventListener('click', runOcr);
  el('img-run').addEventListener('click', generateImage);
}

function init() {
  wire();
  selectTab('chat');
  listenPayloadProgress();
  invoke('data_root')
    .then((value) => {
      el('data-root').textContent = value;
    })
    .catch((err) => {
      el('data-root').textContent = String(err);
    });
  invoke('hardware_profile')
    .then((value) => {
      hardware = value;
      renderHardware();
    })
    .catch((err) => {
      el('env').hidden = false;
      el('env-backend').textContent = t('hwCpu');
      el('env-cpu').textContent = String(err);
    });
  invoke('hub_config')
    .then((value) => {
      hubState = value;
      renderHub();
    })
    .catch((err) => {
      el('hub-state').textContent = String(err);
    });
  refreshStatus();
  window.setInterval(() => {
    if (!busy) refreshStatus();
  }, 3000);
}

applyLang();
if (!invoke) {
  el('boot').hidden = false;
  document.querySelectorAll('button, textarea, input, select').forEach((node) => {
    node.disabled = true;
  });
} else {
  init();
}
