const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const html = fs.readFileSync('src/omarchy_ai/phone/static/index.html', 'utf8');
const source = html.split('<script>')[1].split('</script>')[0];
function page({offerFails = false} = {}) {
  const elements = new Map(), sent = [];
  let micRequests = 0, stopped = 0, peers = 0;
  function element(id) {
    if (!elements.has(id)) elements.set(id, {value: '', hidden: true, style: {}, dataset: {}, children: [],
      handlers: {}, addEventListener(name, fn) { this.handlers[name] = fn; },
      setAttribute() {}, removeAttribute(name) {delete this[name];}, focus() {}, append(value) { this.children.push(value); },
      appendChild(value) { this.children.push(value); }, getContext() { return {setTransform() {}}; }, play: async () => {}});
    return elements.get(id);
  }
  class Peer {
    constructor() { peers++; this.iceGatheringState = 'complete'; }
    addTransceiver(kind, opts) { assert.equal(opts.direction, 'sendrecv'); return {sender: {replaceTrack: async () => {}}}; }
    createDataChannel() { this.channel = {readyState: 'open', send: s => sent.push(JSON.parse(s)), close() {}}; return this.channel; }
    async createOffer() { return {sdp: 'test-offer'}; }
    async setLocalDescription(offer) { this.localDescription = offer; }
    async setRemoteDescription() { this.channel.onopen(); }
    close() {}
  }
  class AudioContext { async resume() {} async close() {} }
  let created = 0;
  const windowHandlers = {};
  const sandbox = {console, document: {getElementById: element, querySelector: element, createElement: () => element('created-' + created++), body: element('body')},
    window: {AudioContext, handlers:windowHandlers, addEventListener(name, fn, options) {windowHandlers[name] = {fn, options};}}, navigator: {mediaDevices: {getUserMedia: async () => {
      micRequests++; const track = {stop() {stopped++;}}; return {getTracks: () => [track], getAudioTracks: () => [track]};
    }}}, RTCPeerConnection: Peer, Uint8Array, Math, Set, Promise, AbortSignal,
    performance: {now: () => 0}, innerWidth: 390, innerHeight: 844, devicePixelRatio: 1,
    matchMedia: () => ({matches: false}), addEventListener() {}, requestAnimationFrame() {}, setInterval() {},
    setTimeout() {return 1;}, clearTimeout() {},
    fetch: async url => url === '/api/live/offer' ? {ok: !offerFails, json: async () => offerFails ? {error: 'test failure'} : {sdp: 'answer'}} : {ok: true, json: async () => ({available: false})},
  };
  vm.createContext(sandbox); vm.runInContext(source, sandbox);
  return {element, sent, sandbox, stats: () => ({micRequests, stopped, peers}),
    async click(id) { await element(id).handlers.click({currentTarget: element(id)}); },
    async send(text) { element('messageInput').value = text; await element('composer').handlers.submit({preventDefault() {}}); }};
}
test('typed conversation connects without microphone; repeated messages reuse session', async () => {
  const p = page(); await p.click('modeBtn'); await p.send('Hello');
  assert.equal(p.stats().micRequests, 0);
  assert.equal(p.element('remoteAudio').muted, true);
  assert.equal(p.sent[0].item.content[0].text, 'Hello');
  assert.equal(p.sent[0].type, 'response.item.create');
  assert.equal(p.sent[1].type, 'response.create');
  assert.equal(p.element('messageInput').value, '');
  vm.runInContext(`onDataMessage(JSON.stringify({type: 'response.event', event: {type: 'response.completed'}}))`, p.sandbox);
  await p.send('Next'); assert.equal(p.stats().peers, 1); assert.equal(p.sent.length, 4);
  await p.click('modeBtn'); assert.equal(p.stats().micRequests, 1);
  await p.click('modeBtn'); assert.equal(p.stats().stopped, 1);
  assert.equal(p.element('remoteAudio').muted, true);
});
test('failed connection retains draft and permits retry', async () => {
  const p = page({offerFails: true}); await p.click('modeBtn'); await p.send('Keep this');
  assert.equal(p.element('messageInput').value, 'Keep this');
  assert.equal(p.element('sendBtn').disabled, false); assert.equal(p.sent.length, 0);
});
test('enable audio retries playback without an output selection API', async () => {
  const p = page();
  let plays = 0;
  const audio = p.element('remoteAudio');
  audio.srcObject = {};
  audio.muted = true;
  audio.play = async () => { plays++; };
  await p.click('mirrorSpeakerBtn');
  assert.equal(plays, 1);
  assert.equal(audio.muted, false);
  assert.equal(audio.volume, 1);
  assert.match(p.element('speakerStatus').textContent, /Audio enabled/);
});
test('visualization never replaces the received playback stream', () => {
  const p = page();
  const links = [];
  const node = name => ({name, connect(target) { links.push([name, target.name]); }});
  const original = {};
  p.element('remoteAudio').srcObject = original;
  p.sandbox.testContext = {resume:async()=>{}, currentTime:0,
    createMediaStreamSource:()=>node('source'), createAnalyser:()=>node('analyser'),
    destination:{name:'speaker'}};
  vm.runInContext('audioCtx = testContext; setupAnalyser({});', p.sandbox);
  assert.equal(p.element('remoteAudio').srcObject, original);
  assert.deepEqual(links, [['source','analyser']]);
});
test('mirror zoom is bounded and keyboard resizes the entire mirror viewport', () => {
  const p = page();
  vm.runInContext('setMirrorZoom(8)', p.sandbox);
  assert.equal(p.element('mirrorVideo').style.width, '400%');
  vm.runInContext('setMirrorZoom(0)', p.sandbox);
  assert.equal(p.element('mirrorVideo').style.width, '100%');
  p.sandbox.window.visualViewport = {height:310, offsetTop:12};
  vm.runInContext('fitKeyboard()', p.sandbox);
  assert.equal(p.element('mirrorPanel').style.height, '310px');
  assert.equal(p.element('mirrorPanel').style.top, '12px');
});
test('amplification uses hardware output and falls back when audio context suspends', () => {
  const p = page();
  const links = [];
  const node = name => ({name, connect(target) {links.push([name, target.name]);}, disconnect() {}});
  const gain = {...node('gain'), gain:{value:0}};
  const limiter = {...node('limiter'), threshold:{}, knee:{}, ratio:{}, attack:{}, release:{}};
  const original = {};
  p.element('remoteAudio').srcObject = original;
  p.sandbox.testContext = {state:'running', destination:{name:'speaker'},
    createMediaStreamSource:()=>node('source'), createGain:()=>gain,
    createDynamicsCompressor:()=>limiter};
  vm.runInContext('audioCtx = testContext; setupPhoneAmplification({})', p.sandbox);
  assert.equal(gain.gain.value, 4);
  assert.equal(p.element('remoteAudio').muted, true);
  assert.equal(p.element('remoteAudio').srcObject, original);
  assert.deepEqual(links, [['source','gain'],['gain','limiter'],['limiter','speaker']]);
  p.sandbox.testContext.state = 'suspended';
  p.sandbox.testContext.onstatechange();
  assert.equal(gain.gain.value, 0);
  assert.equal(p.element('remoteAudio').muted, false);
  p.sandbox.testContext.state = 'running';
  vm.runInContext('castActive = true; syncPhonePlayback()', p.sandbox);
  assert.equal(gain.gain.value, 0);
  assert.equal(p.element('remoteAudio').muted, true);
  vm.runInContext("castActive = false; mode = 'text'; usePhone()", p.sandbox);
  assert.equal(gain.gain.value, 0);
  assert.equal(p.element('remoteAudio').muted, true);
});
test('fullscreen releases orientation lock without a rotate button', async () => {
  const p = page(); let unlocked = 0;
  p.sandbox.window.screen = {orientation:{unlock(){unlocked++;}}};
  await vm.runInContext('enterFullscreen()', p.sandbox);
  assert.equal(unlocked, 1);
  assert.equal(p.element('mirrorControls').children.some(b => b.textContent === 'Rotate'), false);
});
test('both views request fullscreen on the whole page and preserve it', async () => {
  const p = page(); let requests = 0;
  p.sandbox.document.documentElement = {async requestFullscreen() {
    requests++; p.sandbox.document.fullscreenElement = this;
  }};
  await p.click('talkFullscreen');
  assert.equal(requests, 1);
  await p.click('mirrorFullscreen');
  assert.equal(requests, 1);
});
test('Talk requests fullscreen before starting the voice connection', async () => {
  const p = page(); let requests = 0;
  p.sandbox.document.documentElement = {async requestFullscreen() {requests++;}};
  await p.click('talkBtn');
  assert.equal(requests, 1);
});
test('first page tap requests fullscreen once, in either view', async () => {
  for (const mirror of ['on', 'off']) {
    const p = page(); let requests = 0;
    p.element('body').dataset.mirror = mirror;
    p.sandbox.document.documentElement = {async requestFullscreen() {requests++;}};
    const handler = p.sandbox.window.handlers.click;
    assert.equal(handler.options.once, true);
    await handler.fn();
    assert.equal(requests, 1);
  }
});
test('mirror hands-free keeps microphone during replies until Unlisten', async () => {
  const p = page();
  p.sandbox.navigator.audioSession = {type:'auto'};
  vm.runInContext('mirrorOn = true', p.sandbox);
  await p.click('mirrorListen');
  assert.equal(p.stats().micRequests, 1);
  assert.equal(p.stats().stopped, 0);
  assert.equal(p.element('mirrorListen').textContent, 'Unlisten');
  await p.send('Describe the screen');
  assert.equal(p.stats().stopped, 0);
  assert.equal(p.sandbox.navigator.audioSession.type, 'auto');
  await p.click('mirrorListen');
  assert.equal(p.stats().stopped, 1);
  assert.equal(vm.runInContext('localStream', p.sandbox), null);
  assert.equal(p.element('mirrorListen').textContent, 'Listen');
  await p.click('mirrorListen');
  assert.equal(p.stats().peers, 2);
  assert.equal(p.stats().micRequests, 2);
  assert.equal(p.sandbox.navigator.audioSession.type, 'auto');
});
test('mirror text does not acquire microphone; normal voice still does', async () => {
  const p = page();
  vm.runInContext('mirrorOn = true', p.sandbox);
  await p.click('modeBtn');
  await p.send('Describe the screen');
  assert.equal(p.stats().micRequests, 0);
  const normal = page();
  await vm.runInContext('start()', normal.sandbox);
  assert.equal(normal.stats().micRequests, 1);
});
test('switching mirror views preserves the live microphone and audio output', async () => {
  const p = page();
  await vm.runInContext('start()', p.sandbox);
  const stream = vm.runInContext('localStream', p.sandbox);
  const audio = p.element('remoteAudio');
  const output = {}; audio.srcObject = output;
  vm.runInContext('setMirror(true); setMirror(false)', p.sandbox);
  assert.equal(vm.runInContext('localStream', p.sandbox), stream);
  assert.equal(audio.srcObject, output);
  assert.equal(p.stats().stopped, 0);
  assert.equal(p.stats().micRequests, 1);
  assert.equal(p.stats().peers, 1);
});
test('microphone state distinguishes a connected text session from a live microphone', async () => {
  const p = page(); await p.click('modeBtn'); await p.send('Hello');
  assert.equal(p.element('body').dataset.mic, 'off');
  await p.click('modeBtn');
  assert.equal(p.element('body').dataset.mic, 'on');
  vm.runInContext('hangup()', p.sandbox);
  assert.equal(p.element('body').dataset.mic, 'off');
});
test('empty text does not connect and pending reply prevents duplicate sends', async () => {
  const p = page(); await p.click('modeBtn'); await p.send('   ');
  assert.equal(p.stats().peers, 0);
  await p.send('First'); await p.send('Second'); assert.equal(p.sent.length, 2);
});
