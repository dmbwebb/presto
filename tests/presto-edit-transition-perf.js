#!/usr/bin/env node
/* Measures Presto edit-preview slide transition time.
 *
 * The regression target is the user-visible edit-mode transition: switching
 * slides should stay inside the already-loaded preview page, not reload the
 * whole deck and flash white. The test compares in-page edit navigation
 * against a full deck reload on the same generated deck.
 */

const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { performance } = require('node:perf_hooks');
const { spawn, spawnSync } = require('node:child_process');

const projectRoot = path.resolve(__dirname, '..');
const chromePath = process.env.CHROME ||
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const minSpeedup = Number(process.env.PRESTO_TRANSITION_MIN_SPEEDUP || '5');
// The dirty path includes the editor flush round trip (about 12 ms). Since the
// theme's fonts are local (Oct 2026) a full reload no longer waits on Google
// Fonts and takes about 60 ms instead of 90, so the dirty ratio is held to 3x;
// the absolute cap below is the user-facing guarantee.
const minDirtySpeedup = Number(process.env.PRESTO_DIRTY_MIN_SPEEDUP || '3');
const maxDirtySwitchMs = Number(process.env.PRESTO_DIRTY_SWITCH_MAX_MS || '150');
const iterations = Number(process.env.PRESTO_TRANSITION_ITERATIONS || '10');
const deckInput = process.env.PRESTO_TRANSITION_DECK || '';
const debug = process.env.PRESTO_TRANSITION_DEBUG === '1';
const slideCount = Number(process.env.PRESTO_TRANSITION_SYNTHETIC_SLIDES || '48');
const rowsPerSlide = Number(process.env.PRESTO_TRANSITION_SYNTHETIC_ROWS || '35');

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
            handlers.splice(0).forEach((handler) => handler(msg.params || {}));
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
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
        });
    }

    once(method) {
        return new Promise((resolve) => {
            const handlers = this.listeners.get(method) || [];
            handlers.push(resolve);
            this.listeners.set(method, handlers);
        });
    }

    close() {
        this.ws.close();
    }
}

function contentType(filePath) {
    if (filePath.endsWith('.html')) return 'text/html';
    if (filePath.endsWith('.js')) return 'application/javascript';
    if (filePath.endsWith('.css')) return 'text/css';
    return 'application/octet-stream';
}

function isPathInsideDirectory(filePath, directory) {
    const relative = path.relative(directory, filePath);
    return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

async function writePerfDeck() {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'presto-transition-'));
    const deckDir = path.join(tempDir, 'deck');
    await fs.mkdir(deckDir);

    const slides = [];
    for (let i = 0; i < slideCount; i++) {
        const rows = [];
        for (let j = 0; j < rowsPerSlide; j++) {
            rows.push(
                `<p data-step="${(j % 6) + 1}"><strong>${i + 1}.${j + 1}</strong> Transition timing content ${'x'.repeat(48)}</p>`
            );
        }
        slides.push(`
            <section class="slide content" id="slide-${i + 1}">
                <h2>Perf Slide ${i + 1}</h2>
                <div class="perf-grid">${rows.join('\n')}</div>
            </section>
        `);
    }

    const html = `<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=1280">
    <title>Presto Transition Perf</title>
    <link rel="stylesheet" href="/engine/webslides.css">
    <style>
        body { margin: 0; background: #151515; font-family: Arial, sans-serif; }
        .slide { box-sizing: border-box; padding: 42px 54px; background: white; color: #111; }
        .slide h2 { margin: 0 0 18px; font-size: 38px; }
        .perf-grid { columns: 3; column-gap: 30px; font-size: 10px; line-height: 1.18; }
        .perf-grid p { break-inside: avoid; margin: 0 0 5px; }
    </style>
    <script defer src="/engine/webslides.js"></script>
</head>
<body>
    <div class="ws-deck">
        ${slides.join('\n')}
    </div>
</body>
</html>`;

    await fs.writeFile(path.join(deckDir, 'index.html'), html, 'utf8');
    return { tempDir, deckDir, entryFile: 'index.html', label: 'generated heavy deck' };
}

async function resolveDeckSource() {
    if (!deckInput) return writePerfDeck();

    const resolved = path.resolve(deckInput);
    const stat = await fs.stat(resolved);
    const deckDir = stat.isDirectory() ? resolved : path.dirname(resolved);
    const entryFile = stat.isDirectory() ? 'index.html' : path.basename(resolved);
    await fs.access(path.join(deckDir, entryFile));
    return { tempDir: null, deckDir, entryFile, label: resolved };
}

async function startServer(deckDir) {
    const server = http.createServer(async (req, res) => {
        try {
            const requestPath = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
            let filePath;
            if (requestPath.startsWith('/deck/')) {
                filePath = path.resolve(deckDir, requestPath.replace(/^\/deck\/?/, ''));
                if (!isPathInsideDirectory(filePath, deckDir)) throw new Error('Forbidden');
            } else {
                filePath = path.resolve(projectRoot, requestPath.slice(1));
                if (!isPathInsideDirectory(filePath, projectRoot)) throw new Error('Forbidden');
            }
            const data = await fs.readFile(filePath);
            res.writeHead(200, { 'Content-Type': contentType(filePath) });
            res.end(data);
        } catch (error) {
            res.writeHead(error.message === 'Forbidden' ? 403 : 404);
            res.end(error.message);
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

async function navigateAndWait(client, url) {
    const loaded = client.once('Page.loadEventFired');
    await client.send('Page.navigate', { url });
    await loaded;
}

async function prepareEditPreview(client) {
    return evaluate(client, `
        (async function() {
            const started = performance.now();
            await new Promise((resolve, reject) => {
                function check() {
                    if (window.WS && WS.slides && WS.slides.length &&
                        typeof installPrestoEditSidebar === 'function' &&
                        typeof applyPrestoEditViewport === 'function' &&
                        typeof window.replacePrestoEditSlideHTML === 'function') {
                        resolve();
                    } else {
                        if (performance.now() - started > 5000) {
                            reject(new Error('preview helpers did not become ready: ' + JSON.stringify({
                                href: location.href,
                                hasWS: !!window.WS,
                                slideCount: window.WS && WS.slides ? WS.slides.length : null,
                                hasSidebar: typeof installPrestoEditSidebar,
                                hasViewport: typeof applyPrestoEditViewport,
                                hasReplace: typeof window.replacePrestoEditSlideHTML
                            })));
                            return;
                        }
                        requestAnimationFrame(check);
                    }
                }
                check();
            });
            installPrestoEditSidebar();
            applyPrestoEditViewport({ viewportWidth: 900, viewportHeight: 1130 });
            await new Promise((resolve) => setTimeout(resolve, 0));
            return WS.slides.length;
        })()
    `);
}

async function prepareEditor(client) {
    return evaluate(client, `
        (async function() {
            const started = performance.now();
            await new Promise((resolve, reject) => {
                function check() {
                    if (typeof window.setContent === 'function' &&
                        typeof window.getPendingEditorChange === 'function' &&
                        typeof window.markEditorSaved === 'function') {
                        resolve();
                    } else {
                        if (performance.now() - started > 5000) {
                            reject(new Error('editor helpers did not become ready: ' + JSON.stringify({
                                href: location.href,
                                hasSetContent: typeof window.setContent,
                                hasPendingChange: typeof window.getPendingEditorChange,
                                hasMarkSaved: typeof window.markEditorSaved
                            })));
                            return;
                        }
                        requestAnimationFrame(check);
                    }
                }
                check();
            });
            return true;
        })()
    `);
}

function editorSetContentExpression(sectionHTML, index, totalSlides) {
    return `
        (async function() {
            window.setContent(${JSON.stringify(sectionHTML)}, ${index}, ${index + 1}, ${totalSlides});
            await new Promise((resolve) => setTimeout(resolve, 0));
            const editor = document.getElementById('editor');
            if (!editor || !editor.value) throw new Error('editor did not receive slide content');
            return true;
        })()
    `;
}

async function setEditorSlide(client, sectionHTML, index, totalSlides) {
    return evaluate(client, editorSetContentExpression(sectionHTML, index, totalSlides));
}

async function makeEditorDirty(client, iteration) {
    return evaluate(client, `
        (function() {
            const editor = document.getElementById('editor');
            editor.value += '\\n<p data-presto-perf="${iteration}">Dirty switch timing ${iteration}</p>';
            editor.dispatchEvent(new Event('input', { bubbles: true }));
            return true;
        })()
    `);
}

async function measureDirtyEditorTransition(previewClient, editorClient, targetIndex) {
    const start = performance.now();
    const pending = await evaluate(editorClient, 'window.getPendingEditorChange()');
    if (!pending || pending.dirty !== true || !Number.isSafeInteger(pending.index)) {
        throw new Error('editor did not expose a dirty pending change');
    }

    const patchOK = await evaluate(previewClient, `
        (function() {
            const source = WS.slides[${pending.index}];
            const clone = source.cloneNode(true);
            clone.innerHTML = ${JSON.stringify(pending.html)};
            return window.replacePrestoEditSlideHTML(${pending.index}, clone.outerHTML);
        })()
    `);
    if (patchOK !== true) throw new Error('dirty slide preview patch failed');

    await evaluate(editorClient, `window.markEditorSaved(${pending.index}, ${JSON.stringify(pending.html)})`);
    const target = await evaluate(previewClient, `
        (async function() {
            WS.goToSlide(${targetIndex}, 0);
            await new Promise((resolve) => setTimeout(resolve, 0));
            const active = WS.slides[${targetIndex}];
            const rect = active.getBoundingClientRect();
            if (WS.currentIndex !== ${targetIndex} || !active.classList.contains('ws-active') || rect.width <= 0 || rect.height <= 0) {
                throw new Error('target slide did not become visible');
            }
            return { html: active.outerHTML, total: WS.slides.length };
        })()
    `);
    await setEditorSlide(editorClient, target.html, targetIndex, target.total);
    return performance.now() - start;
}

async function measureReloadTransition(browserWsURL, url, targetIndex) {
    const start = performance.now();
    const page = await newPage(browserWsURL, 'about:blank');
    const reloadClient = new CDP(page.webSocketDebuggerUrl);
    try {
        await reloadClient.open();
        await reloadClient.send('Page.enable');
        await reloadClient.send('Runtime.enable');
        logDebug(`reload navigate start ${targetIndex + 1}`);
        await navigateAndWait(reloadClient, `${url}?slide=${targetIndex + 1}`);
        logDebug(`reload load fired ${targetIndex + 1}`);
        await prepareEditPreview(reloadClient);
        logDebug(`reload preview prepared ${targetIndex + 1}`);
        await evaluate(reloadClient, `
            (async function() {
                await new Promise((resolve) => {
                    function check() {
                        const active = window.WS && WS.slides && WS.slides[${targetIndex}];
                        if (active && WS.currentIndex === ${targetIndex} && active.classList.contains('ws-active')) {
                            resolve();
                        } else {
                            requestAnimationFrame(check);
                        }
                    }
                    check();
                });
                return true;
            })()
        `);
        return performance.now() - start;
    } finally {
        reloadClient.close();
    }
}

async function measureDirectTransition(client, targetIndex) {
    return evaluate(client, `
        (async function() {
            const started = performance.now();
            WS.goToSlide(${targetIndex}, 0);
            await new Promise((resolve) => setTimeout(resolve, 0));
            const active = WS.slides[${targetIndex}];
            const rect = active.getBoundingClientRect();
            if (WS.currentIndex !== ${targetIndex} || !active.classList.contains('ws-active') || rect.width <= 0 || rect.height <= 0) {
                throw new Error('target slide did not become visible');
            }
            return performance.now() - started;
        })()
    `);
}

function median(values) {
    const sorted = values.slice().sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
}

function fmt(value) {
    return `${value.toFixed(1)}ms`;
}

function logDebug(message) {
    if (debug) console.error(`[presto-transition] ${message}`);
}

function trashPath(target) {
    if (!target) return;
    spawnSync('trash', [target], { stdio: 'ignore' });
}

function transitionTargets(totalSlides) {
    if (!Number.isSafeInteger(totalSlides) || totalSlides < 3) {
        throw new Error(`Need at least 3 slides for transition timing; found ${totalSlides}`);
    }
    const first = Math.min(24, totalSlides - 2);
    const second = first + 1;
    return [first, second];
}

function alternateTransitionTarget(targetIndex, targets) {
    return targetIndex === targets[0] ? targets[1] : targets[0];
}

(async function main() {
    if (!Number.isFinite(minSpeedup) || minSpeedup <= 0) {
        throw new Error('PRESTO_TRANSITION_MIN_SPEEDUP must be a positive number');
    }
    if (!Number.isFinite(maxDirtySwitchMs) || maxDirtySwitchMs <= 0) {
        throw new Error('PRESTO_DIRTY_SWITCH_MAX_MS must be a positive number');
    }
    if (!Number.isSafeInteger(iterations) || iterations < 3) {
        throw new Error('PRESTO_TRANSITION_ITERATIONS must be an integer >= 3');
    }
    if (!Number.isSafeInteger(slideCount) || slideCount < 3) {
        throw new Error('PRESTO_TRANSITION_SYNTHETIC_SLIDES must be an integer >= 3');
    }
    if (!Number.isSafeInteger(rowsPerSlide) || rowsPerSlide < 1) {
        throw new Error('PRESTO_TRANSITION_SYNTHETIC_ROWS must be an integer >= 1');
    }

    const deckSource = await resolveDeckSource();
    const userDataDir = await fs.mkdtemp(path.join(os.tmpdir(), 'presto-chrome-'));
    const server = await startServer(deckSource.deckDir);
    const port = server.address().port;
    const deckURL = `http://127.0.0.1:${port}/deck/${encodeURIComponent(deckSource.entryFile)}`;
    const editorURL = `http://127.0.0.1:${port}/presto/editor.html`;
    const chrome = spawn(chromePath, [
        '--headless=new',
        '--remote-debugging-port=0',
        '--disable-gpu',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-background-timer-throttling',
        `--user-data-dir=${userDataDir}`,
        'about:blank'
    ], { stdio: ['ignore', 'ignore', 'pipe'] });

    let client;
    let editorClient;
    try {
        const browserWsURL = await waitForDevTools(chrome);
        logDebug('Chrome DevTools ready');
        const page = await newPage(browserWsURL, 'about:blank');
        client = new CDP(page.webSocketDebuggerUrl);
        await client.open();
        await client.send('Page.enable');
        await client.send('Runtime.enable');
        await navigateAndWait(client, deckURL);
        logDebug('Preview page loaded');
        const totalSlides = await prepareEditPreview(client);
        logDebug(`Preview prepared with ${totalSlides} slides`);
        const targets = transitionTargets(totalSlides);

        const editorPage = await newPage(browserWsURL, 'about:blank');
        editorClient = new CDP(editorPage.webSocketDebuggerUrl);
        await editorClient.open();
        await editorClient.send('Page.enable');
        await editorClient.send('Runtime.enable');
        await navigateAndWait(editorClient, editorURL);
        logDebug('Editor page loaded');
        await prepareEditor(editorClient);
        logDebug('Editor prepared');

        const reloadTimes = [];
        const directTimes = [];
        const dirtyTimes = [];
        for (let i = 0; i < iterations; i++) {
            const target = i % 2 === 0 ? targets[0] : targets[1];
            logDebug(`Iteration ${i + 1}/${iterations}: reload to ${target + 1}`);
            reloadTimes.push(await measureReloadTransition(browserWsURL, deckURL, target));
            const directTarget = alternateTransitionTarget(target, targets);
            logDebug(`Iteration ${i + 1}/${iterations}: direct to ${directTarget + 1}`);
            directTimes.push(await measureDirectTransition(client, directTarget));

            const currentHTML = await evaluate(client, `WS.slides[WS.currentIndex].outerHTML`);
            await setEditorSlide(editorClient, currentHTML, directTarget, totalSlides);
            await makeEditorDirty(editorClient, i);
            logDebug(`Iteration ${i + 1}/${iterations}: dirty switch to ${target + 1}`);
            dirtyTimes.push(await measureDirtyEditorTransition(client, editorClient, target));
        }

        const reloadMedian = median(reloadTimes);
        const directMedian = median(directTimes);
        const dirtyMedian = median(dirtyTimes);
        const speedup = reloadMedian / Math.max(directMedian, 0.1);
        const dirtySpeedup = reloadMedian / Math.max(dirtyMedian, 0.1);

        console.log(`Presto edit transition deck: ${deckSource.label}`);
        console.log(`Presto edit transition median reload: ${fmt(reloadMedian)}`);
        console.log(`Presto edit transition median direct: ${fmt(directMedian)}`);
        console.log(`Presto edit transition median dirty editor switch: ${fmt(dirtyMedian)}`);
        console.log(`Presto edit transition speedup: ${speedup.toFixed(1)}x`);
        console.log(`Presto dirty editor transition speedup: ${dirtySpeedup.toFixed(1)}x`);

        if (speedup < minSpeedup) {
            throw new Error(`Expected at least ${minSpeedup}x faster direct slide switching; measured ${speedup.toFixed(1)}x`);
        }
        if (dirtySpeedup < minDirtySpeedup) {
            throw new Error(`Expected at least ${minDirtySpeedup}x faster dirty editor slide switching; measured ${dirtySpeedup.toFixed(1)}x`);
        }
        if (dirtyMedian > maxDirtySwitchMs) {
            throw new Error(`Expected dirty editor slide switching under ${fmt(maxDirtySwitchMs)}; measured ${fmt(dirtyMedian)}`);
        }
    } finally {
        if (editorClient) editorClient.close();
        if (client) client.close();
        server.close();
        chrome.kill('SIGTERM');
        trashPath(deckSource.tempDir);
        trashPath(userDataDir);
    }
})().catch((error) => {
    console.error(`FAIL ${error.message}`);
    process.exit(1);
});
