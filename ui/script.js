let globalData = {};
let currentRole = 'JUNGLE'; 
let currentTime = 'patch';
const DDRAGON_VER = "14.23.1"; 

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

function setTime(time) {
    currentTime = time;
    document.querySelectorAll('.controls button').forEach(btn => btn.classList.remove('active'));
    document.getElementById(`btn-${time}`).classList.add('active');
    refreshTables();
}

function refreshTables() {
    renderTable('kr');
    renderTable('euw1');
}

// --- NEW: DETAIL VIEW LOGIC ---
function openDetailView(champName) {
    // 1. Hide Dashboard, Show Detail
    document.getElementById('dashboard-view').classList.add('hidden');
    document.getElementById('role-nav').classList.add('hidden');
    document.getElementById('time-controls').classList.add('hidden');
    document.getElementById('detail-view').classList.remove('hidden');

    // 2. Populate Header
    document.getElementById('detail-name').innerText = champName;
    document.getElementById('detail-img').src = `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/champion/${champName}.png`;

    // 3. Populate Table
    const tbody = document.querySelector('#table-players tbody');
    tbody.innerHTML = '';

    const players = globalData.leaderboards ? globalData.leaderboards[champName] : [];

    if (!players || players.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:30px; color:#666;">No individual player data found.</td></tr>';
        return;
    }

    players.forEach(p => {
        let wrClass = p.win_rate >= 55 ? 'win-high' : (p.win_rate < 45 ? 'win-low' : '');
        let kdaClass = p.kda >= 3.5 ? 'kda-great' : '';

        const html = `
            <tr>
                <td style="font-weight:bold; color:#fff;">${p.player}</td>
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
    document.getElementById('time-controls').classList.remove('hidden');
    document.getElementById('detail-view').classList.add('hidden');
}

// --- CORE RENDER ---
function renderTable(regionKey) {
    const table = document.getElementById(`table-${regionKey}`);
    if (!table) return;
    const tbody = table.querySelector('tbody');
    tbody.innerHTML = '';

    if (!globalData.regions || !globalData.regions[regionKey]) return;
    const timeData = globalData.regions[regionKey][currentTime];
    if (!timeData || !timeData[currentRole]) return;
    const data = timeData[currentRole]; 

    if (data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:30px; color:#666;">No games recorded.</td></tr>';
        return;
    }

    data.forEach((champ, index) => {
        let wrClass = champ.win_rate >= 52 ? 'win-high' : (champ.win_rate < 48 ? 'win-low' : '');
        let banClass = champ.ban_rate >= 20 ? 'ban-high' : '';
        let kdaClass = champ.kda >= 3.0 ? 'kda-great' : '';

        const tr = document.createElement('tr');
        // ADD CLICK LISTENER
        tr.onclick = () => openDetailView(champ.name);
        
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
    const patchGames = (globalData.meta.patch_games || 0).toLocaleString();
    const totalGames = (globalData.meta.total_games || 0).toLocaleString();
    document.getElementById('meta-games').innerText = totalGames;
    document.getElementById('meta-patch').innerText = globalData.meta.current_patch || "--";
    document.getElementById('meta-updated').innerText = globalData.meta.last_updated || "--";
    document.getElementById('btn-patch').innerText = `Current Patch (${patchGames})`;
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