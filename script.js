let globalData = {};
let currentRole = 'JUNGLE';
let DDRAGON_VER = "14.23.1"; // Will be updated from data.json 

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

    // 3. Populate Core Items
    const itemContainer = document.getElementById('detail-items');
    itemContainer.innerHTML = '';
    
    if (champData.top_items && champData.top_items.length > 0) {
        champData.top_items.forEach(itemId => {
            const img = document.createElement('img');
            img.src = `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/item/${itemId}.png`;
            img.className = 'item-icon';
            img.title = `Item ID: ${itemId}`;
            itemContainer.appendChild(img);
        });
    } else {
        itemContainer.innerHTML = '<span style="color:#666; font-size:0.8rem;">No build data</span>';
    }

    // --- NEW: POPULATE CONTEXT BUILDS ---
    const contextContainer = document.getElementById('context-builds');
    contextContainer.innerHTML = ''; // Clear previous

    if (champData.context_builds && Object.keys(champData.context_builds).length > 0) {
        // Define color classes for specific tags
        const tagMap = {
            "Heavy AD": "ctx-ad",
            "Heavy AP": "ctx-ap",
            "Tank Heavy": "ctx-tank"
        };

        for (const [tag, data] of Object.entries(champData.context_builds)) {
            // Only show if we have items
            if (!data.items || data.items.length === 0) continue;

            const colorClass = tagMap[tag] || "ctx-default";
            
            // Create Card HTML
            const card = document.createElement('div');
            card.className = `context-card ${colorClass}`;
            
            // Build Item Images HTML
            const itemsHtml = data.items.map(id => 
                `<img src="https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/item/${id}.png" class="small-item-icon" title="${tag} Build">`
            ).join('');

            card.innerHTML = `
                <span class="context-label">
                    VS ${tag}
                    <span class="context-games">${data.games} Games</span>
                </span>
                <div class="small-item-row">
                    ${itemsHtml}
                </div>
            `;
            contextContainer.appendChild(card);
        }
    }
    // -------------------------------------

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
        })
        .catch(err => console.error("Error:", err));
});