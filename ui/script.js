let globalData = {};
let currentRole = 'JUNGLE'; // Default Role
let currentTime = 'patch';  // Default Timeframe
const DDRAGON_VER = "14.23.1"; 

// --- MODAL LOGIC ---
function toggleModal() {
    document.getElementById('about-modal').classList.toggle('show');
}
// Close on Escape key
document.addEventListener('keydown', (e) => {
    if (e.key === "Escape") document.getElementById('about-modal').classList.remove('show');
});

// --- CORE LOGIC ---
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

function renderTable(regionKey) {
    const table = document.getElementById(`table-${regionKey}`);
    if (!table) return;
    const tbody = table.querySelector('tbody');
    tbody.innerHTML = '';

    // Safety Checks
    if (!globalData.regions || !globalData.regions[regionKey]) return;
    
    // Drill down: Region -> Timeframe -> Role
    const timeData = globalData.regions[regionKey][currentTime];
    if (!timeData || !timeData[currentRole]) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:30px; color:#666;">No data found for ${currentRole}.</td></tr>`;
        return;
    }
    
    const data = timeData[currentRole]; 

    if (data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:30px; color:#666;">No games recorded yet.</td></tr>';
        return;
    }

    data.forEach((champ, index) => {
        let wrClass = champ.win_rate >= 52 ? 'win-high' : (champ.win_rate < 48 ? 'win-low' : '');
        let banClass = champ.ban_rate >= 20 ? 'ban-high' : '';
        let kdaClass = champ.kda >= 3.0 ? 'kda-great' : '';

        const html = `
            <tr>
                <td style="color:#666; font-weight:bold; text-align:center;">${index + 1}</td>
                <td>
                    <div class="champ-flex">
                        <img src="https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/champion/${champ.name}.png" 
                             class="champ-img" 
                             onerror="this.src='https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/profileicon/29.png'">
                        <span>${champ.name}</span>
                    </div>
                </td>
                <td class="stat-cell">${champ.pick_rate}%</td>
                <td class="stat-cell ${banClass}">${champ.ban_rate}%</td>
                <td class="stat-cell ${wrClass}">${champ.win_rate}%</td>
                <td class="stat-cell ${kdaClass}">${champ.kda}</td>
            </tr>
        `;
        tbody.insertAdjacentHTML('beforeend', html);
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
    // Detect environment to find data file
    const isProduction = window.location.hostname.includes('github.io');
    const dataUrl = isProduction ? 'data/data.json' : '../data/data.json';

    fetch(dataUrl)
        .then(res => res.json())
        .then(data => {
            globalData = data;
            updateMetaInfo();
            refreshTables(); // Initial Render
        })
        .catch(err => console.error("Error:", err));
});