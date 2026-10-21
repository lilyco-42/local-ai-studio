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
    notInstalledHint: '引擎负载还没装进数据目录；点“安装运行组件”按清单下载并逐文件校验。',
    payloadUnconfigured: '数据目录里还没有 payload-source.json：写入交付清单地址与下载基址后重开窗口，这里才会出现安装入口。',
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
    payloadHint: '安装包只装外壳；引擎负载由“安装运行组件”从配置的源取回并逐文件校验。',
    needFile: '还没有选文件。',
    engineError: '引擎错误',
    emptyReply: '模型没有返回内容。'
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
    notInstalledHint: 'The engine payload is not in the data folder yet; Install runtime fetches it from the configured source and verifies every file.',
    payloadUnconfigured: 'No payload-source.json in the data folder yet: add the manifest URL and download bases, then reopen the window to get the install button.',
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
    payloadHint: 'The installer ships only the shell; Install runtime fetches engines from the configured source and verifies each file.',
    needFile: 'No file selected yet.',
    engineError: 'Engine error',
    emptyReply: 'The model returned no content.'
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
  document.querySelectorAll('main button, #engine-start, #engine-stop, #payload-install').forEach((node) => {
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
  unavailable = new Set(statusRows
    .filter((row) => row.available === false)
    .map((row) => row.capability));
  document.querySelectorAll('[data-capability]').forEach((node) => {
    node.disabled = busy || unavailable.has(node.dataset.capability);
  });
  renderStatus();
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
    el('engine-state').textContent = cap + ' · '
      + (payloadSource ? t('notInstalledHint') : (payloadSourceError || t('payloadUnconfigured')));
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
