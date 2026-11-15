let globalData = {};
let currentRole = 'JUNGLE';
let DDRAGON_VER = "14.23.1"; // Will be updated from data.json 
let itemNames = {}; // id -> name, from Data Dragon item.json
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
document.addEventListener('keydown', (e) => { if (e.key === "Escape") document.getElementById('about-modal').classList.remove('show'); });

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

// --- NEW: DETAIL VIEW WITH ITEMS ---
function openDetailView(champData) {
    // 1. Hide Dashboard, Show Detail
    document.getElementById('dashboard-view').classList.add('hidden');
    document.getElementById('role-nav').classList.add('hidden');
    document.getElementById('detail-view').classList.remove('hidden');

    // 2. Populate Header Info
    const champName = champData.name;
    document.getElementById('detail-name').innerText = champName;
    document.getElementById('detail-img').src = `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/champion/${champName}.png`;

    // 3. Populate Items and Boots
    renderItemShares(document.getElementById('detail-items'), champData.items, champData.item_games);
    renderItemShares(document.getElementById('detail-boots'), champData.boots, champData.item_games);
    renderRunes(document.getElementById('detail-runes'), champData);
    renderSpells(document.getElementById('detail-spells'), champData);
    renderOrder(document.getElementById('detail-order'), champData);
    renderSkills(document.getElementById('detail-skills'), champData);

    renderMatchups(document.getElementById('context-builds'), champData.matchups);

    // 4. Populate Player Table (now filtered by role)
    const tbody = document.querySelector('#table-players tbody');
    tbody.innerHTML = '';

    const leaderboardKey = `${currentRole}:${champName}`;
    const players = globalData.leaderboards ? globalData.leaderboards[leaderboardKey] : [];

    if (!players || players.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:30px; color:#666;">No individual player data found.</td></tr>';
        return;
    }

    players.forEach(p => {
        let wrClass = p.win_rate >= 55 ? 'win-high' : (p.win_rate < 45 ? 'win-low' : '');
        let kdaClass = p.kda >= 3.5 ? 'kda-great' : '';
        const profileUrl = getOpGgUrl(p.region, p.player);

        const html = `
            <tr>
                <td>
                    <div class="player-cell">
                        <span style="font-weight:bold; color:#fff;">${p.player}</span>
                        <a href="${profileUrl}" target="_blank" class="opgg-link" title="View on OP.GG">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
                        </a>
                    </div>
                </td>
                <td><span class="region-badge">${p.region.toUpperCase()}</span></td>
                <td>${p.games}</td>
                <td class="stat-cell ${wrClass}">${p.win_rate}%</td>
                <td class="stat-cell ${kdaClass}">${p.kda}</td>
            </tr>
        `;
        tbody.insertAdjacentHTML('beforeend', html);
    });
}

// Each item shows its share; the game count shows on hover, or on tap via focus.
function renderItemShares(container, items, count) {
    container.innerHTML = '';
    if (!items || items.length === 0) {
        container.innerHTML = '<span style="color:#666; font-size:0.8rem;">No item data</span>';
        return;
    }
    items.forEach(item => {
        const name = itemNames[item.id] || `Item ${item.id}`;
        const cell = document.createElement('figure');
        cell.className = 'item-cell';
        cell.tabIndex = 0;
        cell.title = `${name}: in ${item.games} of ${count} games`;

        const img = document.createElement('img');
        img.src = `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/item/${item.id}.png`;
        img.className = 'item-icon';
        img.alt = name;

        const caption = document.createElement('figcaption');
        caption.textContent = `${item.share}%`;
        const games = document.createElement('span');
        games.className = 'item-games';
        games.textContent = `${item.games}/${count}`;
        caption.appendChild(games);

        cell.append(img, caption);
        container.appendChild(cell);
    });
}

// A card lists only the items picked noticeably more or less often against that enemy type.
function renderMatchups(container, matchups) {
    container.innerHTML = '';
    const tagMap = { "Heavy AD": "ctx-ad", "Heavy AP": "ctx-ap", "Tank Heavy": "ctx-tank" };
    for (const [tag, card] of Object.entries(matchups || {})) {
        const el = document.createElement('div');
        el.className = `context-card ${tagMap[tag] || "ctx-default"}`;

        const label = document.createElement('span');
        label.className = 'context-label';
        label.textContent = `VS ${tag}`;
        const games = document.createElement('span');
        games.className = 'context-games';
        games.textContent = `${card.games} vs ${card.other_games} other games`;
        label.appendChild(games);
        el.appendChild(label);

        card.items.forEach(item => {
            const name = itemNames[item.id] || `Item ${item.id}`;
            const row = document.createElement('div');
            row.className = 'matchup-item';
            row.tabIndex = 0;
            row.title = `${name}: in ${item.games} of ${card.games} games vs ${tag}, ` +
                `${item.other_games} of ${card.other_games} other games`;

            const img = document.createElement('img');
            img.src = `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/item/${item.id}.png`;
            img.className = 'small-item-icon';
            img.alt = name;
            const text = document.createElement('span');
            text.textContent = `${item.share}% vs ${item.other_share}% in other games`;

            row.append(img, text);
            el.appendChild(row);
        });
        container.appendChild(el);
    }
}

function iconRow(icons, text) {
    const row = document.createElement('div');
    row.className = 'matchup-item';
    icons.forEach(({ src, name }) => {
        const img = document.createElement('img');
        img.src = src;
        img.className = 'small-item-icon';
        img.alt = name;
        img.title = name;
        row.appendChild(img);
    });
    const span = document.createElement('span');
    span.textContent = text;
    row.appendChild(span);
    return row;
}

function noData(container, text) {
    container.innerHTML = `<span style="color:#666; font-size:0.8rem;">${text}</span>`;
}

function rune(id) {
    const r = runeInfo[id];
    return r
        ? { name: r.name, src: `https://ddragon.leagueoflegends.com/cdn/img/${r.icon}` }
        : { name: `Rune ${id}`, src: '' };
}

function renderRunes(container, champ) {
    container.innerHTML = '';
    if (!champ.rune_games || !champ.runes || champ.runes.length === 0) {
        noData(container, 'No rune data for this champion yet.');
        return;
    }
    champ.runes.forEach(page => {
        const icons = [rune(page.keystone), rune(page.primary_style), rune(page.secondary_style)];
        container.appendChild(iconRow(icons,
            `${icons[0].name}, ${icons[1].name} + ${icons[2].name}: ${page.share}% ` +
            `(${page.games} of ${champ.rune_games} games), ${page.win_rate}% win rate`));
    });
    const sel = champ.runes[0].selection;
    if (sel) {
        const shards = sel.shards.map(id => SHARD_NAMES[id] || `Shard ${id}`).join(', ');
        container.appendChild(iconRow([...sel.primary, ...sel.secondary].map(rune),
            `Most common full page of the first: ${sel.games} of ${champ.runes[0].games} games. ` +
            `Shards: ${shards}`));
    }
}

function renderSpells(container, champ) {
    container.innerHTML = '';
    if (!champ.spells || champ.spells.length === 0) {
        noData(container, 'No summoner spell data for this champion yet.');
        return;
    }
    champ.spells.forEach(pair => {
        const icons = pair.ids.map(id => {
            const s = spellInfo[id];
            return s
                ? { name: s.name, src: `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/spell/${s.image}` }
                : { name: `Spell ${id}`, src: '' };
        });
        container.appendChild(iconRow(icons,
            `${icons.map(i => i.name).join(' + ')}: ${pair.share}% ` +
            `(${pair.games} of ${champ.rune_games} games)`));
    });
}

function item(id) {
    const name = itemNames[id] || `Item ${id}`;
    return { name, src: `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/item/${id}.png` };
}

function renderOrder(container, champ) {
    container.innerHTML = '';
    const n = champ.timeline_games;
    if (!n) {
        noData(container, 'No timeline data for this champion yet.');
        return;
    }
    (champ.paths || []).forEach(path => {
        const icons = path.ids.map(item);
        container.appendChild(iconRow(icons,
            `${icons.map((i, k) => `${k + 1}. ${i.name}`).join(' ')} (${path.games} of ${n} games)`));
    });
    (champ.first_items || []).forEach(f => {
        const i = item(f.id);
        container.appendChild(iconRow([i],
            `First item ${i.name}: ${f.share}% (${f.games} of ${n} games), ${f.win_rate}% win rate`));
    });
    const b = champ.boots_position;
    if (b) {
        const i = item(b.id);
        container.appendChild(iconRow([i],
            `${i.name}: bought after a median of ${b.median_before} finished items (${b.games} games)`));
    }
}

function renderSkills(container, champ) {
    container.innerHTML = '';
    const n = champ.timeline_games;
    if (!n || (!champ.skill_start && !champ.skill_max)) {
        noData(container, 'No timeline data for this champion yet.');
        return;
    }
    if (champ.skill_start) {
        container.appendChild(iconRow([], `First three levels: ${champ.skill_start.order.join(' > ')} ` +
            `(${champ.skill_start.games} of ${n} games)`));
    }
    if (champ.skill_max) {
        container.appendChild(iconRow([], `Max order: ${champ.skill_max.order.join(' > ')} ` +
            `(${champ.skill_max.games} of ${n} games)`));
    }
}

function loadRunesAndSpells() {
    fetch(`https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/data/en_US/runesReforged.json`)
        .then(res => res.json())
        .then(styles => {
            for (const style of styles) {
                runeInfo[style.id] = { name: style.name, icon: style.icon };
                for (const slot of style.slots) {
                    for (const r of slot.runes) runeInfo[r.id] = { name: r.name, icon: r.icon };
                }
            }
        })
        .catch(err => console.error("Runes:", err));
    fetch(`https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/data/en_US/summoner.json`)
        .then(res => res.json())
        .then(json => {
            for (const d of Object.values(json.data)) spellInfo[d.key] = { name: d.name, image: d.image.full };
        })
        .catch(err => console.error("Summoner spells:", err));
}

function loadItemNames() {
    fetch(`https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/data/en_US/item.json`)
        .then(res => res.json())
        .then(json => {
            for (const [id, d] of Object.entries(json.data)) itemNames[id] = d.name;
        })
        .catch(err => console.error("Item names:", err));
}

function closeDetailView() {
    document.getElementById('dashboard-view').classList.remove('hidden');
    document.getElementById('role-nav').classList.remove('hidden');
    document.getElementById('detail-view').classList.add('hidden');
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
        tr.onclick = () => openDetailView(champ); 
        
        tr.innerHTML = `
            <td style="color:#666; font-weight:bold; text-align:center;">${index + 1}</td>
            <td>
                <div class="champ-flex">
                    <img src="https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/champion/${champ.name}.png" 
                         class="champ-img" onerror="this.src='https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/profileicon/29.png'">
                    <span>${champ.name}</span>
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