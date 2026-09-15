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
      setAttribute() {}, focus() {}, append(value) { this.children.push(value); },
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
  const sandbox = {console, document: {getElementById: element, querySelector: element, createElement: () => ({append() {}}), body: element('body')},
    window: {AudioContext, addEventListener() {}}, navigator: {mediaDevices: {getUserMedia: async () => {
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
test('empty text does not connect and pending reply prevents duplicate sends', async () => {
  const p = page(); await p.click('modeBtn'); await p.send('   ');
  assert.equal(p.stats().peers, 0);
  await p.send('First'); await p.send('Second'); assert.equal(p.sent.length, 2);
});
