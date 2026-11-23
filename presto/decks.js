/* Presto Decks mode (presto/decks.html).
 *
 * Renders the model built by DeckLibrary.swift: the sidebar (Recent, Pinned,
 * Projects, added folders, Engine), the Continue hero and the deck grid.
 * Shell API:
 *   PrestoDecks.render(model)      model object or its JSON string
 *   PrestoDecks.filter(query)      from the native toolbar's search field
 *   PrestoDecks.setDropActive(on)  native file-drop feedback
 * Messages (webkit.messageHandlers.presto.postMessage({type, ...})):
 *   decksReady {}, decksOpen {path, mode: 'edit'|'present'}, decksOpenPanel {},
 *   decksAddFolder {}, decksRemoveFolder {path}, decksReveal {path},
 *   decksPin {path, pinned}, decksRemoveRecent {path}, decksNeedThumbnail {path}
 */
(function () {
    'use strict';

    var THUMBNAIL_TIMEOUT_MS = 20000;
    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var ICONS = {
        recent: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v5l3 2"/><circle cx="8" cy="8" r="6"/></svg>',
        pinned: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 2.5h10v11l-5-3-5 3z"/></svg>',
        folder: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4.5h4l1.5 1.5H14v6.5H2z"/></svg>',
        add: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10"/></svg>',
        engine: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="3.5" width="11" height="8" rx="1.5"/><path d="M6 14h4"/></svg>',
        play: '<svg class="play" viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.5v9l7-4.5z"/></svg>',
        pin: '<svg class="pin" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 2.5h10v11l-5-3-5 3z"/></svg>',
        cloud: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.6 12.5h6.9a2.9 2.9 0 0 0 .3-5.8 4 4 0 0 0-7.6 1.1 2.4 2.4 0 0 0 .4 4.7z"/></svg>'
    };

    var now = function () { return Date.now(); };

    var els = {
        side: document.getElementById('decks-side'),
        lib: document.getElementById('decks-lib'),
        continueSection: document.getElementById('decks-continue'),
        hero: document.getElementById('decks-hero'),
        results: document.getElementById('decks-results'),
        resultsTitle: document.getElementById('decks-results-title'),
        resultsAside: document.getElementById('decks-results-aside'),
        grid: document.getElementById('decks-grid'),
        none: document.getElementById('decks-none'),
        empty: document.getElementById('decks-empty'),
        menu: document.getElementById('decks-menu'),
        drop: document.getElementById('decks-drop')
    };

    var state = {
        model: { continueDeck: null, projects: [], decks: [], folders: [] },
        byPath: new Map(),
        view: { kind: 'recent' },
        query: '',
        focusKey: null,      // a deck path, or 'hero'
        heroPath: null,
        shown: [],           // decks in the grid, in order
        menu: null,
        dropDepth: 0
    };
    var cards = new Map();   // path -> card entry

    // ------------------------------------------------------------ utilities

    function post(type, payload) {
        var message = { type: type };
        if (payload) Object.keys(payload).forEach(function (k) { message[k] = payload[k]; });
        try {
            var handlers = window.webkit && window.webkit.messageHandlers;
            if (handlers && handlers.presto) handlers.presto.postMessage(message);
        } catch (err) {
            console.warn('Presto Decks: message failed', type, err);
        }
    }

    function esc(text) {
        return String(text == null ? '' : text).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function num(value) {
        return typeof value === 'number' && isFinite(value) ? value : null;
    }

    function folderName(path) {
        var parts = String(path || '').split('/').filter(Boolean);
        if (parts.length && /\.html?$/i.test(parts[parts.length - 1])) parts.pop();
        return parts.length ? parts[parts.length - 1] : 'Untitled deck';
    }

    function isInside(path, folder) {
        var root = folder.charAt(folder.length - 1) === '/' ? folder : folder + '/';
        return path.indexOf(root) === 0;
    }

    function isEngineOnly(deck) {
        return deck.sources.indexOf('engine') !== -1 && !deck.inRecent;
    }

    function plural(n, word) {
        return n + ' ' + word + (n === 1 ? '' : 's');
    }

    function pad(n) { return (n < 10 ? '0' : '') + n; }

    function startOfDay(date) {
        return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
    }

    function editedLabel(ms) {
        if (ms == null) return '';
        var date = new Date(ms);
        var today = new Date(now());
        var time = pad(date.getHours()) + ':' + pad(date.getMinutes());
        var days = Math.round((startOfDay(today) - startOfDay(date)) / 86400000);
        if (days === 0) return 'edited today at ' + time;
        if (days === 1) return 'edited yesterday at ' + time;
        var label = 'edited ' + date.getDate() + ' ' + MONTHS[date.getMonth()];
        return date.getFullYear() === today.getFullYear() ? label : label + ' ' + date.getFullYear();
    }

    function fold(text) {
        return String(text || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
    }

    function matcher(query) {
        var tokens = fold(query).split(/\s+/).filter(Boolean);
        return function (deck) {
            var hay = fold([deck.title, deck.project, deck.path, deck.occasion].join(' \u0001 '));
            return tokens.every(function (t) { return hay.indexOf(t) !== -1; });
        };
    }

    function byModified(a, b) {
        return (b.modified || 0) - (a.modified || 0);
    }

    // ------------------------------------------------------------ model

    function normalizeDeck(raw) {
        var d = raw || {};
        var path = String(d.path || '');
        var sources = Array.isArray(d.sources) ? d.sources.map(String) : [];
        var title = d.title ? String(d.title) : folderName(path);
        var lastSlide = num(d.lastSlide);
        var deck = {
            path: path,
            title: title,
            headline: d.headline ? String(d.headline) : title,
            occasion: d.occasion ? String(d.occasion) : null,
            project: d.project ? String(d.project) : null,
            modified: num(d.modified),
            slideCount: num(d.slideCount),
            appendixCount: num(d.appendixCount),
            stepCount: num(d.stepCount),
            lastSlide: lastSlide,
            lastStep: num(d.lastStep),
            lastSlideLabel: d.lastSlideLabel ? String(d.lastSlideLabel) : (lastSlide == null ? null : String(lastSlide + 1)),
            lastSlideTitle: d.lastSlideTitle ? String(d.lastSlideTitle) : null,
            lastSlideThumbnailDataURL: d.lastSlideThumbnailDataURL || null,
            pinned: !!d.pinned,
            sources: sources,
            thumbnailDataURL: d.thumbnailDataURL || null,
            thumbnailStale: !!d.thumbnailStale,
            cloudOnly: !!d.cloudOnly
        };
        deck.inRecent = d.inRecent === undefined
            ? !(sources.length === 1 && sources[0] === 'engine')
            : !!d.inRecent;
        return deck;
    }

    function normalizeModel(input) {
        var m = input;
        if (typeof m === 'string') {
            try { m = JSON.parse(m); } catch (err) { console.warn('Presto Decks: bad model JSON', err); m = {}; }
        }
        m = m || {};
        var decks = (Array.isArray(m.decks) ? m.decks : []).map(normalizeDeck).filter(function (d) { return d.path; });
        var continueDeck = m.continueDeck && typeof m.continueDeck === 'object' ? m.continueDeck.path : m.continueDeck;
        return {
            continueDeck: continueDeck ? String(continueDeck) : null,
            projects: (Array.isArray(m.projects) ? m.projects : []).filter(function (p) { return p && p.name; })
                .map(function (p) { return { name: String(p.name), count: num(p.count) || 0 }; }),
            decks: decks,
            folders: (Array.isArray(m.folders) ? m.folders : []).map(function (f) {
                if (typeof f === 'string') return { path: f, name: folderName(f + '/x'), count: null, available: true };
                return { path: String(f.path || ''), name: String(f.name || folderName(String(f.path || '') + '/x')),
                         count: num(f.count), available: f.available !== false };
            }).filter(function (f) { return f.path; })
        };
    }

    function engineDecks() {
        return state.model.decks.filter(function (d) { return d.sources.indexOf('engine') !== -1; });
    }

    function folderByPath(path) {
        return state.model.folders.filter(function (f) { return f.path === path; })[0];
    }

    function viewIsValid(view) {
        var m = state.model;
        if (view.kind === 'project') return m.projects.some(function (p) { return p.name === view.name; });
        if (view.kind === 'folder') return m.folders.some(function (f) { return f.path === view.path; });
        if (view.kind === 'engine') return engineDecks().length > 0;
        return view.kind === 'recent' || view.kind === 'pinned';
    }

    // ------------------------------------------------------------ sidebar

    function sideItem(attrs, icon, label, count, extraClass) {
        var on = isCurrentView(attrs) ? ' on' : '';
        var data = Object.keys(attrs).map(function (k) { return ' data-' + k + '="' + esc(attrs[k]) + '"'; }).join('');
        return '<button type="button" class="side-item' + on + (extraClass ? ' ' + extraClass : '') + '"' + data +
            (on ? ' aria-current="page"' : '') + '>' + icon + '<span class="label">' + esc(label) + '</span>' +
            '<em>' + (count === '' || count == null ? '' : esc(count)) + '</em></button>';
    }

    function isCurrentView(attrs) {
        var v = state.view;
        if (attrs.view !== v.kind) return false;
        if (v.kind === 'project') return attrs.name === v.name;
        if (v.kind === 'folder') return attrs.path === v.path;
        return true;
    }

    function engineLabel(deck) {
        return /\/kitchen-sink\/index\.html?$/i.test(deck.path) ? 'Kitchen sink' : deck.title;
    }

    function renderSidebar() {
        var m = state.model;
        var recentCount = m.decks.filter(function (d) { return d.inRecent; }).length;
        var pinnedCount = m.decks.filter(function (d) { return d.pinned; }).length;
        var html = [];
        html.push(sideItem({ view: 'recent' }, ICONS.recent, 'Recent', recentCount || ''));
        html.push(sideItem({ view: 'pinned' }, ICONS.pinned, 'Pinned', pinnedCount || ''));
        var addFolder = '<button type="button" class="side-item quiet" data-act="add-folder">' + ICONS.add +
            '<span class="label">Add folder…</span><em></em></button>';
        if (m.projects.length) {
            html.push('<h6>Projects</h6>');
            m.projects.forEach(function (p) {
                html.push(sideItem({ view: 'project', name: p.name }, ICONS.folder, p.name, p.count));
            });
        }
        if (m.folders.length) {
            html.push('<h6>Folders</h6>');
            m.folders.forEach(function (f) {
                html.push(sideItem({ view: 'folder', path: f.path }, ICONS.folder, f.name,
                    f.available ? f.count : '', f.available ? '' : 'missing'));
            });
        }
        if (!m.projects.length && !m.folders.length) html.push('<h6>Projects</h6>');
        html.push(addFolder);
        var engine = engineDecks();
        if (engine.length) {
            html.push('<h6>Engine</h6>');
            html.push(sideItem({ view: 'engine' }, ICONS.engine, engine.length === 1 ? engineLabel(engine[0]) : 'Examples',
                engine.length === 1 ? '' : engine.length));
        }
        // Keep keyboard focus on the same sidebar row across re-renders.
        var active = document.activeElement;
        var focused = active && els.side.contains(active) && active.classList.contains('side-item')
            ? { view: active.dataset.view, name: active.dataset.name, path: active.dataset.path, act: active.dataset.act }
            : null;
        els.side.innerHTML = html.join('');
        if (focused) {
            var again = sideItems().filter(function (b) {
                return b.dataset.view === focused.view && b.dataset.name === focused.name &&
                    b.dataset.path === focused.path && b.dataset.act === focused.act;
            })[0];
            if (again) again.focus({ preventScroll: true });
        }
        Array.prototype.forEach.call(els.side.querySelectorAll('.side-item'), function (b) {
            if (b.dataset.view === 'folder') {
                var folder = folderByPath(b.dataset.path);
                b.title = folder ? folder.path + (folder.available ? '' : ' (not found)') : '';
            }
        });
    }

    // ------------------------------------------------------------ thumbnails

    function makeThumb(container) {
        var ph = container.querySelector('.ph');
        return {
            thumb: container,
            img: container.querySelector('img'),
            phTitle: ph ? ph.querySelector('b') : null,
            phSub: ph ? ph.querySelector('small') : null,
            src: null
        };
    }

    function setThumb(t, deck) {
        if (t.phTitle) t.phTitle.textContent = deck.headline || deck.title;
        if (t.phSub) t.phSub.textContent = deck.occasion || '';
        var src = deck.thumbnailDataURL || '';
        if (t.src === src) return;
        t.src = src;
        if (!src) {
            t.img.removeAttribute('src');
            t.img.classList.remove('loaded');
            t.thumb.classList.remove('has-image');
            return;
        }
        t.img.onload = function () {
            if (t.src !== src) return;
            t.img.classList.add('loaded');
            t.thumb.classList.add('has-image');
        };
        t.img.onerror = function () {
            if (t.src !== src) return;
            t.img.classList.remove('loaded');
            t.thumb.classList.remove('has-image');
        };
        t.img.src = src;
    }

    var thumbs = {
        observer: null,
        visible: new Set(),
        pending: null,
        timer: 0,
        failed: new Set(),

        init: function () {
            var self = this;
            if (!('IntersectionObserver' in window)) return;
            this.observer = new IntersectionObserver(function (entries) {
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) self.visible.add(entry.target);
                    else self.visible.delete(entry.target);
                });
                self.pump();
            }, { root: els.lib, rootMargin: '240px 0px' });
        },
        track: function (el) {
            if (this.observer) this.observer.observe(el);
            else this.visible.add(el);
        },
        untrack: function (el) {
            if (this.observer) this.observer.unobserve(el);
            this.visible.delete(el);
        },
        needs: function (deck) {
            return !!deck && !deck.cloudOnly && !this.failed.has(deck.path) &&
                (!deck.thumbnailDataURL || deck.thumbnailStale);
        },
        afterRender: function () {
            if (this.pending) {
                var deck = state.byPath.get(this.pending);
                if (!deck || (deck.thumbnailDataURL && !deck.thumbnailStale)) this.finish();
            }
            this.pump();
        },
        finish: function () {
            clearTimeout(this.timer);
            this.timer = 0;
            this.pending = null;
        },
        candidates: function () {
            var self = this;
            var order = [];
            if (!els.continueSection.hidden && state.heroPath) order.push({ el: els.hero, path: state.heroPath });
            Array.prototype.forEach.call(els.grid.children, function (el) { order.push({ el: el, path: el.dataset.path }); });
            return order.filter(function (item) {
                return self.visible.has(item.el) && self.needs(state.byPath.get(item.path));
            }).map(function (item) { return state.byPath.get(item.path); });
        },
        pump: function () {
            if (this.pending) return;
            var list = this.candidates();
            var next = list.filter(function (d) { return !d.thumbnailDataURL; })[0] || list[0];
            if (!next) return;
            var self = this;
            var path = next.path;
            this.pending = path;
            this.timer = setTimeout(function () {
                if (self.pending !== path) return;
                self.failed.add(path);
                self.finish();
                self.pump();
            }, THUMBNAIL_TIMEOUT_MS);
            post('decksNeedThumbnail', { path: path });
        }
    };

    // ------------------------------------------------------------ hero

    var hero = {
        thumb: makeThumb(els.hero.querySelector('.hero-thumb')),
        eyebrow: els.hero.querySelector('.eyebrow'),
        title: els.hero.querySelector('.hero-title'),
        facts: els.hero.querySelector('.hero-facts'),
        where: els.hero.querySelector('.where'),
        whereThumb: els.hero.querySelector('.where-thumb'),
        whereImg: els.hero.querySelector('.where-thumb img'),
        whereText: els.hero.querySelector('.where-text')
    };

    function factsLine(deck) {
        var parts = [];
        if (deck.occasion) parts.push(deck.occasion);
        if (deck.cloudOnly) {
            parts.push('Not downloaded yet');
        } else if (deck.slideCount != null) {
            parts.push(plural(deck.slideCount, 'slide') +
                (deck.appendixCount ? ' and ' + deck.appendixCount + ' in the appendix' : ''));
        }
        if (deck.stepCount) parts.push(plural(deck.stepCount, 'reveal step'));
        return parts.join(' · ');
    }

    function renderHero(deck) {
        state.heroPath = deck ? deck.path : null;
        els.continueSection.hidden = !deck;
        if (!deck) return;
        els.hero.dataset.path = deck.path;
        els.hero.setAttribute('aria-label', 'Continue ' + deck.title);
        hero.eyebrow.textContent = [deck.project, editedLabel(deck.modified)].filter(Boolean).join(' · ');
        hero.title.textContent = deck.headline || deck.title;
        hero.title.title = deck.title;
        hero.facts.textContent = factsLine(deck);
        hero.facts.hidden = !hero.facts.textContent;
        setThumb(hero.thumb, deck);

        var showWhere = deck.lastSlide != null && !(deck.lastSlide === 0 && !deck.lastStep);
        hero.where.hidden = !showWhere;
        if (showWhere) {
            var label = deck.lastSlideLabel || String(deck.lastSlide + 1);
            var place = /^A/.test(label) ? 'appendix slide ' + label : 'slide ' + label;
            hero.whereText.innerHTML = 'You left off on ' + esc(place) +
                (deck.lastSlideTitle ? ', <b>' + esc(deck.lastSlideTitle) + '</b>' : '');
            var src = deck.lastSlideThumbnailDataURL || '';
            hero.whereThumb.hidden = !src;
            if (src && hero.whereImg.getAttribute('src') !== src) hero.whereImg.src = src;
        }
    }

    // ------------------------------------------------------------ cards

    function createCard(path) {
        var el = document.createElement('div');
        el.className = 'card';
        el.setAttribute('role', 'listitem');
        el.tabIndex = -1;
        el.dataset.path = path;
        el.innerHTML =
            '<div class="thumb"><div class="ph"><b></b><small></small></div>' +
            '<img alt="" decoding="async" draggable="false">' +
            '<div class="qa"><button type="button" class="btn" data-act="edit" tabindex="-1">Edit</button>' +
            '<button type="button" class="btn primary" data-act="present" tabindex="-1">' + ICONS.play + 'Present</button></div></div>' +
            '<b class="card-title"></b><small class="card-meta"></small>';
        var entry = {
            el: el,
            thumb: makeThumb(el.querySelector('.thumb')),
            title: el.querySelector('.card-title'),
            meta: el.querySelector('.card-meta'),
            metaKey: null
        };
        thumbs.track(el);
        return entry;
    }

    // A card is named by its occasion ("BREAD/ITAM/CAF 2026", "TCD 2026"): the
    // thumbnail already shows the paper title, and several talks share one.
    // Decks without an occasion, or sharing one with another card, use the full title.
    function cardLabel(deck, occasionCounts) {
        return deck.occasion && occasionCounts[deck.occasion] === 1 ? deck.occasion : deck.title;
    }

    function cardFor(deck, occasionCounts) {
        var entry = cards.get(deck.path);
        if (!entry) {
            entry = createCard(deck.path);
            cards.set(deck.path, entry);
        }
        entry.title.textContent = cardLabel(deck, occasionCounts);
        entry.el.setAttribute('aria-label', deck.title);
        entry.el.title = deck.title + '\n' + deck.path;
        var metaText = [deck.project, deck.cloudOnly ? 'not downloaded' : null, editedLabel(deck.modified)]
            .filter(Boolean).join(' · ');
        var metaKey = (deck.pinned ? 'p' : '') + (deck.cloudOnly ? 'c' : '') + '|' + metaText;
        if (entry.metaKey !== metaKey) {
            entry.metaKey = metaKey;
            entry.meta.innerHTML = (deck.pinned ? ICONS.pin : '') + (deck.cloudOnly ? ICONS.cloud : '') +
                '<span>' + esc(metaText) + '</span>';
        }
        setThumb(entry.thumb, deck);
        return entry.el;
    }

    function placeCards(list) {
        var occasionCounts = {};
        list.forEach(function (d) { if (d.occasion) occasionCounts[d.occasion] = (occasionCounts[d.occasion] || 0) + 1; });
        var wanted = list.map(function (d) { return cardFor(d, occasionCounts); });
        var keep = new Set(wanted);
        Array.prototype.slice.call(els.grid.children).forEach(function (child) {
            if (!keep.has(child)) els.grid.removeChild(child);
        });
        wanted.forEach(function (el, i) {
            if (els.grid.children[i] !== el) els.grid.insertBefore(el, els.grid.children[i] || null);
        });
        // Drop cached cards for decks that left the model.
        cards.forEach(function (entry, path) {
            if (!state.byPath.has(path)) {
                thumbs.untrack(entry.el);
                if (entry.el.parentNode) entry.el.parentNode.removeChild(entry.el);
                cards.delete(path);
            }
        });
    }

    // ------------------------------------------------------------ main area

    function currentList() {
        var m = state.model;
        var v = state.view;
        var scope;
        var title;
        if (v.kind === 'pinned') {
            scope = m.decks.filter(function (d) { return d.pinned; });
            title = 'Pinned';
        } else if (v.kind === 'project') {
            scope = m.decks.filter(function (d) { return d.project === v.name && !isEngineOnly(d); });
            title = v.name;
        } else if (v.kind === 'folder') {
            scope = m.decks.filter(function (d) { return isInside(d.path, v.path); });
            var folder = folderByPath(v.path);
            title = folder ? folder.name : folderName(v.path + '/x');
        } else if (v.kind === 'engine') {
            scope = engineDecks();
            title = 'Engine';
        } else {
            scope = m.decks.filter(function (d) { return d.inRecent; });
            title = 'Recent';
        }
        return { scope: scope.slice().sort(byModified), title: title };
    }

    function renderMain() {
        var m = state.model;
        var query = state.query.trim();
        var view = currentList();
        var heroDeck = null;
        var list;
        var librarySize = m.decks.filter(function (d) { return !isEngineOnly(d); }).length;

        if (query) {
            var pool = state.view.kind === 'recent' ? m.decks.slice().sort(byModified) : view.scope;
            list = pool.filter(matcher(query));
            els.resultsTitle.textContent = state.view.kind === 'recent' ? 'Results' : 'Results in ' + view.title;
            els.resultsAside.textContent = (list.length === 1 ? '1 deck matches' : list.length + ' decks match') +
                ' “' + query + '”';
        } else {
            list = view.scope;
            if (state.view.kind === 'recent' && m.continueDeck) {
                heroDeck = state.byPath.get(m.continueDeck) || null;
                if (heroDeck) list = list.filter(function (d) { return d.path !== heroDeck.path; });
            }
            els.resultsTitle.textContent = view.title;
            els.resultsAside.textContent = list.length > 1 ? 'Sorted by last edited' : '';
        }

        var showEmpty = !query && state.view.kind === 'recent' && librarySize === 0;
        els.empty.hidden = !showEmpty;
        renderHero(showEmpty ? null : heroDeck);
        state.shown = showEmpty ? [] : list;
        placeCards(state.shown);

        var hideResults = showEmpty || (!query && heroDeck && list.length === 0);
        els.results.hidden = !!hideResults;
        els.none.hidden = list.length > 0 || hideResults;
        if (!els.none.hidden) {
            els.none.textContent = query ? 'No decks match “' + query + '”.' : emptyViewText();
        }
        restoreFocus();
    }

    function emptyViewText() {
        if (state.view.kind === 'pinned') return 'Nothing pinned yet. Right-click a deck and choose Pin.';
        if (state.view.kind === 'folder') {
            var folder = folderByPath(state.view.path);
            if (folder && !folder.available) return 'This folder is not available. It may be on a drive that is not connected.';
            return 'No decks in this folder. Decks are folders with an index.html, one or two levels down.';
        }
        return 'No decks here yet.';
    }

    function setView(view) {
        if (!viewIsValid(view)) view = { kind: 'recent' };
        state.view = view;
        state.focusKey = null;
        renderSidebar();
        renderMain();
        els.lib.scrollTop = 0;
        thumbs.afterRender();
    }

    // ------------------------------------------------------------ focus and keys

    function focusables() {
        var items = [];
        if (!els.continueSection.hidden) items.push(els.hero);
        return items.concat(Array.prototype.slice.call(els.grid.children));
    }

    function keyOf(el) {
        return el === els.hero ? 'hero' : el.dataset.path;
    }

    function elementForKey(key) {
        if (!key) return null;
        if (key === 'hero') return els.continueSection.hidden ? null : els.hero;
        var entry = cards.get(key);
        return entry && entry.el.parentNode === els.grid ? entry.el : null;
    }

    function deckForElement(el) {
        if (!el) return null;
        if (el === els.hero) return state.heroPath ? state.byPath.get(state.heroPath) : null;
        return state.byPath.get(el.dataset.path) || null;
    }

    function setFocusEl(el, options) {
        var opts = options || {};
        focusables().forEach(function (item) {
            item.classList.toggle('is-focus', item === el);
            item.tabIndex = item === el ? 0 : -1;
        });
        state.focusKey = el ? keyOf(el) : null;
        if (!el) return;
        if (opts.focus !== false) el.focus({ preventScroll: true });
        if (opts.scroll !== false) {
            if (el === els.hero) els.lib.scrollTop = 0;
            else el.scrollIntoView({ block: 'nearest' });
        }
    }

    function restoreFocus() {
        var items = focusables();
        items.forEach(function (item) { item.tabIndex = -1; item.classList.remove('is-focus'); });
        var el = elementForKey(state.focusKey);
        if (el) {
            var hadFocus = document.activeElement === el || document.activeElement === document.body ||
                els.lib.contains(document.activeElement);
            setFocusEl(el, { focus: hadFocus, scroll: false });
        } else {
            state.focusKey = null;
            if (items[0]) items[0].tabIndex = 0;
        }
    }

    function gridColumns() {
        var template = getComputedStyle(els.grid).gridTemplateColumns || '';
        var n = template.split(/\s+/).filter(function (t) { return t && t !== 'none'; }).length;
        return Math.max(1, n);
    }

    function moveFocus(direction) {
        var items = focusables();
        if (!items.length) return;
        var current = items.indexOf(elementForKey(state.focusKey));
        if (current < 0) { setFocusEl(items[0]); return; }
        var offset = items[0] === els.hero ? 1 : 0;
        var count = items.length - offset;
        var next = current;
        if (direction === 'left' && current === 0) {
            var side = els.side.querySelector('.side-item.on') || els.side.querySelector('.side-item');
            if (side) side.focus();
            return;
        }
        if (direction === 'left') next = Math.max(0, current - 1);
        else if (direction === 'right') next = Math.min(items.length - 1, current + 1);
        else if (direction === 'home') next = 0;
        else if (direction === 'end') next = items.length - 1;
        else {
            var cols = gridColumns();
            var onHero = offset === 1 && current === 0;
            var index = current - offset;
            if (direction === 'up') {
                if (onHero) next = current;
                else if (index - cols >= 0) next = current - cols;
                else if (offset) next = 0;
            } else if (direction === 'down') {
                if (onHero) next = count ? offset : current;
                else {
                    var lastRowStart = Math.floor((count - 1) / cols) * cols;
                    if (index < lastRowStart) next = offset + Math.min(index + cols, count - 1);
                }
            }
        }
        setFocusEl(items[next]);
    }

    function open(deck, mode) {
        if (!deck) return;
        closeMenu();
        post('decksOpen', { path: deck.path, mode: mode === 'present' ? 'present' : 'edit' });
    }

    function sideItems() {
        return Array.prototype.slice.call(els.side.querySelectorAll('.side-item'));
    }

    document.addEventListener('keydown', function (event) {
        if (state.menu) { menuKey(event); return; }
        var key = event.key;
        var active = document.activeElement;
        if (active && active.classList && active.classList.contains('side-item')) {
            if (key === 'ArrowDown' || key === 'ArrowUp') {
                event.preventDefault();
                var list = sideItems();
                var i = list.indexOf(active) + (key === 'ArrowDown' ? 1 : -1);
                if (list[i]) list[i].focus();
                return;
            }
            if (key === 'ArrowRight') {
                event.preventDefault();
                var target = elementForKey(state.focusKey) || focusables()[0];
                if (target) setFocusEl(target);
            }
            return;
        }
        var directions = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', Home: 'home', End: 'end' };
        if (directions[key] && !event.altKey && !event.ctrlKey && !event.metaKey) {
            event.preventDefault();
            moveFocus(directions[key]);
            return;
        }
        // A focused button (Chromium focuses on click) handles its own Return.
        if (event.target && event.target.closest && event.target.closest('button')) return;
        if (key === 'Enter') {
            var deck = deckForElement(elementForKey(state.focusKey));
            if (!deck) return;
            event.preventDefault();
            open(deck, event.metaKey ? 'present' : 'edit');
            return;
        }
        if (key === 'ContextMenu' || (key === 'F10' && event.shiftKey)) {
            var el = elementForKey(state.focusKey);
            if (el) {
                event.preventDefault();
                var rect = el.getBoundingClientRect();
                openDeckMenu(deckForElement(el), rect.left + 24, rect.top + Math.min(rect.height / 2, 80));
            }
        }
    });

    // ------------------------------------------------------------ pointer

    els.lib.addEventListener('click', function (event) {
        var target = event.target;
        var button = target.closest('button[data-act]');
        if (button) {
            var act = button.dataset.act;
            if (act === 'open-panel') { post('decksOpenPanel', {}); return; }
            if (act === 'add-folder') { post('decksAddFolder', {}); return; }
            var holder = button.closest('.card, .hero');
            var deck = deckForElement(holder);
            if (holder) setFocusEl(holder, { scroll: false });
            if (deck) open(deck, act === 'present' ? 'present' : 'edit');
            return;
        }
        var item = target.closest('.card, .hero');
        if (item) setFocusEl(item, { scroll: false });
    });

    els.lib.addEventListener('dblclick', function (event) {
        if (event.target.closest('button')) return;
        var item = event.target.closest('.card, .hero');
        if (item) open(deckForElement(item), 'edit');
    });

    els.side.addEventListener('click', function (event) {
        var item = event.target.closest('.side-item');
        if (!item) return;
        if (item.dataset.act === 'add-folder') { post('decksAddFolder', {}); return; }
        var view = { kind: item.dataset.view };
        if (view.kind === 'project') view.name = item.dataset.name;
        if (view.kind === 'folder') view.path = item.dataset.path;
        setView(view);
    });

    document.addEventListener('contextmenu', function (event) {
        event.preventDefault(); // never WebKit's own menu (Reload, Inspect)
        var item = event.target.closest('.card, .hero');
        if (item) {
            setFocusEl(item, { scroll: false });
            openDeckMenu(deckForElement(item), event.clientX, event.clientY);
            return;
        }
        var side = event.target.closest('.side-item[data-view="folder"]');
        if (side) {
            var folder = folderByPath(side.dataset.path);
            if (folder) openFolderMenu(folder, event.clientX, event.clientY);
            return;
        }
        closeMenu();
    });

    // ------------------------------------------------------------ context menu

    function openDeckMenu(deck, x, y) {
        if (!deck) return;
        var items = [
            { label: 'Edit', kbd: '↩', run: function () { open(deck, 'edit'); } },
            { label: 'Present from Start', kbd: '⌘↩', run: function () { open(deck, 'present'); } },
            'sep',
            { label: 'Reveal in Finder', run: function () { post('decksReveal', { path: deck.path }); } },
            { label: deck.pinned ? 'Unpin' : 'Pin', run: function () { setPinned(deck, !deck.pinned); } }
        ];
        if (deck.inRecent && state.view.kind !== 'engine') {
            items.push('sep');
            items.push({ label: 'Remove from Recent', run: function () { removeRecent(deck); } });
        }
        openMenu(items, x, y);
    }

    function openFolderMenu(folder, x, y) {
        openMenu([
            { label: 'Reveal in Finder', run: function () { post('decksReveal', { path: folder.path }); } },
            'sep',
            { label: 'Remove Folder from Decks', danger: true, run: function () { removeFolder(folder); } }
        ], x, y);
    }

    function openMenu(items, x, y) {
        closeMenu();
        var html = items.map(function (item, i) {
            if (item === 'sep') return '<div class="sep" role="separator"></div>';
            return '<button type="button" class="mi' + (item.danger ? ' danger' : '') + '" role="menuitem" data-i="' + i + '">' +
                '<span>' + esc(item.label) + '</span>' + (item.kbd ? '<kbd>' + esc(item.kbd) + '</kbd>' : '') + '</button>';
        }).join('');
        els.menu.innerHTML = html;
        els.menu.hidden = false;
        var width = els.menu.offsetWidth;
        var height = els.menu.offsetHeight;
        var left = Math.max(6, Math.min(x, window.innerWidth - width - 6));
        var top = Math.max(6, Math.min(y, window.innerHeight - height - 6));
        els.menu.style.left = left + 'px';
        els.menu.style.top = top + 'px';
        state.menu = { items: items, index: -1 };
    }

    function closeMenu() {
        if (!state.menu) return;
        state.menu = null;
        els.menu.hidden = true;
        els.menu.innerHTML = '';
    }

    function menuButtons() {
        return Array.prototype.slice.call(els.menu.querySelectorAll('.mi'));
    }

    function setMenuActive(index) {
        var buttons = menuButtons();
        buttons.forEach(function (b, i) { b.classList.toggle('active', i === index); });
        if (state.menu) state.menu.index = index;
    }

    function runMenuItem(button) {
        if (!state.menu || !button) return;
        var item = state.menu.items[Number(button.dataset.i)];
        closeMenu();
        if (item && item.run) item.run();
        var el = elementForKey(state.focusKey);
        if (el) el.focus({ preventScroll: true });
    }

    function menuKey(event) {
        var buttons = menuButtons();
        var index = state.menu.index;
        if (event.key === 'Escape') { event.preventDefault(); closeMenu(); return; }
        if (event.key === 'ArrowDown') { event.preventDefault(); setMenuActive((index + 1) % buttons.length); return; }
        if (event.key === 'ArrowUp') { event.preventDefault(); setMenuActive(index <= 0 ? buttons.length - 1 : index - 1); return; }
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            if (index >= 0) runMenuItem(buttons[index]);
            return;
        }
        if (event.key === 'Tab') { event.preventDefault(); return; }
        closeMenu();
    }

    els.menu.addEventListener('mousemove', function (event) {
        var button = event.target.closest('.mi');
        if (button) setMenuActive(menuButtons().indexOf(button));
    });
    els.menu.addEventListener('mouseleave', function () { setMenuActive(-1); });
    els.menu.addEventListener('click', function (event) {
        runMenuItem(event.target.closest('.mi'));
    });
    document.addEventListener('mousedown', function (event) {
        if (state.menu && !els.menu.contains(event.target)) closeMenu();
    }, true);
    window.addEventListener('blur', closeMenu);
    els.lib.addEventListener('scroll', closeMenu, { passive: true });

    // ------------------------------------------------------------ actions (optimistic; the next render wins)

    function rerender() {
        renderSidebar();
        renderMain();
        thumbs.afterRender();
    }

    function setPinned(deck, pinned) {
        post('decksPin', { path: deck.path, pinned: pinned });
        deck.pinned = pinned;
        if (!pinned && !deck.inRecent && deck.sources.length === 1 && deck.sources[0] === 'pinned') {
            state.model.decks = state.model.decks.filter(function (d) { return d !== deck; });
            state.byPath.delete(deck.path);
        }
        rerender();
    }

    function removeRecent(deck) {
        post('decksRemoveRecent', { path: deck.path });
        deck.inRecent = false;
        if (state.model.continueDeck === deck.path) state.model.continueDeck = null;
        if (!deck.pinned && deck.sources.every(function (s) { return s === 'recent'; })) {
            state.model.decks = state.model.decks.filter(function (d) { return d !== deck; });
            state.byPath.delete(deck.path);
        }
        rerender();
    }

    function removeFolder(folder) {
        post('decksRemoveFolder', { path: folder.path });
        state.model.folders = state.model.folders.filter(function (f) { return f !== folder; });
        if (state.view.kind === 'folder' && state.view.path === folder.path) state.view = { kind: 'recent' };
        rerender();
    }

    // ------------------------------------------------------------ drag and drop

    function dragHasFiles(event) {
        var types = event.dataTransfer && event.dataTransfer.types;
        if (!types) return false;
        var list = Array.prototype.slice.call(types);
        return list.indexOf('Files') !== -1 || list.indexOf('text/uri-list') !== -1;
    }

    function droppedPath(transfer) {
        if (!transfer) return null;
        var text = '';
        try { text = transfer.getData('text/uri-list') || transfer.getData('text/plain') || ''; } catch (err) { text = ''; }
        var lines = text.split(/\r?\n/).map(function (l) { return l.trim(); })
            .filter(function (l) { return l && l.charAt(0) !== '#'; });
        for (var i = 0; i < lines.length; i++) {
            var path = null;
            if (/^file:\/\//i.test(lines[i])) {
                try { path = decodeURIComponent(new URL(lines[i]).pathname); } catch (err) { path = null; }
            } else if (lines[i].charAt(0) === '/') {
                path = lines[i];
            }
            if (!path) continue;
            path = path.replace(/\/+$/, '') || '/';
            var last = path.split('/').pop();
            if (/\.html?$/i.test(last) || last.indexOf('.') === -1) return path;
        }
        return null;
    }

    // body.dropping keeps cards from looking hovered under the drop target.
    function setDropActive(on) {
        els.drop.hidden = !on;
        document.body.classList.toggle('dropping', on);
        if (!on) state.dropDepth = 0;
    }

    window.addEventListener('dragenter', function (event) {
        if (!dragHasFiles(event)) return;
        event.preventDefault();
        state.dropDepth += 1;
        setDropActive(true);
    });
    window.addEventListener('dragover', function (event) {
        if (!dragHasFiles(event)) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    });
    window.addEventListener('dragleave', function (event) {
        if (!dragHasFiles(event)) return;
        state.dropDepth = Math.max(0, state.dropDepth - 1);
        if (state.dropDepth === 0) setDropActive(false);
    });
    window.addEventListener('drop', function (event) {
        event.preventDefault(); // never let WebKit navigate this page to the dropped file
        setDropActive(false);
        var path = droppedPath(event.dataTransfer);
        if (path) post('decksOpen', { path: path, mode: 'edit' });
    });

    // ------------------------------------------------------------ public API

    function render(model) {
        state.model = normalizeModel(model);
        state.byPath = new Map(state.model.decks.map(function (d) { return [d.path, d]; }));
        if (!viewIsValid(state.view)) state.view = { kind: 'recent' };
        rerender();
    }

    function filter(query) {
        var next = String(query == null ? '' : query);
        if (next === state.query) return;
        state.query = next;
        renderMain();
        if (state.query.trim() && !elementForKey(state.focusKey)) state.focusKey = null;
        els.lib.scrollTop = 0;
        thumbs.afterRender();
    }

    thumbs.init();
    thumbs.track(els.hero);

    window.PrestoDecks = {
        render: render,
        filter: filter,
        setDropActive: function (on) { setDropActive(!!on); },
        // Test hooks.
        _setNow: function (fn) { now = typeof fn === 'function' ? fn : function () { return Date.now(); }; },
        _state: function () { return state; },
        _setThumbnailTimeout: function (ms) { THUMBNAIL_TIMEOUT_MS = ms; },
        _thumbnailPending: function () { return thumbs.pending; },
        _editedLabel: editedLabel
    };

    rerender();
    post('decksReady', {});
})();
