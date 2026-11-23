#!/usr/bin/env node
/* Chrome E2E harness for the Presto WYSIWYG alignment guides module.
 *
 * Proves presto/wysiwyg-guides.js works in a REAL browser at REAL Presto zoom.
 * It builds the exact edit-mode environment Presto creates (engine sidebar +
 * viewport zoom), injects BOTH wysiwyg.js and the guides module, then — because
 * wysiwyg.js is not yet wired to the guides — drives the guides module DIRECTLY
 * with synthetic dragStart/dragMove and resizeStart/resizeMove calls whose
 * coordinates are derived from real element bounding rects. It asserts the
 * returned coordinates snapped, and that guide + readout elements actually
 * appear in the live DOM at the right client positions, then screenshots.
 *
 * Usage: node tests/chrome-guides-e2e.js
 *   Screenshot: /tmp/webslides-guides-e2e.png (override WEBSLIDES_TEST_SCREENSHOT).
 */

const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

const projectRoot = path.resolve(__dirname, '..');
const deckPath = process.env.PRESTO_WYSIWYG_DECK || 'tests/wysiwyg-e2e-deck.html';
const workingSlide = Number(process.env.PRESTO_WYSIWYG_SLIDE || 1);
const screenshotPath = process.env.WEBSLIDES_TEST_SCREENSHOT || '/tmp/webslides-guides-e2e.png';
const chromePath = process.env.CHROME ||
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const viewportW = 1100;
const viewportH = 800;

class CDP {
    constructor(wsURL) {
        this.ws = new WebSocket(wsURL);
        this.nextId = 1;
        this.pending = new Map();
        this.listeners = new Map();
        this.ws.addEventListener('message', (event) => {
            const msg = JSON.parse(event.data);
            if (msg.id && this.pending.has(msg.id)) {
                const { resolve, reject } = this.pending.get(msg.id);
                this.pending.delete(msg.id);
                if (msg.error) reject(new Error(JSON.stringify(msg.error)));
                else resolve(msg.result || {});
                return;
            }
            const handlers = this.listeners.get(msg.method) || [];
            handlers.forEach((handler) => handler(msg.params || {}));
        });
    }
    async open() {
        if (this.ws.readyState === WebSocket.OPEN) return;
        await new Promise((resolve, reject) => {
            this.ws.addEventListener('open', resolve, { once: true });
            this.ws.addEventListener('error', reject, { once: true });
        });
    }
    send(method, params = {}) {
        const id = this.nextId++;
        this.ws.send(JSON.stringify({ id, method, params }));
        return new Promise((resolve, reject) => { this.pending.set(id, { resolve, reject }); });
    }
    once(method) {
        return new Promise((resolve) => {
            const handlers = this.listeners.get(method) || [];
            handlers.push(resolve);
            this.listeners.set(method, handlers);
        });
    }
    on(method, handler) {
        const handlers = this.listeners.get(method) || [];
        handlers.push(handler);
        this.listeners.set(method, handlers);
    }
    close() { this.ws.close(); }
}

function contentType(filePath) {
    if (filePath.endsWith('.html')) return 'text/html';
    if (filePath.endsWith('.js')) return 'application/javascript';
    if (filePath.endsWith('.css')) return 'text/css';
    if (filePath.endsWith('.svg')) return 'image/svg+xml';
    return 'application/octet-stream';
}

async function startServer() {
    const server = http.createServer(async (req, res) => {
        try {
            const rawPath = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
            const filePath = path.resolve(projectRoot, rawPath.slice(1) || deckPath);
            if (!filePath.startsWith(projectRoot)) { res.writeHead(403); res.end('Forbidden'); return; }
            const data = await fs.readFile(filePath);
            res.writeHead(200, { 'Content-Type': contentType(filePath) });
            res.end(data);
        } catch { res.writeHead(404); res.end('Not found'); }
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    return server;
}

function waitForDevTools(chrome) {
    return new Promise((resolve, reject) => {
        let stderr = '';
        const timer = setTimeout(() => reject(new Error('Timed out waiting for Chrome DevTools URL')), 10000);
        chrome.on('error', (error) => { clearTimeout(timer); reject(new Error(`Chrome failed to launch: ${error.message}`)); });
        chrome.stderr.on('data', (chunk) => {
            stderr += chunk.toString();
            const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
            if (match) { clearTimeout(timer); resolve(match[1]); }
        });
        chrome.on('exit', (code) => { clearTimeout(timer); reject(new Error(`Chrome exited before DevTools was ready: ${code}\n${stderr}`)); });
    });
}

async function newPage(browserWsURL, url) {
    const port = new URL(browserWsURL).port;
    const response = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
    if (!response.ok) throw new Error(`Chrome /json/new failed: ${response.status}`);
    return response.json();
}

async function evaluate(client, expression) {
    const result = await client.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
}

function assertTrue(name, condition, detail) {
    if (!condition) throw new Error(`${name}${detail !== undefined ? ` — ${JSON.stringify(detail)}` : ''}`);
    console.log(`PASS ${name}`);
}
function assertClose(name, actual, expected, tolerance = 1) {
    if (typeof actual !== 'number' || Math.abs(actual - expected) > tolerance) {
        throw new Error(`${name}: got ${actual}, expected ${expected} ±${tolerance}`);
    }
    console.log(`PASS ${name} (${actual.toFixed(1)} ≈ ${expected.toFixed(1)})`);
}

async function main() {
    let server, userDataDir, chrome, client;
    try {
        server = await startServer();
        const url = `http://127.0.0.1:${server.address().port}/${deckPath}`;
        userDataDir = await fs.mkdtemp(path.join(os.tmpdir(), 'webslides-guides-e2e-'));
        chrome = spawn(chromePath, [
            '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
            `--user-data-dir=${userDataDir}`, '--remote-debugging-port=0',
            `--window-size=${viewportW},${viewportH}`, 'about:blank'
        ], { stdio: ['ignore', 'ignore', 'pipe'] });

        const browserWsURL = await waitForDevTools(chrome);
        const target = await newPage(browserWsURL, url);
        client = new CDP(target.webSocketDebuggerUrl);
        await client.open();
        await client.send('Page.enable');
        await client.send('Runtime.enable');
        const runtimeErrors = [];
        client.on('Runtime.exceptionThrown', (params) => runtimeErrors.push(params));
        await client.send('Emulation.setDeviceMetricsOverride', { width: viewportW, height: viewportH, deviceScaleFactor: 1, mobile: false });
        const loaded = client.once('Page.loadEventFired');
        await client.send('Page.navigate', { url });
        await loaded;

        // Exact edit-mode environment: the slide-frame layout (edit-layout.js
        // when present, else the engine's viewport with the rail inset), the
        // edit chrome's rail, then both injected layers (wysiwyg.js is required
        // by the brief even though the guides run standalone here).
        const chromeCSS = await fs.readFile(path.join(projectRoot, 'presto/edit-chrome.css'), 'utf8');
        const chromeJS = await fs.readFile(path.join(projectRoot, 'presto/edit-chrome.js'), 'utf8');
        let layoutJS = '';
        try {
            layoutJS = await fs.readFile(path.join(projectRoot, 'presto/edit-layout.js'), 'utf8');
        } catch { /* before the engine's edit-layout.js lands */ }
        await evaluate(client, `(function(){ var s = document.createElement('style');
            s.textContent = ${JSON.stringify(chromeCSS)}; document.head.appendChild(s); return true; })()`);
        if (layoutJS) await evaluate(client, layoutJS + '\n;true');
        await evaluate(client, chromeJS + '\n;true');
        await evaluate(client, `
            (async function() {
                await new Promise((resolve) => {
                    (function check() {
                        if (window.WS && WS.slides && WS.slides.length &&
                            (window.PrestoLayout || typeof applyPrestoEditViewport === 'function')) resolve();
                        else requestAnimationFrame(check);
                    })();
                });
                var layout = function () {
                    if (window.PrestoLayout) return PrestoLayout.apply({ mode: 'edit', insetLeft: 232, insetBottom: 72, padding: 28 });
                    return applyPrestoEditViewport({ railWidth: 232 });
                };
                layout();
                window.addEventListener('resize', layout);
                PrestoEditChrome.install({ railVisible: true });
                WS.goToSlide(${workingSlide}, 0);
                await document.fonts.ready;
                await new Promise((r) => setTimeout(r, 120));
                window.__slide = function () { return WS.slides[${workingSlide}]; };
                return true;
            })()
        `);
        const wysiwygSource = await fs.readFile(path.join(projectRoot, 'presto/wysiwyg.js'), 'utf8');
        const guidesSource = await fs.readFile(path.join(projectRoot, 'presto/wysiwyg-guides.js'), 'utf8');
        await evaluate(client, wysiwygSource + '\n;true');
        await evaluate(client, guidesSource + '\n;true');
        await evaluate(client, '__prestoWysiwygGuides.init(); true');
        console.log('PASS environment ready (sidebar + viewport + wysiwyg + guides layers)');

        const scale = await evaluate(client, '__slide().getBoundingClientRect().width / 1280');
        assertTrue('slide is zoom-scaled below 1:1 (real Presto zoom)', scale > 0.2 && scale < 1, scale);

        const T = '__prestoWysiwygGuides.__test';

        // ===== Drag: snap a real element's center to the slide center =====
        // Derive box0 from the real element rect, propose a translate that lands
        // the element's center 3 slide-px short of the slide center (640) so a
        // snap must fire.
        const dragSetup = await evaluate(client, `
            (function() {
                var slide = __slide();
                var el = slide.querySelector('p') || slide.querySelector('h2, h1');
                var m = ${T}.slideMetrics(slide);
                var box0 = ${T}.boxInSlide(el, m.rect, m.scale);
                el.id = el.id || '__guides_drag_target';
                return { id: el.id, centerX: box0.left + box0.width / 2, left: box0.left, top: box0.top };
            })()
        `);
        const proposedX = 640 - dragSetup.centerX - 3;   // center 3px short of 640
        const dragResult = await evaluate(client, `
            (function() {
                var slide = __slide();
                var el = document.getElementById(${JSON.stringify(dragSetup.id)});
                __prestoWysiwygGuides.dragStart(el, slide);
                var out = __prestoWysiwygGuides.dragMove(el, slide, ${proposedX}, 0, {});
                var lr = ${T}.lastRender();
                var v = ${T}.dom.vLine;
                var rd = ${T}.dom.readout;
                var slideRect = slide.getBoundingClientRect();
                return {
                    outX: out.x,
                    snappedValue: lr && lr.snapX ? lr.snapX.value : null,
                    snappedKind: lr && lr.snapX ? lr.snapX.kind : null,
                    vVisible: v.classList.contains('visible'),
                    vCenterClass: v.classList.contains('pw-guide-center'),
                    vLeft: parseFloat(v.style.left),
                    readoutVisible: rd.classList.contains('visible'),
                    readoutText: rd.textContent,
                    scale: slide.getBoundingClientRect().width / 1280,
                    slideLeft: slideRect.left,
                    injected: slide.querySelectorAll('.presto-wysiwyg-chrome').length
                };
            })()
        `);
        assertTrue('drag produced a real snap on the x-axis', dragResult.snappedValue !== null, dragResult);
        assertClose('snapped translate brought the element center onto 640',
            dragResult.outX + dragSetup.centerX, 640, 0.7);
        assertTrue('snapped target is the slide center', dragResult.snappedKind === 'center', dragResult);
        assertTrue('vertical guide is visible in the live DOM', dragResult.vVisible, dragResult);
        assertTrue('center snap uses the emphasized (pw-guide-center) guide', dragResult.vCenterClass, dragResult);
        assertClose('guide is drawn at the snapped target in client px',
            dragResult.vLeft, dragResult.slideLeft + dragResult.snappedValue * dragResult.scale - 1, 1.5);
        assertTrue('readout is visible with content', dragResult.readoutVisible && /\d+, \d+/.test(dragResult.readoutText), dragResult);
        assertTrue('the guide overlay never injects into the slide DOM', dragResult.injected === 0, dragResult);

        // ===== Resize: snap a real element's right edge to the slide right edge =====
        const resizeResult = await evaluate(client, `
            (function() {
                var slide = __slide();
                var el = slide.querySelector('svg[data-ws-original-img], img, .box, div[style*="absolute"]') ||
                         slide.querySelector('p');
                var m = ${T}.slideMetrics(slide);
                var box0 = ${T}.boxInSlide(el, m.rect, m.scale);
                __prestoWysiwygGuides.dragEnd();
                __prestoWysiwygGuides.resizeStart(el, slide);
                // Propose a width whose right edge lands 3px short of the slide right (1280).
                var proposedW = (1280 - 3) - box0.left;
                var out = __prestoWysiwygGuides.resizeMove(el, slide, proposedW, undefined, {});
                var lr = ${T}.lastRender();
                var v = ${T}.dom.vLine;
                var rd = ${T}.dom.readout;
                return {
                    width: out.width,
                    right: box0.left + out.width,
                    snappedValue: lr && lr.snapX ? lr.snapX.value : null,
                    vVisible: v.classList.contains('visible'),
                    readoutVisible: rd.classList.contains('visible'),
                    readoutText: rd.textContent
                };
            })()
        `);
        assertTrue('resize produced a real snap on the right edge', resizeResult.snappedValue !== null, resizeResult);
        assertClose('right edge snapped to the slide right edge (1280)', resizeResult.right, 1280, 0.7);
        assertTrue('resize guide is visible in the live DOM', resizeResult.vVisible, resizeResult);
        assertTrue('resize readout shows w × h', resizeResult.readoutVisible && /\d+ × \d+/.test(resizeResult.readoutText), resizeResult);

        // Leave a drag snap on screen for the screenshot record.
        await evaluate(client, `
            (function() {
                var slide = __slide();
                var el = document.getElementById(${JSON.stringify(dragSetup.id)});
                __prestoWysiwygGuides.resizeEnd();
                __prestoWysiwygGuides.dragStart(el, slide);
                __prestoWysiwygGuides.dragMove(el, slide, ${proposedX}, 0, {});
                return true;
            })()
        `);

        if (runtimeErrors.length) {
            const detail = runtimeErrors[0].exceptionDetails?.exception?.description ||
                runtimeErrors[0].exceptionDetails?.text || 'unknown runtime error';
            throw new Error(`Uncaught runtime exception: ${detail}`);
        }
        console.log('PASS zero uncaught runtime exceptions across the whole run');

        const screenshot = await client.send('Page.captureScreenshot', { format: 'png' });
        await fs.writeFile(screenshotPath, Buffer.from(screenshot.data, 'base64'));
        console.log(`PASS screenshot: ${screenshotPath}`);
        console.log('GUIDES_E2E: passed');
    } finally {
        if (client) client.close();
        if (chrome) chrome.kill('SIGKILL');
        if (server) await new Promise((resolve) => server.close(resolve));
        if (userDataDir) spawnSync('trash', [userDataDir], { stdio: 'ignore' });
    }
}

main().catch((error) => { console.error(`FAIL ${error.message}`); process.exit(1); });
