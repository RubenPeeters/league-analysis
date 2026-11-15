let globalData = {};
let currentRole = 'JUNGLE';
let DDRAGON_VER = "14.23.1"; // Will be updated from data.json 
let itemInfo = {}; // id -> {name, text, cost}, from Data Dragon item.json
let runeInfo = {}; // rune or rune style id -> {name, icon}, from Data Dragon runesReforged.json
let spellInfo = {}; // summoner spell id -> {name, image}, from Data Dragon summoner.json
// shortcut: stat shards are not in runesReforged.json, so their names are fixed here; update if Riot changes the shards.
const SHARD_NAMES = {
    5001: "Health Scaling", 5005: "Attack Speed", 5007: "Ability Haste", 5008: "Adaptive Force",
    5010: "Move Speed", 5011: "Health", 5013: "Tenacity and Slow Resist",
};

// --- URL GENERATOR ---
function getOpGgUrl(region, riotId) {
    if (!riotId || !riotId.includes('#')) return '#';
    const regionMap = { 'kr': 'kr', 'euw1': 'euw', 'na1': 'na', 'br1': 'br', 'eun1': 'eune' };
    const opGgRegion = regionMap[region] || 'kr';
    const [name, tag] = riotId.split('#');
    return `https://www.op.gg/summoners/${opGgRegion}/${encodeURIComponent(name)}-${encodeURIComponent(tag)}`;
}

// --- MODAL & UI LOGIC ---
function toggleModal() { document.getElementById('about-modal').classList.toggle('show'); }
document.addEventListener('keydown', (e) => {
    if (e.key === "Escape") { document.getElementById('about-modal').classList.remove('show'); hideTip(); }
});

function setRole(role) {
    currentRole = role;
    document.querySelectorAll('.role-btn').forEach(btn => {
        btn.classList.remove('active');
        if(btn.getAttribute('onclick').includes(role)) btn.classList.add('active');
    });
    refreshTables();
}

function refreshTables() {
    renderTable('kr');
    renderTable('euw1');
}

// --- CHAMPION VIEW ---
const PLAYERS_SHOWN = 8; // rows before "Show all"
const LOW_SAMPLE = 10; // below this many games a row is dimmed and its tooltip says so
const ROLE_NAMES = { TOP: 'Top', JUNGLE: 'Jungle', MIDDLE: 'Mid', BOTTOM: 'Bot', UTILITY: 'Support' };
const REGION_NAMES = { kr: 'KR', euw1: 'EUW' };

// Names from Riot (players) and Data Dragon end up in innerHTML, so they are escaped.
function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
}

// Tooltip content: a title, what it is (from Data Dragon), the numbers, and how they are counted.
function tipHtml({ title, about, lines = [], note, games }) {
    let html = `<div class="cv-tip-title">${esc(title)}</div>`;
    if (about) html += `<div class="cv-tip-about">${esc(about)}</div>`;
    lines.forEach(l => { html += `<div class="cv-tip-line">${esc(l)}</div>`; });
    if (games !== undefined && games < LOW_SAMPLE) {
        html += `<div class="cv-tip-warn">Small sample: ${games} game${games === 1 ? '' : 's'}.</div>`;
    }
    if (note) html += `<div class="cv-tip-note">${esc(note)}</div>`;
    return html;
}

function withTip(e, tip) {
    e.dataset.tip = tipHtml(tip);
    e.tabIndex = 0; // focusable, so a tap shows the tooltip on a phone
    e.classList.add('has-tip');
    if (tip.games !== undefined && tip.games < LOW_SAMPLE) e.classList.add('cv-low');
    return e;
}

function showTip(target) {
    const tip = document.getElementById('cv-tip');
    tip.innerHTML = target.dataset.tip;
    tip.classList.add('show');
    const r = target.getBoundingClientRect();
    const t = tip.getBoundingClientRect();
    let left = r.left + r.width / 2 - t.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - t.width - 8));
    let top = r.bottom + 8;
    if (top + t.height > window.innerHeight - 8) top = Math.max(8, r.top - t.height - 8);
    tip.style.left = `${left + window.scrollX}px`;
    tip.style.top = `${top + window.scrollY}px`;
}

function hideTip() { document.getElementById('cv-tip').classList.remove('show'); }

['mouseover', 'focusin'].forEach(evt => document.addEventListener(evt, e => {
    const t = e.target.closest && e.target.closest('.has-tip');
    if (t) showTip(t);
}));
['mouseout', 'focusout'].forEach(evt => document.addEventListener(evt, e => {
    if (e.target.closest && e.target.closest('.has-tip')) hideTip();
}));
// The tip is placed once in page coordinates, so scrolling (any scroller, hence capture) or resizing would strand it.
document.addEventListener('scroll', hideTip, { capture: true, passive: true });
window.addEventListener('resize', hideTip);

function pct(games, total) { return total ? Math.round((games / total) * 1000) / 10 : 0; }

function item(id) {
    const d = itemInfo[id] || {};
    return {
        name: d.name || `Item ${id}`,
        about: [d.cost ? `${d.cost} gold` : '', d.text || ''].filter(Boolean).join(' · '),
        src: `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/item/${id}.png`,
    };
}

function rune(id) {
    const r = runeInfo[id];
    return r
        ? { name: r.name, about: r.desc || '', src: `https://ddragon.leagueoflegends.com/cdn/img/${r.icon}` }
        : { name: `Rune ${id}`, about: '', src: '' };
}

function spell(id) {
    const s = spellInfo[id];
    return s
        ? { name: s.name, about: s.desc || '', src: `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/spell/${s.image}` }
        : { name: `Spell ${id}`, about: '', src: '' };
}

function icon(info, cls) {
    const img = el('img', cls);
    img.src = info.src;
    img.alt = info.name;
    return img;
}

function bar(share) {
    const b = el('span', 'cv-bar');
    const fill = el('span', 'cv-bar-fill');
    fill.style.width = `${Math.min(100, share)}%`;
    b.appendChild(fill);
    return b;
}

function noData(container, text) {
    container.innerHTML = '';
    container.appendChild(el('span', 'cv-empty', text));
}

function wrClass(wr) { return wr >= 52 ? 'win-high' : (wr < 48 ? 'win-low' : ''); }

function openDetailView(champData, regionKey) {
    document.getElementById('dashboard-view').classList.add('hidden');
    document.getElementById('role-nav').classList.add('hidden');
    document.getElementById('detail-view').classList.remove('hidden');
    window.scrollTo(0, 0);

    const champName = champData.name;
    document.getElementById('detail-name').innerText = champName;
    document.getElementById('detail-img').src = `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/champion/${champName}.png`;
    const patch = (globalData.meta && globalData.meta.current_patch) || '';
    document.getElementById('detail-sub').innerText =
        [ROLE_NAMES[currentRole] || currentRole, REGION_NAMES[regionKey] || '', patch && `Patch ${patch}`].filter(Boolean).join(' · ');

    renderStats(document.getElementById('detail-stats'), champData, regionKey);
    renderRunes(document.getElementById('detail-runes'), champData);
    renderOrder(document.getElementById('detail-order'), champData);
    renderItemShares(document.getElementById('detail-items'), champData.items, champData.item_games);
    renderItemShares(document.getElementById('detail-boots'), champData.boots, champData.item_games);
    renderSpells(document.getElementById('detail-spells'), champData);
    renderSkills(document.getElementById('detail-skills'), champData);
    renderMatchups(document.getElementById('context-builds'), champData.matchups);
    renderPlayers(champName);
}

function renderStats(container, champ, regionKey) {
    container.innerHTML = '';
    const players = (globalData.meta && globalData.meta.player_count) || 20;
    const region = REGION_NAMES[regionKey] || 'this region';
    const role = (ROLE_NAMES[currentRole] || currentRole).toLowerCase();
    const stats = [
        ['Win', `${champ.win_rate}%`, champ.count >= LOW_SAMPLE ? wrClass(champ.win_rate) : '', {
            title: 'Win rate', games: champ.count,
            lines: [`${champ.win_rate}% of ${champ.count} games won.`],
            note: `All ${role} games on this champion in the tracked matches, either team.`,
        }],
        ['Pick', `${champ.pick_rate}%`, '', {
            title: 'Pick rate',
            lines: [`Played in ${champ.pick_rate}% of ${role} slots (${champ.count} games).`],
            note: 'A match has two slots per role, one on each team, so a role\'s pick rates add up to about 100%.',
        }],
        ['Ban', `${champ.ban_rate}%`, champ.ban_rate >= 20 ? 'ban-high' : '', {
            title: 'Ban rate',
            lines: [`Banned in ${champ.ban_rate}% of matches.`],
            note: 'Bans are not tied to a role: this is the share of all tracked matches in the region.',
        }],
        ['KDA', `${champ.kda}`, champ.kda >= 3 ? 'kda-great' : '', {
            title: 'KDA',
            lines: [`${champ.kda} = (kills + assists) / deaths, summed over ${champ.count} games.`],
        }],
        ['Games', `${champ.count}`, '', {
            title: 'Games', games: champ.count,
            lines: [`${champ.count} games in the ranked matches of the top ${players} players in ${region}, this patch.`],
            note: 'Every player in those matches counts, not only the tracked ones.',
        }],
    ];
    stats.forEach(([label, value, cls, tip]) => {
        const s = withTip(el('div', 'cv-stat'), tip);
        s.append(el('span', `cv-stat-value ${cls}`, value), el('span', 'cv-stat-label', label));
        container.appendChild(s);
    });
}

// A tile per item: icon, share of games, and a bar. Name, cost and counts are in the tooltip.
function renderItemShares(container, items, count) {
    container.innerHTML = '';
    if (!items || items.length === 0) { noData(container, 'No item data yet.'); return; }
    items.forEach(it => {
        const info = item(it.id);
        const tile = withTip(el('div', 'cv-tile'), {
            title: info.name, about: info.about, games: it.games,
            lines: [`In the final inventory in ${it.games} of ${count} games (${it.share}%).`],
            note: 'Counted at the end of the game, so it says how often the item is built, not when.',
        });
        tile.append(icon(info, 'cv-icon'), el('span', 'cv-tile-share', `${Math.round(it.share)}%`), bar(it.share));
        container.appendChild(tile);
    });
}

function renderRunes(container, champ) {
    container.innerHTML = '';
    const n = champ.rune_games;
    if (!n || !champ.runes || champ.runes.length === 0) { noData(container, 'No rune data yet.'); return; }

    const top = champ.runes[0];
    const pageTip = page => ({
        title: `${rune(page.keystone).name}: ${rune(page.primary_style).name} + ${rune(page.secondary_style).name}`,
        games: page.games,
        lines: [`${page.games} of ${n} games (${page.share}%), ${page.win_rate}% won.`],
        note: 'A page is the keystone with its primary and secondary tree; the smaller runes may differ between games.',
    });

    const main = el('div', 'cv-rune-main');
    const key = rune(top.keystone);
    main.appendChild(withTip(icon(key, 'cv-keystone'), { title: key.name, about: key.about, lines: ['Keystone of the most used page.'] }));
    const picks = el('div', 'cv-rune-picks');
    const sel = top.selection;
    if (sel) {
        const selNote = `Most used exact selection within this page: ${sel.games} of ${top.games} games.`;
        const row = (ids, style) => {
            const r = el('div', 'cv-rune-row');
            r.appendChild(withTip(icon(rune(style), 'cv-tree'), { title: `${rune(style).name} tree` }));
            ids.forEach(id => {
                const info = rune(id);
                r.appendChild(withTip(icon(info, 'cv-rune'), { title: info.name, about: info.about, games: sel.games, lines: [selNote] }));
            });
            return r;
        };
        picks.append(row(sel.primary, top.primary_style), row(sel.secondary, top.secondary_style));
        const shards = withTip(el('div', 'cv-shards', sel.shards.map(id => SHARD_NAMES[id] || `Shard ${id}`).join(' · ')),
            { title: 'Stat shards', games: sel.games, lines: [selNote] });
        picks.appendChild(shards);
    }
    main.appendChild(picks);
    const fig = withTip(el('div', 'cv-figure'), pageTip(top));
    fig.append(el('span', 'cv-figure-value', `${Math.round(top.share)}%`),
        el('span', `cv-figure-sub ${top.games >= LOW_SAMPLE ? wrClass(top.win_rate) : ''}`, `${top.win_rate}% WR`));
    main.appendChild(fig);
    container.appendChild(main);

    champ.runes.slice(1).forEach(page => {
        const row = withTip(el('div', 'cv-row'), pageTip(page));
        row.append(icon(rune(page.keystone), 'cv-rune'), icon(rune(page.primary_style), 'cv-tree'),
            icon(rune(page.secondary_style), 'cv-tree'), el('span', 'cv-row-name', rune(page.keystone).name),
            bar(page.share), el('span', 'cv-num', `${Math.round(page.share)}%`),
            el('span', `cv-num ${page.games >= LOW_SAMPLE ? wrClass(page.win_rate) : ''}`, `${Math.round(page.win_rate)}% WR`));
        container.appendChild(row);
    });
}

function renderOrder(container, champ) {
    container.innerHTML = '';
    const n = champ.timeline_games;
    if (!n) { noData(container, 'No timeline data yet.'); return; }

    (champ.paths || []).forEach(path => {
        const infos = path.ids.map(item);
        const row = withTip(el('div', 'cv-row'), {
            title: infos.map(i => i.name).join(' → '), games: path.games,
            lines: [`Bought in this order in ${path.games} of ${n} games (${pct(path.games, n)}%).`],
            note: 'The first three finished items by purchase time, boots left out. Exact sequences split the games into many small groups, so these shares are low by nature.',
        });
        infos.forEach((info, k) => {
            if (k) row.appendChild(el('span', 'cv-arrow', '›'));
            row.appendChild(icon(info, 'cv-icon'));
        });
        row.append(el('span', 'cv-spacer'), el('span', 'cv-num', `${path.games} games`));
        container.appendChild(row);
    });

    if (champ.first_items && champ.first_items.length) {
        container.appendChild(el('div', 'cv-subhead', 'First item'));
        champ.first_items.forEach(f => {
            const info = item(f.id);
            const row = withTip(el('div', 'cv-row'), {
                title: info.name, about: info.about, games: f.games,
                lines: [`First finished item in ${f.games} of ${n} games (${f.share}%).`, `${f.win_rate}% of those games won.`],
                note: 'Boots are not counted as a first item.',
            });
            row.append(icon(info, 'cv-icon-sm'), el('span', 'cv-row-name', info.name), bar(f.share),
                el('span', 'cv-num', `${Math.round(f.share)}%`),
                el('span', `cv-num ${f.games >= LOW_SAMPLE ? wrClass(f.win_rate) : ''}`, `${Math.round(f.win_rate)}% WR`));
            container.appendChild(row);
        });
    }

    const b = champ.boots_position;
    if (b) {
        const info = item(b.id);
        const when = b.median_before === 0 ? 'before the first item' : `after ${b.median_before} item${b.median_before === 1 ? '' : 's'}`;
        const row = withTip(el('div', 'cv-row cv-chip-row'), {
            title: `${info.name}: bought ${when}`, about: info.about, games: b.games,
            lines: [`Median number of finished items bought before these boots, over ${b.games} games.`],
        });
        row.append(icon(info, 'cv-icon-sm'), el('span', 'cv-row-name', `Boots ${when}`));
        container.appendChild(row);
    }
}

function renderSpells(container, champ) {
    container.innerHTML = '';
    if (!champ.spells || champ.spells.length === 0) { noData(container, 'No summoner spell data yet.'); return; }
    champ.spells.forEach(pair => {
        const infos = pair.ids.map(spell);
        const row = withTip(el('div', 'cv-row'), {
            title: infos.map(i => i.name).join(' + '), games: pair.games,
            about: infos.map(i => `${i.name}: ${i.about}`).join(' '),
            lines: [`${pair.games} of ${champ.rune_games} games (${pair.share}%).`],
        });
        infos.forEach(info => row.appendChild(icon(info, 'cv-icon-sm')));
        row.append(el('span', 'cv-row-name', infos.map(i => i.name).join(' + ')), bar(pair.share),
            el('span', 'cv-num', `${Math.round(pair.share)}%`));
        container.appendChild(row);
    });
}

function renderSkills(container, champ) {
    container.innerHTML = '';
    const n = champ.timeline_games;
    if (!n || (!champ.skill_start && !champ.skill_max)) { noData(container, 'No skill order data yet.'); return; }
    const line = (label, data, sep, tip) => {
        const row = withTip(el('div', 'cv-row'), tip);
        row.appendChild(el('span', 'cv-skill-label', label));
        data.order.forEach((k, i) => {
            if (i) row.appendChild(el('span', 'cv-arrow', sep));
            row.appendChild(el('span', `cv-key cv-key-${k.toLowerCase()}`, k));
        });
        row.append(el('span', 'cv-spacer'), el('span', 'cv-num', `${Math.round(pct(data.games, n))}%`));
        container.appendChild(row);
    };
    if (champ.skill_max) {
        line('Max', champ.skill_max, '›', {
            title: `Max order: ${champ.skill_max.order.join(' › ')}`, games: champ.skill_max.games,
            lines: [`${champ.skill_max.games} of ${n} games with a timeline.`],
            note: 'The order in which Q, W and E reach rank 5. Games where fewer than two of them get there are left out.',
        });
    }
    if (champ.skill_start) {
        line('Lv 1–3', champ.skill_start, ' ', {
            title: `First three levels: ${champ.skill_start.order.join(', ')}`, games: champ.skill_start.games,
            lines: [`${champ.skill_start.games} of ${n} games with a timeline.`],
        });
    }
}

// Only items picked noticeably more or less often against that kind of enemy team are listed.
function renderMatchups(container, matchups) {
    container.innerHTML = '';
    const entries = Object.entries(matchups || {});
    document.getElementById('panel-matchups').classList.toggle('hidden', entries.length === 0);
    entries.forEach(([tag, card]) => {
        container.appendChild(el('div', 'cv-subhead', `vs ${tag} · ${card.games} games`));
        card.items.forEach(it => {
            const info = item(it.id);
            const diff = Math.round(it.share - it.other_share);
            const row = withTip(el('div', 'cv-row'), {
                title: `${info.name} vs ${tag}`, about: info.about, games: card.games,
                lines: [`Against ${tag}: ${it.games} of ${card.games} games (${it.share}%).`,
                    `In the other games: ${it.other_games} of ${card.other_games} (${it.other_share}%).`],
                note: 'The enemy type comes from the enemy champions, not from how the game went. Only differences of 20 points or more are shown.',
            });
            row.append(icon(info, 'cv-icon-sm'), el('span', 'cv-row-name', info.name),
                el('span', `cv-num ${diff > 0 ? 'win-high' : 'win-low'}`, `${diff > 0 ? '+' : ''}${diff} pts`));
            container.appendChild(row);
        });
    });
}

function renderPlayers(champName) {
    const tbody = document.querySelector('#table-players tbody');
    const more = document.getElementById('players-more');
    tbody.innerHTML = '';
    more.classList.add('hidden');

    const players = (globalData.leaderboards && globalData.leaderboards[`${currentRole}:${champName}`]) || [];
    document.getElementById('players-header').textContent = `PLAYERS (${players.length})`;
    if (players.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:30px; color:#666;">No individual player data found.</td></tr>';
        return;
    }

    const draw = list => {
        tbody.innerHTML = list.map(p => {
            const low = p.games < LOW_SAMPLE;
            const tip = tipHtml({
                title: p.player, games: p.games,
                lines: [`${p.games} games on this champion in this role, ${p.win_rate}% won, KDA ${p.kda}.`],
            });
            return `
            <tr class="${low ? 'has-tip cv-low' : ''}" ${low ? `tabindex="0" data-tip="${esc(tip)}"` : ''}>
                <td>
                    <div class="player-cell">
                        <span style="font-weight:bold; color:#fff;">${esc(p.player)}</span>
                        <a href="${esc(getOpGgUrl(p.region, p.player))}" target="_blank" rel="noopener" class="opgg-link" title="View on OP.GG">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
                        </a>
                    </div>
                </td>
                <td><span class="region-badge">${esc(p.region.toUpperCase())}</span></td>
                <td>${p.games}</td>
                <td class="stat-cell ${low ? '' : (p.win_rate >= 55 ? 'win-high' : (p.win_rate < 45 ? 'win-low' : ''))}">${p.win_rate}%</td>
                <td class="stat-cell ${p.kda >= 3.5 && !low ? 'kda-great' : ''}">${p.kda}</td>
            </tr>`;
        }).join('');
    };

    draw(players.slice(0, PLAYERS_SHOWN));
    if (players.length > PLAYERS_SHOWN) {
        more.textContent = `Show all ${players.length} players`;
        more.classList.remove('hidden');
        more.onclick = () => { draw(players); more.classList.add('hidden'); };
    }
}

function stripTags(html) { return String(html || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim(); }

function loadRunesAndSpells() {
    fetch(`https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/data/en_US/runesReforged.json`)
        .then(res => res.json())
        .then(styles => {
            for (const style of styles) {
                runeInfo[style.id] = { name: style.name, icon: style.icon };
                for (const slot of style.slots) {
                    for (const r of slot.runes) runeInfo[r.id] = { name: r.name, icon: r.icon, desc: stripTags(r.shortDesc) };
                }
            }
        })
        .catch(err => console.error("Runes:", err));
    fetch(`https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/data/en_US/summoner.json`)
        .then(res => res.json())
        .then(json => {
            for (const d of Object.values(json.data)) {
                spellInfo[d.key] = { name: d.name, image: d.image.full, desc: stripTags(d.description) };
            }
        })
        .catch(err => console.error("Summoner spells:", err));
}

function loadItemNames() {
    fetch(`https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/data/en_US/item.json`)
        .then(res => res.json())
        .then(json => {
            for (const [id, d] of Object.entries(json.data)) {
                const text = d.plaintext || stripTags(d.description);
                itemInfo[id] = { name: d.name, text: text.length > 180 ? `${text.slice(0, 177)}…` : text, cost: d.gold && d.gold.total };
            }
        })
        .catch(err => console.error("Item names:", err));
}

function closeDetailView() {
    document.getElementById('dashboard-view').classList.remove('hidden');
    document.getElementById('role-nav').classList.remove('hidden');
    document.getElementById('detail-view').classList.add('hidden');
    hideTip();
}

// --- CORE RENDER ---
function renderTable(regionKey) {
    const table = document.getElementById(`table-${regionKey}`);
    if (!table) return;
    const tbody = table.querySelector('tbody');
    tbody.innerHTML = '';

    if (!globalData.regions || !globalData.regions[regionKey]) return;
    const data = globalData.regions[regionKey][currentRole];
    if (!data) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:30px; color:#666;">No data found for ${currentRole}.</td></tr>`;
        return;
    } 

    if (data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:30px; color:#666;">No games recorded yet.</td></tr>';
        return;
    }

    data.forEach((champ, index) => {
        let wrClass = champ.win_rate >= 52 ? 'win-high' : (champ.win_rate < 48 ? 'win-low' : '');
        let banClass = champ.ban_rate >= 20 ? 'ban-high' : '';
        let kdaClass = champ.kda >= 3.0 ? 'kda-great' : '';

        const tr = document.createElement('tr');
        // KEY CHANGE: Passing the full 'champ' object, not just the name
        tr.onclick = () => openDetailView(champ, regionKey);
        
        tr.innerHTML = `
            <td style="color:#666; font-weight:bold; text-align:center;">${index + 1}</td>
            <td>
                <div class="champ-flex">
                    <img src="https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/champion/${esc(champ.name)}.png" 
                         class="champ-img" onerror="this.src='https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/profileicon/29.png'">
                    <span>${esc(champ.name)}</span>
                </div>
            </td>
            <td class="stat-cell">${champ.pick_rate}%</td>
            <td class="stat-cell ${banClass}">${champ.ban_rate}%</td>
            <td class="stat-cell ${wrClass}">${champ.win_rate}%</td>
            <td class="stat-cell ${kdaClass}">${champ.kda}</td>
        `;
        tbody.appendChild(tr);
    });
}

function updateMetaInfo() {
    if (!globalData.meta) return;
    const totalGames = (globalData.meta.total_games || 0).toLocaleString();
    document.getElementById('meta-games').innerText = totalGames;
    document.getElementById('meta-patch').innerText = globalData.meta.current_patch || "--";
    document.getElementById('meta-updated').innerText = globalData.meta.last_updated || "--";

    // Update Data Dragon version from backend
    if (globalData.meta.ddragon_version) {
        DDRAGON_VER = globalData.meta.ddragon_version;
    }

    // Update subtitle with actual player count
    const playerCount = globalData.meta.player_count || 20;
    const subtitle = document.querySelector('.subtitle');
    if (subtitle) {
        subtitle.innerText = `Tracking the Top ${playerCount} Challengers in KR & EUW`;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const isProduction = window.location.hostname.includes('github.io');
    const dataUrl = isProduction ? 'data/data.json' : '../data/data.json';

    fetch(dataUrl)
        .then(res => res.json())
        .then(data => {
            globalData = data;
            updateMetaInfo();
            refreshTables();
            loadItemNames();
            loadRunesAndSpells();
        })
        .catch(err => console.error("Error:", err));
});

if (typeof module !== 'undefined') {
    module.exports = { esc, tipHtml, renderRunes, LOW_SAMPLE };
}
