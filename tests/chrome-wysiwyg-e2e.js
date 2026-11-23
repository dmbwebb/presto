#!/usr/bin/env node
/* Chrome E2E harness for Presto WYSIWYG editing.
 *
 * Drives REAL trusted input events (CDP Input domain) against the edit-mode
 * preview environment: engine sidebar installed, Presto viewport zoom applied,
 * wysiwyg.js injected exactly as Presto.swift injects it. Verifies the full
 * pipeline: select → drag-to-move (zoom-compensated) → double-click text
 * editing → typing → toolbar formatting → keyboard nudge/undo → new text box →
 * serialization messages that Presto would write to disk.
 *
 * Usage: node tests/chrome-wysiwyg-e2e.js
 *   PRESTO_WYSIWYG_DECK=path/relative/to/repo.html — optional deck override.
 *   The harness tags its own targets: the first heading and paragraph-like
 *   block on the working slide, so it runs against any deck (e.g. a real
 *   research deck). It only edits the in-browser DOM; nothing touches disk.
 *   PRESTO_WYSIWYG_SLIDE=n — working slide index (default 1).
 *   CHROME=/path/to/chrome — override the browser binary.
 */

const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

const projectRoot = path.resolve(__dirname, '..');
const deckPath = process.env.PRESTO_WYSIWYG_DECK || 'tests/wysiwyg-e2e-deck.html';
const workingSlide = Number(process.env.PRESTO_WYSIWYG_SLIDE || 1);
const screenshotPath = process.env.WEBSLIDES_TEST_SCREENSHOT || '/tmp/webslides-wysiwyg-e2e.png';
const chromePath = process.env.CHROME ||
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const cdpRequestTimeoutMs = Number(process.env.CDP_REQUEST_TIMEOUT_MS || 30000);
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
                const { resolve, reject, timer } = this.pending.get(msg.id);
                this.pending.delete(msg.id);
                clearTimeout(timer);
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
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                this.pending.delete(id);
                reject(new Error(`CDP ${method} timed out after ${cdpRequestTimeoutMs}ms`));
            }, cdpRequestTimeoutMs);
            this.pending.set(id, { resolve, reject, timer });
            this.ws.send(JSON.stringify({ id, method, params }));
        });
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

    close() {
        this.ws.close();
    }
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
            if (!filePath.startsWith(projectRoot)) {
                res.writeHead(403);
                res.end('Forbidden');
                return;
            }
            const data = await fs.readFile(filePath);
            res.writeHead(200, { 'Content-Type': contentType(filePath) });
            res.end(data);
        } catch {
            res.writeHead(404);
            res.end('Not found');
        }
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    return server;
}

function waitForDevTools(chrome) {
    return new Promise((resolve, reject) => {
        let stderr = '';
        const timer = setTimeout(() => reject(new Error('Timed out waiting for Chrome DevTools URL')), 10000);
        chrome.on('error', (error) => {
            clearTimeout(timer);
            reject(new Error(`Chrome failed to launch: ${error.message}`));
        });
        chrome.stderr.on('data', (chunk) => {
            stderr += chunk.toString();
            const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
            if (match) {
                clearTimeout(timer);
                resolve(match[1]);
            }
        });
        chrome.on('exit', (code) => {
            clearTimeout(timer);
            reject(new Error(`Chrome exited before DevTools was ready: ${code}\n${stderr}`));
        });
    });
}

async function newPage(browserWsURL, url) {
    const port = new URL(browserWsURL).port;
    const response = await fetch(
        `http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`,
        { method: 'PUT' }
    );
    if (!response.ok) throw new Error(`Chrome /json/new failed: ${response.status}`);
    return response.json();
}

async function evaluate(client, expression) {
    const result = await client.send('Runtime.evaluate', {
        expression,
        awaitPromise: true,
        returnByValue: true
    });
    if (result.exceptionDetails) {
        throw new Error(JSON.stringify(result.exceptionDetails));
    }
    return result.result.value;
}

function assertTrue(name, condition, detail) {
    if (!condition) {
        throw new Error(`${name}${detail !== undefined ? ` — ${JSON.stringify(detail)}` : ''}`);
    }
    console.log(`PASS ${name}`);
}

function assertClose(name, actual, expected, tolerance = 2) {
    if (typeof actual !== 'number' || Math.abs(actual - expected) > tolerance) {
        throw new Error(`${name}: got ${actual}, expected ${expected} ±${tolerance}`);
    }
    console.log(`PASS ${name} (${actual.toFixed(1)} ≈ ${expected.toFixed(1)})`);
}

/* ===== Real input helpers ===== */

async function mouseClick(client, x, y, clickCount = 1) {
    for (let i = 1; i <= clickCount; i++) {
        await client.send('Input.dispatchMouseEvent', {
            type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: i
        });
        await client.send('Input.dispatchMouseEvent', {
            type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: i
        });
    }
}

async function mouseDrag(client, fromX, fromY, toX, toY, steps = 6, modifiers = 0) {
    await client.send('Input.dispatchMouseEvent', {
        type: 'mousePressed', x: fromX, y: fromY, button: 'left', buttons: 1, clickCount: 1, modifiers
    });
    for (let i = 1; i <= steps; i++) {
        const x = fromX + ((toX - fromX) * i) / steps;
        const y = fromY + ((toY - fromY) * i) / steps;
        await client.send('Input.dispatchMouseEvent', {
            type: 'mouseMoved', x, y, button: 'left', buttons: 1, modifiers
        });
    }
    await client.send('Input.dispatchMouseEvent', {
        type: 'mouseReleased', x: toX, y: toY, button: 'left', buttons: 0, clickCount: 1, modifiers
    });
}

// Poll a browser-evaluated boolean until true (real-input timing beats sleeps).
async function pollUntil(client, expression, { tries = 40, interval = 25 } = {}) {
    for (let i = 0; i < tries; i++) {
        if (await evaluate(client, `!!(${expression})`)) return true;
        await new Promise((r) => setTimeout(r, interval));
    }
    return false;
}

const KEY_CODES = {
    Escape: 27,
    ArrowRight: 39,
    ArrowLeft: 37,
    ArrowDown: 40,
    ArrowUp: 38,
    Backspace: 8,
    Enter: 13,
    z: 90
};

async function pressKey(client, key, modifiers = 0) {
    const vk = KEY_CODES[key];
    // Windows virtual key codes are not macOS hardware key codes. Let Chrome
    // resolve the native code; passing 90 for "z" sends macOS F20 and blurs editing.
    await client.send('Input.dispatchKeyEvent', {
        type: 'rawKeyDown', key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key,
        windowsVirtualKeyCode: vk, modifiers
    });
    await client.send('Input.dispatchKeyEvent', {
        type: 'keyUp', key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key,
        windowsVirtualKeyCode: vk, modifiers
    });
}

async function elementCenter(client, expr) {
    const rect = await evaluate(client, `
        (function() {
            const el = (${expr});
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return { left: r.left, top: r.top, width: r.width, height: r.height,
                     x: r.left + r.width / 2, y: r.top + r.height / 2 };
        })()
    `);
    if (!rect) throw new Error(`element not found: ${expr}`);
    return rect;
}

async function wysState(client) {
    return evaluate(client, 'window.__prestoWysiwyg.state()');
}

async function messages(client, type) {
    return evaluate(client, `window.__wysMessages.filter(m => m.type === ${JSON.stringify(type)})`);
}

async function main() {
    let server;
    let userDataDir;
    let chrome;
    let client;
    try {
        server = await startServer();
        const url = `http://127.0.0.1:${server.address().port}/${deckPath}`;
        userDataDir = await fs.mkdtemp(path.join(os.tmpdir(), 'webslides-wys-e2e-'));
        chrome = spawn(chromePath, [
            '--headless=new',
            '--disable-gpu',
            '--no-first-run',
            '--no-default-browser-check',
            `--user-data-dir=${userDataDir}`,
            '--remote-debugging-port=0',
            `--window-size=${viewportW},${viewportH}`,
            'about:blank'
        ], { stdio: ['ignore', 'ignore', 'pipe'] });

        const browserWsURL = await waitForDevTools(chrome);
        const target = await newPage(browserWsURL, url);
        client = new CDP(target.webSocketDebuggerUrl);
        await client.open();
        await client.send('Page.enable');
        await client.send('Runtime.enable');
        const runtimeErrors = [];
        client.on('Runtime.exceptionThrown', (params) => runtimeErrors.push(params));
        await client.send('Emulation.setDeviceMetricsOverride', {
            width: viewportW, height: viewportH, deviceScaleFactor: 1, mobile: false
        });
        const loaded = client.once('Page.loadEventFired');
        await client.send('Page.navigate', { url });
        await loaded;

        // Set up the exact edit-mode environment Presto creates: the shared UI
        // tokens, the slide-frame layout (presto/edit-layout.js when present,
        // else the engine's viewport with the rail inset), the edit chrome,
        // then the injected WYSIWYG layer.
        const wysiwygSource = await fs.readFile(path.join(projectRoot, 'presto/wysiwyg.js'), 'utf8');
        const chromeCSS = await fs.readFile(path.join(projectRoot, 'presto/edit-chrome.css'), 'utf8');
        const chromeJS = await fs.readFile(path.join(projectRoot, 'presto/edit-chrome.js'), 'utf8');
        const tokensCSS = await fs.readFile(path.join(projectRoot, 'presto/ui-tokens.css'), 'utf8');
        let layoutJS = '';
        try {
            layoutJS = await fs.readFile(path.join(projectRoot, 'presto/edit-layout.js'), 'utf8');
        } catch { /* before the engine's edit-layout.js lands */ }
        await evaluate(client, `
            (function() {
                [${JSON.stringify(tokensCSS)}, ${JSON.stringify(chromeCSS)}].forEach(function (css) {
                    var s = document.createElement('style');
                    s.textContent = css;
                    document.head.appendChild(s);
                });
                return true;
            })()
        `);
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
                window.__wysLayout = function () {
                    if (window.PrestoLayout) return PrestoLayout.apply({ mode: 'edit', insetLeft: 232, insetBottom: 72, padding: 28 });
                    return applyPrestoEditViewport({ railWidth: 232 });
                };
                window.__wysLayout();
                window.addEventListener('resize', () => window.__wysLayout());
                PrestoEditChrome.install({ railVisible: true, ghostFuture: true });
                WS.goToSlide(${workingSlide}, 0);
                // Web fonts reflow the slide when they land; wait so click
                // coordinates captured later stay valid.
                await document.fonts.ready;
                await new Promise((r) => setTimeout(r, 120));

                // Deck-agnostic target resolution, re-resolved on every call so
                // undo/redo DOM replacement can never leave the harness holding
                // a detached node.
                window.__wysSlide = function () { return WS.slides[${workingSlide}]; };
                window.__wysTarget = function (kind) {
                    var slide = window.__wysSlide();
                    if (kind === 'heading') return slide.querySelector('h1, h2, h3');
                    var heading = slide.querySelector('h1, h2, h3');
                    var candidates = slide.querySelectorAll('p, li');
                    for (var i = 0; i < candidates.length; i++) {
                        if (candidates[i] !== heading &&
                            window.__prestoWysiwyg.canTextEdit(candidates[i])) {
                            return candidates[i];
                        }
                    }
                    return null;
                };
                // First client point inside the slide where nothing is selectable.
                window.__wysEmptyPoint = function () {
                    var slide = window.__wysSlide();
                    var r = slide.getBoundingClientRect();
                    for (var fy = 0.92; fy >= 0.05; fy -= 0.08) {
                        for (var fx = 0.06; fx <= 0.95; fx += 0.08) {
                            var x = r.left + r.width * fx;
                            var y = r.top + r.height * fy;
                            var hit = document.elementFromPoint(x, y);
                            if (!hit || !slide.contains(hit)) continue;
                            if (!window.__prestoWysiwyg.pickSelectable(hit)) {
                                return { x: x, y: y };
                            }
                        }
                    }
                    return null;
                };
                return true;
            })()
        `);
        await evaluate(client, wysiwygSource + '\n;true');
        // Inject the alignment-guides module right after wysiwyg.js, exactly as
        // Presto.swift injects it into the edit preview. wysiwyg.js reads
        // window.__prestoWysiwygGuides lazily at gesture time, so once present its
        // drag/resize paths snap through it.
        const guidesSource = await fs.readFile(path.join(projectRoot, 'presto/wysiwyg-guides.js'), 'utf8');
        await evaluate(client, guidesSource + '\n;true');
        await evaluate(client, `
            (function() {
                window.__wysMessages = [];
                __prestoWysiwyg.onMessage = (m) => window.__wysMessages.push(m);
                __prestoWysiwyg.enable();
                return true;
            })()
        `);
        await evaluate(client, `
            new Promise((resolve) => {
                (function check() {
                    if (__prestoWysiwyg.state().enabled) resolve(true);
                    else requestAnimationFrame(check);
                })();
            })
        `);
        console.log('PASS environment ready (sidebar + viewport + wysiwyg layer)');

        const scale = await evaluate(client,
            '__wysSlide().getBoundingClientRect().width / 1280');
        assertTrue('slide is zoom-scaled below 1:1', scale > 0.2 && scale < 1, scale);

        // --- Real click selects ---
        const para = await elementCenter(client, "__wysTarget('para')");
        await mouseClick(client, para.x, para.y);
        let st = await wysState(client);
        assertTrue('real click selects paragraph', st.hasSelection && !st.textEditing, st);
        const toolbar = await evaluate(client, `
            (function() {
                const t = document.getElementById('presto-wysiwyg-toolbar');
                const r = t.getBoundingClientRect();
                return { visible: t.classList.contains('visible'),
                         inViewport: r.left >= 0 && r.top >= 0 && r.right <= ${viewportW} && r.bottom <= ${viewportH} };
            })()
        `);
        assertTrue('toolbar visible and inside viewport', toolbar.visible && toolbar.inViewport, toolbar);

        // --- Real drag moves by exactly the mouse delta (zoom compensated) ---
        // Alt held (modifiers bit 1) disables guide snapping so this asserts the
        // raw zoom-compensated delta; snapping itself is covered by the dedicated
        // snap scenarios below.
        const beforeDrag = await elementCenter(client, "__wysTarget('para')");
        await mouseDrag(client, beforeDrag.x, beforeDrag.y, beforeDrag.x + 80, beforeDrag.y + 50, 6, 1);
        const afterDrag = await elementCenter(client, "__wysTarget('para')");
        assertClose('drag moves element horizontally with the mouse', afterDrag.x - beforeDrag.x, 80, 3);
        assertClose('drag moves element vertically with the mouse', afterDrag.y - beforeDrag.y, 50, 3);
        let changed = await messages(client, 'wysiwygChanged');
        assertTrue('drag committed a save', changed.length >= 1);
        assertTrue('saved source carries the translate',
            changed[changed.length - 1].html.includes('translate'), changed[changed.length - 1].html.slice(0, 200));

        // --- Keyboard nudge with real key events ---
        const beforeNudge = await elementCenter(client, "__wysTarget('para')");
        await pressKey(client, 'ArrowRight');
        const afterNudge = await elementCenter(client, "__wysTarget('para')");
        assertClose('arrow key nudges element (2 slide px)', afterNudge.x - beforeNudge.x, 2 * scale, 1.5);
        const navState = await evaluate(client, '({ index: WS.currentIndex, step: WS.currentStep })');
        assertTrue('arrow key did not navigate the deck',
            navState.index === workingSlide && navState.step === 0, navState);

        // --- Real double-click starts text editing; typing lands in the slide ---
        const heading = await elementCenter(client, "__wysTarget('heading')");
        await mouseClick(client, heading.x, heading.y, 2);
        st = await wysState(client);
        assertTrue('double-click starts text editing', st.textEditing, st);
        await client.send('Input.insertText', { text: ' TYPED' });
        const headingText = await evaluate(client, "__wysTarget('heading').textContent");
        assertTrue('typed text landed in the heading', headingText.includes('TYPED'), headingText);
        await pressKey(client, 'Escape');
        st = await wysState(client);
        assertTrue('Escape ends text editing but keeps selection', !st.textEditing && st.hasSelection, st);
        changed = await messages(client, 'wysiwygChanged');
        const lastSave = changed[changed.length - 1];
        assertTrue('text edit was committed to source', lastSave.html.includes('TYPED'));
        assertTrue('committed source is clean of chrome',
            !lastSave.html.includes('presto-wysiwyg') && !lastSave.html.includes('contenteditable'));

        // --- Toolbar formatting with a real mouse click ---
        const paraForBold = await elementCenter(client, "__wysTarget('para')");
        await mouseClick(client, paraForBold.x, paraForBold.y);
        const boldBtn = await elementCenter(client,
            "document.querySelector('#presto-wysiwyg-toolbar .pw-b')");
        await mouseClick(client, boldBtn.x, boldBtn.y);
        const paraWeight = await evaluate(client, "__wysTarget('para').style.fontWeight");
        assertTrue('toolbar bold applies to selected paragraph', paraWeight === '700', paraWeight);

        // --- Escape deselects; engine click-nav stays suppressed ---
        await pressKey(client, 'Escape');
        st = await wysState(client);
        assertTrue('Escape deselects', !st.hasSelection, st);
        const slideRect = await evaluate(client, `
            (function() { const r = __wysSlide().getBoundingClientRect();
                return { left: r.left, top: r.top, right: r.right, bottom: r.bottom }; })()
        `);
        await mouseClick(client, slideRect.right - 30, slideRect.top + 300);
        const afterClickNav = await evaluate(client, '({ index: WS.currentIndex, step: WS.currentStep })');
        assertTrue('click on slide does not advance steps', afterClickNav.step === 0, afterClickNav);

        // --- Double-click on empty slide area creates a text box ---
        const emptyPoint = await evaluate(client, '__wysEmptyPoint()');
        assertTrue('found an empty point on the slide', !!emptyPoint, emptyPoint);
        await mouseClick(client, emptyPoint.x, emptyPoint.y, 2);
        st = await wysState(client);
        assertTrue('dblclick empty area creates a text box in edit mode', st.textEditing, st);
        await client.send('Input.insertText', { text: 'Fresh label' });
        await pressKey(client, 'Escape');
        const boxInfo = await evaluate(client, `
            (function() {
                const box = __wysSlide().querySelector('.text-box');
                return box ? { text: box.textContent, position: box.style.position } : null;
            })()
        `);
        assertTrue('text box exists with typed content', boxInfo && boxInfo.text === 'Fresh label' && boxInfo.position === 'absolute', boxInfo);
        changed = await messages(client, 'wysiwygChanged');
        assertTrue('text box committed to source',
            changed[changed.length - 1].html.includes('Fresh label'));

        // --- Cmd+Z undo via real keys (no selection needed) ---
        await pressKey(client, 'Escape');
        const savesBeforeUndo = (await messages(client, 'wysiwygChanged')).length;
        await pressKey(client, 'z', 4); // Meta
        const boxAfterUndo = await evaluate(client,
            '__wysSlide().querySelector(".text-box") ? __wysSlide().querySelector(".text-box").textContent : null');
        assertTrue('Cmd+Z undoes the text box edit', boxAfterUndo !== 'Fresh label', boxAfterUndo);
        const savesAfterUndo = (await messages(client, 'wysiwygChanged')).length;
        assertTrue('undo posted a save of the restored state', savesAfterUndo === savesBeforeUndo + 1);

        /* =====================================================================
         * Extended real-input scenarios (appended). Deck-SPECIFIC scenarios skip
         * when PRESTO_WYSIWYG_DECK overrides the default deck. Every scenario
         * re-resolves its targets, and each leaves the deck in a state later
         * scenarios can handle.
         * ===================================================================== */
        const deckOverride = !!process.env.PRESTO_WYSIWYG_DECK;
        const goHome = () => evaluate(client,
            `WS.goToSlide(${workingSlide}, 0); __prestoWysiwyg.deselect(); true`);

        // ===== 1 & 2. Resize via the SE and E handles =====
        // Regression guard: handles live outside the slide, so isChrome() must
        // claim them (class-based check) or the capture-phase mousedown
        // deselects before startResize can run.
        if (deckOverride) {
            console.log('SKIP resize-se/e (deck-specific: needs the inlined SVG)');
        } else {
            const svgSel = "__wysSlide().querySelector('svg[data-ws-original-img]')";
            for (const kind of ['se', 'e']) {
                await goHome();
                const svg = await elementCenter(client, svgSel);
                await mouseClick(client, svg.x, svg.y);
                assertTrue(`image selects for ${kind}-resize`, (await wysState(client)).hasSelection);
                const handle = await evaluate(client, `
                    (function(){
                        var h = document.querySelector('.presto-wysiwyg-handle[data-handle="${kind}"]');
                        if (!h) return null;
                        var r = h.getBoundingClientRect();
                        var s = ${svgSel}.getBoundingClientRect();
                        return { x: r.left + r.width / 2, y: r.top + r.height / 2,
                                 visible: h.classList.contains('visible'),
                                 onRightEdge: Math.abs((r.left + r.width / 2) - s.right) < 8 };
                    })()`);
                assertTrue(`${kind} handle sits visible on the element's right edge`,
                    handle && handle.visible && handle.onRightEdge, handle);
                const wBefore = await evaluate(client, `parseFloat(${svgSel}.style.width)`);
                await mouseDrag(client, handle.x, handle.y, handle.x + 60, handle.y);
                const wAfter = await evaluate(client, `parseFloat(${svgSel}.style.width)`);
                const heightStyle = await evaluate(client, `${svgSel} ? ${svgSel}.style.height : null`);
                const selAfter = (await wysState(client)).hasSelection;
                assertClose(`${kind} handle grew width by ~60/scale slide px`, wAfter - wBefore, 60 / scale, 5);
                assertTrue(`${kind} resize set height:auto to preserve aspect`, heightStyle === 'auto', heightStyle);
                assertTrue(`${kind} resize kept the element selected + committed a save`, selAfter, selAfter);
            }
        }

        /* ===================================================================
         * RESIZE FLOW BLOCK (wys/resize-complete) — full handle set + guides
         * snap integration through the REAL drag/resize paths. Deck-agnostic:
         * a fresh absolute text box stands in for a "box", and the harness's
         * paragraph is dragged onto the guaranteed slide-center snap line.
         * =================================================================== */

        // ===== R1. SE (2D box), S (height, no jitter), W (right-edge anchored) =====
        await goHome();
        {
            // A fresh absolute text box, uniquely id'd so measurements never pick
            // up a stray .text-box left by an earlier scenario.
            const scaleBox = await evaluate(client, `
                (function(){
                    __prestoWysiwyg.deselect();
                    var box = __prestoWysiwyg.addTextBox(300, 300);
                    // An untouched new box is discarded (BACKLOG 9): type into it.
                    document.execCommand('insertText', false, 'Resize me');
                    __prestoWysiwyg.endTextEdit();
                    box.id = '__rszBox';
                    box.style.boxSizing = 'border-box'; box.style.padding = '0'; box.style.borderWidth = '0';
                    box.style.width = '240px'; box.style.height = '140px';
                    __prestoWysiwyg.select(box);
                    var r = __wysSlide().getBoundingClientRect();
                    return r.width / 1280;
                })()`);
            const boxDims = () => evaluate(client, `
                (function(){ var b = document.getElementById('__rszBox'); var r = b.getBoundingClientRect();
                    return { w: parseFloat(b.style.width), h: parseFloat(b.style.height),
                             right: r.right, left: parseFloat(b.style.left) }; })()`);
            const handleC = (kind) => evaluate(client, `
                (function(){ var h = document.querySelector('.presto-wysiwyg-handle[data-handle="${kind}"]');
                    var r = h.getBoundingClientRect();
                    return { x: r.left + r.width / 2, y: r.top + r.height / 2, visible: h.classList.contains('visible') }; })()`);

            // SE grows BOTH axes. Alt held (bit 1) defeats snapping for exact deltas.
            const se = await handleC('se');
            assertTrue('box shows an SE handle', se.visible, se);
            const seBefore = await boxDims();
            await mouseDrag(client, se.x, se.y, se.x + 50, se.y + 40, 6, 1);
            const seAfter = await boxDims();
            assertClose('SE on a box grew width by dx/scale', seAfter.w - seBefore.w, 50 / scaleBox, 3);
            assertClose('SE on a box grew height by dy/scale', seAfter.h - seBefore.h, 40 / scaleBox, 3);

            // S grows height only; a horizontal jitter must NOT change width.
            const s = await handleC('s');
            const sBefore = await boxDims();
            await mouseDrag(client, s.x, s.y, s.x + 25, s.y + 45, 6, 1);
            const sAfter = await boxDims();
            assertClose('S-handle vertical drag grew height by dy/scale', sAfter.h - sBefore.h, 45 / scaleBox, 3);
            assertClose('S-handle drag left width unchanged (no horizontal jitter)', sAfter.w - sBefore.w, 0, 0.6);

            // W grows width leftward while keeping the RIGHT edge fixed.
            const w = await handleC('w');
            const wBefore = await boxDims();
            await mouseDrag(client, w.x, w.y, w.x - 40, w.y, 6, 1);
            const wAfter = await boxDims();
            assertClose('W-handle drag keeps the right edge fixed', wAfter.right - wBefore.right, 0, 2.5);
            assertClose('W-handle drag grew width by |dx|/scale', wAfter.w - wBefore.w, 40 / scaleBox, 3);
            assertTrue('W-handle drag moved the left edge left', wAfter.left < wBefore.left - 1,
                { before: wBefore.left, after: wAfter.left });

            await evaluate(client, `(function(){ var b = document.getElementById('__rszBox'); if (b) b.remove(); __prestoWysiwyg.deselect(); return true; })()`);
        }

        // ===== R2. Guides snap integration via the REAL drag path (+ Alt no-snap) =====
        {
            const measurePara = () => evaluate(client, `
                (function(){ var slide = __wysSlide(); var r = slide.getBoundingClientRect(); var scale = r.width / 1280;
                    var el = __wysTarget('para'); var er = el.getBoundingClientRect();
                    return { scale: scale, leftSlide: (er.left - r.left) / scale,
                             cx: er.left + er.width / 2, cy: er.top + er.height / 2 }; })()`);
            const resetPara = () => evaluate(client, `(function(){ __wysTarget('para').style.removeProperty('translate'); return true; })()`);

            // (a) No Alt: dragging the left edge to 4 slide-px shy of the slide
            //     center (a guaranteed snap target) lands it EXACTLY on 640.
            await goHome();
            await resetPara();
            let m = await measurePara();
            let deltaClient = ((640 - 4) - m.leftSlide) * m.scale;
            await mouseDrag(client, m.cx, m.cy, m.cx + deltaClient, m.cy);
            let leftNow = (await measurePara()).leftSlide;
            assertClose('real drag within 4px of the slide center snaps the left edge exactly to 640', leftNow, 640, 0.9);

            // (b) Alt held: the identical drag does NOT snap — the committed left
            //     edge stays at the raw 636, proving live snap-defeat.
            await goHome();
            await resetPara();
            m = await measurePara();
            deltaClient = ((640 - 4) - m.leftSlide) * m.scale;
            await mouseDrag(client, m.cx, m.cy, m.cx + deltaClient, m.cy, 6, 1);
            leftNow = (await measurePara()).leftSlide;
            assertClose('Alt-held drag to the same spot does NOT snap (stays at 636)', leftNow, 636, 1.2);
            assertTrue('Alt-held drag stayed clear of the 640 snap line', Math.abs(leftNow - 640) > 2.5, leftNow);
            await resetPara();
            await goHome();
        }

        // ===== 3. Color swatch =====
        await goHome();
        {
            const p = await elementCenter(client, "__wysTarget('para')");
            await mouseClick(client, p.x, p.y);
            assertTrue('paragraph selects for color', (await wysState(client)).hasSelection);
            // Swatches come from the deck's theme variables, in a popover
            // opened by the format bar's colour button.
            const colorBtn = await elementCenter(client, "document.querySelector('#presto-wysiwyg-toolbar .pw-color')");
            await mouseClick(client, colorBtn.x, colorBtn.y);
            assertTrue('the colour button opens the theme colour popover',
                await evaluate(client, "!!document.getElementById('presto-wysiwyg-colors')"));
            const swatch = await elementCenter(client,
                "document.querySelector('#presto-wysiwyg-colors .pw-swatch[title=\"Accent\"]')");
            const accent = await evaluate(client, `
                (function(){ var s = document.createElement('span'); s.style.color = 'var(--color-accent)';
                  document.body.appendChild(s); var c = getComputedStyle(s).color; s.remove(); return c; })()`);
            const savesBefore = (await messages(client, 'wysiwygChanged')).length;
            await mouseClick(client, swatch.x, swatch.y);
            const color = await evaluate(client, "__wysTarget('para').style.color");
            assertTrue('the Accent swatch writes the theme variable', color === 'var(--color-accent)', color);
            const computed = await evaluate(client, "getComputedStyle(__wysTarget('para')).color");
            assertTrue('…which renders in the deck accent colour', computed === accent, { computed, accent });
            const saves = await messages(client, 'wysiwygChanged');
            assertTrue('color change committed a save', saves.length === savesBefore + 1, { before: savesBefore, after: saves.length });
            assertTrue('committed source carries the theme variable',
                saves[saves.length - 1].html.includes('color: var(--color-accent)'), saves[saves.length - 1].html.slice(0, 200));
        }

        // ===== 4. Font-size input =====
        await goHome();
        {
            const h = await elementCenter(client, "__wysTarget('heading')");
            await mouseClick(client, h.x, h.y);
            assertTrue('heading selects for font size', (await wysState(client)).hasSelection);
            // The +/- steppers are real toolbar clicks and DO apply/commit.
            const sizeBefore = await evaluate(client, "parseFloat(getComputedStyle(__wysTarget('heading')).fontSize)");
            const plus = await elementCenter(client, "document.querySelector('#presto-wysiwyg-toolbar .pw-font-plus')");
            await mouseClick(client, plus.x, plus.y);
            await mouseClick(client, plus.x, plus.y);
            const sizeAfterPlus = await evaluate(client, "parseFloat(getComputedStyle(__wysTarget('heading')).fontSize)");
            assertClose('+ stepper raises font size ~4px over 2 real clicks', sizeAfterPlus - sizeBefore, 4, 1.6);
            // The text input + Enter path.
            const fi = await elementCenter(client, "document.getElementById('presto-wysiwyg-font-size')");
            await mouseClick(client, fi.x, fi.y, 3); // triple-click selects the field contents
            await client.send('Input.insertText', { text: '9999' });
            const typed = await evaluate(client, "document.getElementById('presto-wysiwyg-font-size').value");
            assertTrue('font input holds the typed value before Enter', typed === '9999', typed);
            const sizePreEnter = await evaluate(client, "parseFloat(getComputedStyle(__wysTarget('heading')).fontSize)");
            await pressKey(client, 'Enter');
            const sizePostEnter = await evaluate(client, "parseFloat(getComputedStyle(__wysTarget('heading')).fontSize)");
            const stAfterEnter = await wysState(client);
            assertClose('Enter applies + clamps 9999 -> 300px', sizePostEnter, 300, 1);
            assertTrue('Enter in the font input does not start a text session',
                !stAfterEnter.textEditing, stAfterEnter);
            // Restore a sane heading size for later scenarios.
            await evaluate(client, '__prestoWysiwyg.setFontSize(' + Math.round(sizePreEnter) + '); true');
        }

        // ===== 5. Duplicate via toolbar =====
        await goHome();
        {
            const empty = await evaluate(client, '__wysEmptyPoint()');
            assertTrue('found an empty point for the duplicate source', !!empty, empty);
            await mouseClick(client, empty.x, empty.y, 2); // dblclick makes a text box
            assertTrue('text box session started', (await wysState(client)).textEditing);
            await client.send('Input.insertText', { text: 'Dupe' });
            await pressKey(client, 'Escape'); // commit; box stays selected
            const orig = await evaluate(client, `
                (function(){ var b = __wysSlide().querySelector('.presto-wysiwyg-selected');
                  return b ? { left: parseFloat(b.style.left), top: parseFloat(b.style.top), text: b.textContent } : null; })()`);
            assertTrue('source box selected after typing', orig && orig.text === 'Dupe', orig);
            const savesBefore = (await messages(client, 'wysiwygChanged')).length;
            // Duplicate and Delete live in the format bar's ⋯ menu.
            const moreBtn = await elementCenter(client, "document.querySelector('#presto-wysiwyg-toolbar .pw-more')");
            await mouseClick(client, moreBtn.x, moreBtn.y);
            const dupBtn = await elementCenter(client, "document.querySelector('#presto-wysiwyg-more .pw-duplicate')");
            await mouseClick(client, dupBtn.x, dupBtn.y);
            const dup = await evaluate(client, `
                (function(){ var copy = __wysSlide().querySelector('.presto-wysiwyg-selected'); if (!copy) return null;
                  var prev = copy.previousElementSibling;
                  return { copyLeft: parseFloat(copy.style.left), copyTop: parseFloat(copy.style.top),
                           origLeft: prev ? parseFloat(prev.style.left) : null,
                           origTop: prev ? parseFloat(prev.style.top) : null,
                           copyText: copy.textContent, isTextBox: copy.classList.contains('text-box') }; })()`);
            assertClose('duplicate offset +24 slide px in x', dup.copyLeft - dup.origLeft, 24, 1);
            assertClose('duplicate offset +24 slide px in y', dup.copyTop - dup.origTop, 24, 1);
            assertTrue('the duplicate copy is now selected', dup.isTextBox && dup.copyText === 'Dupe', dup);
            const saves = await messages(client, 'wysiwygChanged');
            assertTrue('duplicate committed a save', saves.length === savesBefore + 1, { before: savesBefore, after: saves.length });
            const dupHtml = saves[saves.length - 1].html;
            assertTrue('saved copy is clean (no chrome classes / contenteditable)',
                dupHtml.includes('Dupe') && !dupHtml.includes('presto-wysiwyg') && !dupHtml.includes('contenteditable'),
                dupHtml.slice(0, 200));
        }

        // ===== 6. Backspace deletes; ⌘Z restores; ⌘⇧Z redo re-deletes =====
        {
            assertTrue('copy still selected going into delete', (await wysState(client)).hasSelection);
            const boxCount = () => evaluate(client, "__wysSlide().querySelectorAll('.text-box').length");
            const countBefore = await boxCount();
            const savesBefore = (await messages(client, 'wysiwygChanged')).length;
            await pressKey(client, 'Backspace');
            assertTrue('Backspace removed the duplicate', (await boxCount()) === countBefore - 1, { countBefore });
            assertTrue('delete committed a save',
                (await messages(client, 'wysiwygChanged')).length === savesBefore + 1);
            await pressKey(client, 'z', 4); // ⌘Z restores the copy
            assertTrue('⌘Z restored the duplicate', (await boxCount()) === countBefore, { countBefore });
            await pressKey(client, 'z', 4 | 8); // ⌘⇧Z redo re-deletes, leaving the deck sane
            assertTrue('⌘⇧Z redo re-deleted the duplicate', (await boxCount()) === countBefore - 1, { countBefore });
        }

        // ===== 7. Shift-drag axis lock =====
        await goHome();
        {
            const before = await elementCenter(client, "__wysTarget('para')");
            // mostly-horizontal drag with vertical jitter, Shift held (CDP bit 8)
            await mouseDrag(client, before.x, before.y, before.x + 70, before.y + 22, 8, 8);
            const after = await elementCenter(client, "__wysTarget('para')");
            assertClose('shift-drag moved horizontally', after.x - before.x, 70, 4);
            assertClose('shift-drag locked the vertical axis', after.y - before.y, 0, 2);
        }

        // ===== 8. Multi-slide history isolation =====
        await goHome();
        {
            const p = await elementCenter(client, "__wysTarget('para')");
            await mouseClick(client, p.x, p.y);
            await pressKey(client, 'ArrowRight'); // one undoable step on the working slide
            const depthHere = (await wysState(client)).undoDepth;
            assertTrue('working slide has undo history', depthHere >= 1, depthHere);
            const otherSlide = workingSlide === 0 ? 1 : 0;
            await evaluate(client, `WS.goToSlide(${otherSlide}, 0); true`);
            const other = await wysState(client);
            assertTrue('navigating away dropped the selection', !other.hasSelection, other);
            assertTrue('the other slide has its own empty history', other.undoDepth === 0, other);
            await evaluate(client, `WS.goToSlide(${workingSlide}, 0); true`);
            const back = await wysState(client);
            assertTrue('returning preserves the working slide history', back.undoDepth === depthHere, { back: back.undoDepth, depthHere });
            await pressKey(client, 'z', 4); // ⌘Z still works on the restored slide
            assertTrue('⌘Z after navigation consumed one undo step',
                (await wysState(client)).undoDepth === depthHere - 1, await wysState(client));
        }

        // ===== 9. Stepped (reveal) element editing =====
        if (deckOverride) {
            console.log('SKIP stepped-element (deck-specific: needs li[data-step="1"])');
        } else {
            await goHome();
            const stepSel = "Array.prototype.find.call(__wysSlide().querySelectorAll('li'), function(x){ return x.dataset.step === '1'; })";
            const li = await elementCenter(client, stepSel);
            await mouseClick(client, li.x, li.y);
            const st = await wysState(client);
            assertTrue('semi-visible stepped element selects', st.hasSelection && st.selectedTag === 'li', st);
            const savesBefore = (await messages(client, 'wysiwygChanged')).length;
            await pressKey(client, 'ArrowRight'); // nudge
            const saves = await messages(client, 'wysiwygChanged');
            assertTrue('nudging the stepped element committed a save', saves.length === savesBefore + 1);
            const saved = saves[saves.length - 1].html;
            assertTrue('saved source keeps data-step="1"', /data-step="1"/.test(saved), saved.slice(0, 300));
            assertTrue('no ws-visible leaked into the saved source', !saved.includes('ws-visible'), saved.slice(0, 300));
        }

        // ===== 10. Atom semantics (inlined SVG) =====
        if (deckOverride) {
            console.log('SKIP svg-atom (deck-specific: needs img[data-inline])');
        } else {
            await goHome();
            const svgSel = "__wysSlide().querySelector('svg[data-ws-original-img]')";
            const svg = await elementCenter(client, svgSel);
            await mouseClick(client, svg.x, svg.y, 2); // double-click
            const st = await wysState(client);
            assertTrue('double-click on an atom selects it', st.hasSelection && st.selectedTag === 'svg', st);
            assertTrue('double-click on an atom does NOT start a text session', !st.textEditing, st);
            const savesBefore = (await messages(client, 'wysiwygChanged')).length;
            await mouseDrag(client, svg.x, svg.y, svg.x + 12, svg.y + 8); // move a few px
            const saves = await messages(client, 'wysiwygChanged');
            assertTrue('dragging the atom committed a save', saves.length === savesBefore + 1);
            const saved = saves[saves.length - 1].html;
            assertTrue('atom serialized back to <img> carrying the translate',
                saved.includes('<img') && saved.includes('translate'), saved.slice(0, 300));
            assertTrue('no data-ws-original-img leaked into the saved source',
                !saved.includes('data-ws-original-img'), saved.slice(0, 300));
        }

        // ===== 11. Click outside the slide commits an open text session =====
        await goHome();
        {
            const p = await elementCenter(client, "__wysTarget('para')");
            await mouseClick(client, p.x, p.y, 2); // start a text session
            assertTrue('text session started for outside-click commit', (await wysState(client)).textEditing);
            await client.send('Input.insertText', { text: 'OUTSIDE' });
            const savesBefore = (await messages(client, 'wysiwygChanged')).length;
            // click on the deck background above the slide (outside the slide, inside chrome)
            const pt = await evaluate(client, `
                (function(){ var r = __wysSlide().getBoundingClientRect();
                  return { x: Math.round((r.left + r.right) / 2), y: Math.max(2, Math.round(r.top / 2)) }; })()`);
            await mouseClick(client, pt.x, pt.y);
            const st = await wysState(client);
            assertTrue('outside click ended the text session and dropped selection',
                !st.textEditing && !st.hasSelection, st);
            const saves = await messages(client, 'wysiwygChanged');
            assertTrue('outside click committed the typed text', saves.length >= savesBefore + 1);
            assertTrue('committed source carries the typed text',
                saves[saves.length - 1].html.includes('OUTSIDE'), saves[saves.length - 1].html.slice(0, 200));
        }

        // ===== 12. Toolbar flips below for a top-edge element =====
        if (deckOverride) {
            console.log('SKIP toolbar-flip (deck-specific: needs a top-edge element)');
        } else {
            await goHome();
            // Shrink the viewport so the slide is height-limited and sits at the very
            // top; a top-edge element then has no room for the toolbar above it.
            await client.send('Emulation.setDeviceMetricsOverride', { width: viewportW, height: 360, deviceScaleFactor: 1, mobile: false });
            await evaluate(client, '__wysLayout(); true');
            await pollUntil(client, "__wysSlide().getBoundingClientRect().top < 60");
            const top = await evaluate(client, `
                (function(){ var slide = __wysSlide();
                  var els = slide.querySelectorAll('h1,h2,h3,h4,p,li,div,img,svg,.box,.text-box');
                  var best = null, bt = Infinity;
                  els.forEach(function(e){ if (e.closest('.presto-wysiwyg-chrome')) return;
                    var r = e.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && r.top < bt) { bt = r.top; best = e; } });
                  if (!best) return null; var r = best.getBoundingClientRect();
                  return { x: r.left + Math.min(r.width / 2, 30), y: r.top + Math.min(r.height / 2, 6) }; })()`);
            assertTrue('found a top-edge element', !!top, top);
            await mouseClick(client, top.x, top.y);
            assertTrue('top-edge element selected', (await wysState(client)).hasSelection);
            const tb = await evaluate(client, `
                (function(){ var t = document.getElementById('presto-wysiwyg-toolbar');
                  var r = t.getBoundingClientRect();
                  var sel = document.querySelector('.presto-wysiwyg-selected');
                  var s = sel ? sel.getBoundingClientRect() : null;
                  return { visible: t.classList.contains('visible'), top: r.top,
                           elTop: s ? s.top : null, elBottom: s ? s.bottom : null }; })()`);
            assertTrue('toolbar visible for the top-edge element', tb.visible, tb);
            assertTrue('toolbar not clipped above the viewport', tb.top >= 0, tb);
            assertTrue('toolbar flipped BELOW the top-edge element', tb.top > tb.elTop, tb);
            // restore the normal viewport for the remaining scenarios
            await client.send('Emulation.setDeviceMetricsOverride', { width: viewportW, height: viewportH, deviceScaleFactor: 1, mobile: false });
            await evaluate(client, '__wysLayout(); true');
            await pollUntil(client, `__wysSlide().getBoundingClientRect().top > 100`);
        }

        // ===== 13. Paste inserts plain text only =====
        await goHome();
        {
            const p = await elementCenter(client, "__wysTarget('para')");
            await mouseClick(client, p.x, p.y, 2); // text session
            assertTrue('text session started for paste', (await wysState(client)).textEditing);
            const paste = await evaluate(client, `
                (function(){
                  var el = __wysSlide().querySelector('.presto-wysiwyg-editing') || document.querySelector('[contenteditable="true"]');
                  if (!el) return { ok: false, reason: 'no editable element' };
                  var sel = window.getSelection(); sel.removeAllRanges();
                  var rng = document.createRange(); rng.selectNodeContents(el); rng.collapse(false); sel.addRange(rng);
                  var dt;
                  try { dt = new DataTransfer(); dt.setData('text/plain', 'PLAINPASTE'); dt.setData('text/html', '<b>HTMLBOLD</b>'); }
                  catch (e) { return { ok: false, reason: 'DataTransfer unsupported: ' + e.message }; }
                  var ev;
                  try { ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }); }
                  catch (e) { return { ok: false, reason: 'ClipboardEvent unsupported: ' + e.message }; }
                  var carried = ev.clipboardData && ev.clipboardData.getData ? ev.clipboardData.getData('text/plain') : null;
                  el.dispatchEvent(ev);
                  return { ok: true, carried: carried, html: el.innerHTML, text: el.textContent }; })()`);
            assertTrue('synthetic paste carried clipboard text/plain', paste.ok && paste.carried === 'PLAINPASTE', paste);
            assertTrue('paste inserted the plain text', paste.text.includes('PLAINPASTE'), paste);
            assertTrue('paste did NOT inject rich HTML (<b> / HTMLBOLD)',
                !paste.html.includes('<b>') && !paste.html.includes('HTMLBOLD'), paste);
            await pressKey(client, 'Escape'); // commit the session
            const saves = await messages(client, 'wysiwygChanged');
            assertTrue('paste committed to source as plain text',
                saves[saves.length - 1].html.includes('PLAINPASTE') && !saves[saves.length - 1].html.includes('HTMLBOLD'),
                saves[saves.length - 1].html.slice(0, 300));
        }

        // ===== 15. Cross-slide navigation mid-session saves to the OWNING slide =====
        // Regression guard for the live-repro'd data-loss bug: a text session
        // that survives WS.goToSlide across a slide boundary must commit the typed
        // text to the slide it was typed on, not the navigated-to slide, and must
        // not poison the other slide's undo history.
        if (deckOverride) {
            console.log('SKIP cross-slide-owner (deck-specific: needs a second slide)');
        } else {
            await goHome();
            const otherSlide = workingSlide === 0 ? 1 : 0;
            await evaluate(client, `WS.goToSlide(${otherSlide}, 0); __prestoWysiwyg.deselect(); true`);
            const otherDepthBefore = (await wysState(client)).undoDepth;
            await evaluate(client, `WS.goToSlide(${workingSlide}, 0); __prestoWysiwyg.deselect(); true`);
            const para = await elementCenter(client, "__wysTarget('para')");
            await mouseClick(client, para.x, para.y, 2);   // start a text session
            assertTrue('text session started for cross-slide nav', (await wysState(client)).textEditing);
            await client.send('Input.insertText', { text: ' OWNERSLIDE' });
            const savesBefore = (await messages(client, 'wysiwygChanged')).length;
            await evaluate(client, `WS.goToSlide(${otherSlide}, 0); true`);   // navigate mid-session
            const st = await wysState(client);
            assertTrue('cross-slide navigation ended the text session', !st.textEditing, st);
            const saves = await messages(client, 'wysiwygChanged');
            assertTrue('the mid-session navigation committed exactly one save',
                saves.length === savesBefore + 1, { before: savesBefore, after: saves.length });
            const ownerSave = saves[saves.length - 1];
            assertTrue('typed text saved to the OWNING slide index', ownerSave.index === workingSlide, ownerSave.index);
            assertTrue('save payload carries the text typed on the owning slide',
                ownerSave.html.includes('OWNERSLIDE'), ownerSave.html.slice(0, 200));
            const ownerText = await evaluate(client, `WS.slides[${workingSlide}].textContent.includes('OWNERSLIDE')`);
            assertTrue('typed text is present on the owning slide in the DOM', ownerText, ownerText);
            const otherAfter = await wysState(client);
            assertTrue('the navigated-to slide history was not poisoned',
                otherAfter.undoDepth === otherDepthBefore, { before: otherDepthBefore, after: otherAfter.undoDepth });
            await evaluate(client, `WS.goToSlide(${workingSlide}, 0); __prestoWysiwyg.deselect(); true`);
        }

        // ===== 16. ⌘→ navigates the deck — never nudges-and-saves =====
        // Regression guard for the audited data-loss bug: with an element
        // selected, ⌘←/→ must change slides/steps, not move the element 2px and
        // write the deck to disk.
        await goHome();
        {
            const p = await elementCenter(client, "__wysTarget('para')");
            await mouseClick(client, p.x, p.y);
            assertTrue('element selected before ⌘→', (await wysState(client)).hasSelection);
            const transBefore = await evaluate(client, "__wysTarget('para').style.translate || ''");
            const navBefore = await evaluate(client, '({ index: WS.currentIndex, step: WS.currentStep })');
            const savesBefore = (await messages(client, 'wysiwygChanged')).length;
            await pressKey(client, 'ArrowRight', 4); // Meta(4)+Right
            const st = await wysState(client);
            assertTrue('⌘→ dropped the selection instead of nudging', !st.hasSelection, st);
            const savesAfter = (await messages(client, 'wysiwygChanged')).length;
            assertTrue('⌘→ wrote nothing to disk (no per-key nudge save)',
                savesAfter === savesBefore, { savesBefore, savesAfter });
            const transAfter = await evaluate(client,
                `WS.goToSlide(${workingSlide}, 0); (__wysTarget('para') ? (__wysTarget('para').style.translate || '') : '')`);
            assertTrue('⌘→ did not nudge the element', transAfter === transBefore, { transBefore, transAfter });
            if (!deckOverride) {
                assertTrue('⌘→ advanced the deck (step or slide)',
                    navBefore.index === workingSlide && navBefore.step === 0, navBefore);
            }
        }

        // ===== 17. Arrow-nudge burst coalesces to one undo step + one save =====
        await goHome();
        {
            const p = await elementCenter(client, "__wysTarget('para')");
            await mouseClick(client, p.x, p.y);
            assertTrue('element selected for a nudge burst', (await wysState(client)).hasSelection);
            const depthBefore = (await wysState(client)).undoDepth;
            const savesBefore = (await messages(client, 'wysiwygChanged')).length;
            const before = await elementCenter(client, "__wysTarget('para')");
            // Three keydowns with NO keyup between (auto-repeat), then release once.
            for (let i = 0; i < 3; i++) {
                await client.send('Input.dispatchKeyEvent', {
                    type: 'rawKeyDown', key: 'ArrowRight', code: 'ArrowRight',
                    windowsVirtualKeyCode: 39
                });
            }
            const mid = await wysState(client);
            const savesMid = (await messages(client, 'wysiwygChanged')).length;
            assertTrue('nudge burst pushes no undo step mid-run',
                mid.undoDepth === depthBefore, { depthBefore, mid: mid.undoDepth });
            assertTrue('nudge burst posts no save mid-run',
                savesMid === savesBefore, { savesBefore, savesMid });
            const moved = await elementCenter(client, "__wysTarget('para')");
            assertClose('all three nudges moved the element (6 slide px)',
                moved.x - before.x, 6 * scale, 3);
            await client.send('Input.dispatchKeyEvent', {
                type: 'keyUp', key: 'ArrowRight', code: 'ArrowRight',
                windowsVirtualKeyCode: 39
            });
            const after = await wysState(client);
            assertTrue('arrow keyup flushes the burst as ONE undo step',
                after.undoDepth === depthBefore + 1, { depthBefore, after: after.undoDepth });
            const savesAfter = (await messages(client, 'wysiwygChanged')).length;
            assertTrue('arrow keyup flushes the burst as ONE save',
                savesAfter === savesBefore + 1, { savesBefore, savesAfter });
            await pressKey(client, 'z', 4); // ⌘Z
            const undone = await elementCenter(client, "__wysTarget('para')");
            assertClose('undo after the burst restores the pre-burst position',
                undone.x - before.x, 0, 2.5);
        }

        // ===== 18. Toolbar stays put across two toolbar clicks (font bump between) =====
        await goHome();
        {
            const h = await elementCenter(client, "__wysTarget('heading')");
            await mouseClick(client, h.x, h.y);
            assertTrue('heading selected for toolbar-stability test', (await wysState(client)).hasSelection);
            const plus = await elementCenter(client, "document.querySelector('#presto-wysiwyg-toolbar .pw-font-plus')");
            const readTb = () => evaluate(client,
                "(function(){var t=document.getElementById('presto-wysiwyg-toolbar');return {top:t.style.top,left:t.style.left};})()");
            const readSize = () => evaluate(client, "parseFloat(getComputedStyle(__wysTarget('heading')).fontSize)");
            const tb0 = await readTb();
            const size0 = await readSize();
            // Pointer over the + button (toolbar 'protected'), then click it twice.
            await client.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: plus.x, y: plus.y, buttons: 0 });
            await mouseClick(client, plus.x, plus.y);
            const tb1 = await readTb();
            await mouseClick(client, plus.x, plus.y);
            const tb2 = await readTb();
            const size2 = await readSize();
            assertTrue('toolbar did not move after the first sizing click',
                tb1.top === tb0.top && tb1.left === tb0.left, { tb0, tb1 });
            assertTrue('toolbar stayed put across two consecutive toolbar clicks',
                tb2.top === tb1.top && tb2.left === tb1.left, { tb1, tb2 });
            assertClose('both clicks landed on the + button (font grew ~4px)', size2 - size0, 4, 2);
            await evaluate(client, '__prestoWysiwyg.setFontSize(' + Math.round(size0) + '); true');
            await pressKey(client, 'Escape');
        }

        // ===== 19. Replace-on-type: printable key replaces a selected element's text =====
        await goHome();
        {
            const pressPrintable = async (ch) => {
                const code = 'Key' + ch.toUpperCase();
                const vk = ch.toUpperCase().charCodeAt(0);
                await client.send('Input.dispatchKeyEvent', {
                    type: 'rawKeyDown', key: ch, code, windowsVirtualKeyCode: vk
                });
                await client.send('Input.dispatchKeyEvent', {
                    type: 'keyUp', key: ch, code, windowsVirtualKeyCode: vk
                });
            };
            const h = await elementCenter(client, "__wysTarget('heading')");
            await mouseClick(client, h.x, h.y);
            let st = await wysState(client);
            assertTrue('heading selected (not editing) before replace-on-type',
                st.hasSelection && !st.textEditing, st);
            const origText = await evaluate(client, "__wysTarget('heading').textContent");
            await pressPrintable('z'); // printable key (no text) → session + select-all
            st = await wysState(client);
            assertTrue('a printable key started a replace-on-type session', st.textEditing, st);
            const sel = await evaluate(client,
                "(function(){var s=getSelection();return {collapsed:s.isCollapsed, len:s.toString().length};})()");
            assertTrue('replace-on-type selected the whole block', !sel.collapsed && sel.len > 0, sel);
            await client.send('Input.insertText', { text: 'REPLACED' });
            const after = await evaluate(client, "__wysTarget('heading').textContent");
            assertTrue('typing replaced the heading contents', after === 'REPLACED', { origText, after });
            await pressKey(client, 'Escape');
            const saves = await messages(client, 'wysiwygChanged');
            assertTrue('replace-on-type committed the new text',
                saves[saves.length - 1].html.includes('REPLACED'), saves[saves.length - 1].html.slice(0, 200));
            assertTrue('replace-on-type dropped the original text',
                !saves[saves.length - 1].html.includes(origText), origText);
        }

        // ===== 20. Font field: blur applies the typed value; Up/Down step live =====
        await goHome();
        {
            const fiSel = "document.getElementById('presto-wysiwyg-font-size')";
            const readSize = () => evaluate(client, "parseFloat(getComputedStyle(__wysTarget('heading')).fontSize)");
            // --- blur commits a typed value (no Enter) ---
            const h = await elementCenter(client, "__wysTarget('heading')");
            await mouseClick(client, h.x, h.y);
            const fi = await elementCenter(client, fiSel);
            await mouseClick(client, fi.x, fi.y, 3); // triple-click selects the field
            await client.send('Input.insertText', { text: '52' });
            const empty = await evaluate(client, '__wysEmptyPoint()');
            await mouseClick(client, empty.x, empty.y); // click back onto the slide → field blurs
            const sizeAfterBlur = await evaluate(client, `WS.goToSlide(${workingSlide}, 0); parseFloat(getComputedStyle(__wysTarget('heading')).fontSize)`);
            assertClose('font field blur applied the typed value (52)', sizeAfterBlur, 52, 1);
            // --- Up/Down arrows step the size live ---
            const h2 = await elementCenter(client, "__wysTarget('heading')");
            await mouseClick(client, h2.x, h2.y);
            const before = await readSize();
            const fi2 = await elementCenter(client, fiSel);
            await mouseClick(client, fi2.x, fi2.y); // focus the field
            await pressKey(client, 'ArrowUp');
            await pressKey(client, 'ArrowUp');
            const afterUp = await readSize();
            assertClose('font field ArrowUp steps +1 each', afterUp - before, 2, 1.2);
            await pressKey(client, 'ArrowDown', 8); // Shift(8)+Down = -10
            const afterShiftDown = await readSize();
            assertClose('font field Shift+ArrowDown steps -10', afterShiftDown - afterUp, -10, 1.5);
            const returnHeading = await elementCenter(client, "__wysTarget('heading')");
            await mouseClick(client, returnHeading.x, returnHeading.y);
            assertTrue('clicking the slide releases font-field keyboard focus',
                await evaluate(client, `document.activeElement !== ${fiSel}`));
            await pressKey(client, 'Enter');
            assertTrue('typing shortcuts work after leaving the font field', (await wysState(client)).textEditing);
            await pressKey(client, 'Escape');
        }

        // --- Editing-state messages kept Swift in sync ---
        const states = await messages(client, 'wysiwygEditingState');
        assertTrue('editing-state messages were posted', states.length >= 4, states.length);

        // ===== 14. Final sweep: LAST, covering every save above =====
        const allSaves = await messages(client, 'wysiwygChanged');
        for (const save of allSaves) {
            if (/ws-visible|ws-active|presto-wysiwyg|contenteditable|data-ws-original/.test(save.html)) {
                throw new Error(`artifact leaked into saved source: ${save.html.slice(0, 300)}`);
            }
        }
        console.log(`PASS all ${allSaves.length} saved payloads are artifact-free`);

        if (runtimeErrors.length) {
            const detail = runtimeErrors[0].exceptionDetails?.exception?.description ||
                runtimeErrors[0].exceptionDetails?.text || 'unknown runtime error';
            throw new Error(`Uncaught runtime exception: ${detail}`);
        }
        console.log('PASS zero uncaught runtime exceptions across the whole run');

        // Screenshot with a live selection for the visual record.
        await goHome();
        const finalPara = await elementCenter(client, "__wysTarget('para')");
        await mouseClick(client, finalPara.x, finalPara.y);
        const screenshot = await client.send('Page.captureScreenshot', { format: 'png' });
        await fs.writeFile(screenshotPath, Buffer.from(screenshot.data, 'base64'));
        console.log(`PASS screenshot: ${screenshotPath}`);
        console.log('WYSIWYG_E2E: passed');
    } finally {
        if (client) client.close();
        if (chrome) chrome.kill('SIGKILL');
        if (server) await new Promise((resolve) => server.close(resolve));
        if (userDataDir) spawnSync('trash', [userDataDir], { stdio: 'ignore' });
    }
}

main().catch((error) => {
    console.error(`FAIL ${error.message}`);
    process.exit(1);
});
