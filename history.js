const TOP_N = 15;
let historyData = null;
let playerCount = null;
let currentRole = 'JUNGLE';
let currentRegion = 'kr';
let DDRAGON_VER = "14.23.1"; // Will be updated from data.json

function roleList(entry, region, role) {
    const r = entry.regions && entry.regions[region];
    return (r && r.roles && r.roles[role]) || [];
}

function topNames(entry, region, role) {
    return [...roleList(entry, region, role)]
        .sort((a, b) => b.pick_rate - a.pick_rate)
        .slice(0, TOP_N)
        .map(c => c.name);
}

// Pure: everything the page shows for one role and region.
function buildView(history, region, role) {
    const patches = [...history.patches].sort((a, b) => {
        const pa = a.patch.split('.').map(Number), pb = b.patch.split('.').map(Number);
        return pa[0] - pb[0] || pa[1] - pb[1];
    });
    const tops = patches.map(p => topNames(p, region, role));
    const inTop = {};
    tops.flat().forEach(n => { inTop[n] = (inTop[n] || 0) + 1; });

    const rows = Object.keys(inTop).map(name => {
        const cells = patches.map(p => roleList(p, region, role).find(c => c.name === name) || null);
        const listed = cells.filter(Boolean);
        // Mean over the patches where the champion is listed: unlisted is unknown, not 0.
        const mean = listed.reduce((s, c) => s + c.pick_rate, 0) / listed.length;
        return { name, cells, inTop: inTop[name], mean };
    }).sort((a, b) => b.inTop - a.inTop || b.mean - a.mean);

    const last = tops.length - 1;
    const prev = last > 0 ? tops[last - 1] : [];
    return {
        patches,
        rows,
        maxPick: Math.max(0, ...rows.flatMap(r => r.cells.filter(Boolean).map(c => c.pick_rate))),
        mostPicked: patches.map((p, i) => ({ patch: p.patch, name: tops[i][0] || null })),
        entered: last > 0 ? tops[last].filter(n => !prev.includes(n)) : [],
        left: last > 0 ? prev.filter(n => !tops[last].includes(n)) : [],
    };
}

function gamesLabel(entry, region) {
    const r = entry.regions && entry.regions[region];
    if (!r) return 'no games';
    return `${r.games_estimated ? '~' : ''}${r.games.toLocaleString()} games${r.games_estimated ? ' (estimate)' : ''}`;
}

function champImg(name) {
    return `<img src="https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/champion/${name}.png" class="champ-img" onerror="this.src='https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VER}/img/profileicon/29.png'">`;
}

function setRole(role) {
    currentRole = role;
    document.querySelectorAll('#role-nav .role-btn').forEach(b => b.classList.toggle('active', b.dataset.role === role));
    render();
}

function setRegion(region) {
    currentRegion = region;
    document.querySelectorAll('#region-nav button').forEach(b => b.classList.toggle('active', b.dataset.region === region));
    render();
}

function showCell(td) {
    document.getElementById('cell-detail').innerText = td.title;
}

function render() {
    const table = document.getElementById('history-grid');
    const summary = document.getElementById('history-summary');
    const note = document.getElementById('history-note');
    if (!historyData || !Array.isArray(historyData.patches) || historyData.patches.length === 0) {
        table.innerHTML = '';
        summary.innerHTML = '';
        note.innerText = 'There is no history yet.';
        return;
    }

    const v = buildView(historyData, currentRegion, currentRole);
    const backfilled = v.patches.some(p => p.complete === false);
    note.innerText = [
        `Tracks ${playerCount ?? 'the top'} players per region.`,
        backfilled
            ? `Patches marked * are backfilled: only the top ${TOP_N} champions per role are known, and only one side of each game was counted. An empty cell means the champion is not listed for that patch, not that nobody picked it.`
            : '',
        'Each column header shows the number of games behind that patch; ~ marks an estimate.',
    ].join(' ');

    const head = v.patches.map(p =>
        `<th title="Patch ${p.patch}: ${gamesLabel(p, currentRegion)}${p.complete === false ? ', backfilled' : ''}">${p.patch}${p.complete === false ? '*' : ''}<span class="patch-games">${gamesLabel(p, currentRegion).replace(' (estimate)', '')}</span></th>`
    ).join('');
    const body = v.rows.map(r => `<tr><td class="champ-col"><div class="champ-flex">${champImg(r.name)}<span>${r.name}</span></div></td>` +
        r.cells.map((c, i) => c
            ? `<td class="stat-cell heat" style="background: rgba(0,210,106,${(c.pick_rate / (v.maxPick || 1)).toFixed(2)})" title="${r.name}, patch ${v.patches[i].patch}: pick ${c.pick_rate}%, win ${c.win_rate}%, ban ${c.ban_rate}%, ${c.games} games" onclick="showCell(this)">${c.pick_rate}</td>`
            : '<td class="heat empty"></td>'
        ).join('') + '</tr>').join('');
    table.innerHTML = `<thead><tr><th class="align-left champ-col">Champion</th>${head}</tr></thead><tbody>${body || `<tr><td colspan="${v.patches.length + 1}">No champions recorded for this role.</td></tr>`}</tbody>`;
    document.getElementById('cell-detail').innerText = 'Pick rate in %. Hover or tap a cell for win rate, ban rate and games.';

    const latest = v.patches[v.patches.length - 1].patch;
    summary.innerHTML = `
        <div class="region-card summary-box">
            <div class="region-header">Most picked per patch</div>
            <ul>${v.mostPicked.map(m => `<li><span class="patch-label">${m.patch}</span> ${m.name ? `${champImg(m.name)} ${m.name}` : '–'}</li>`).join('')}</ul>
        </div>
        <div class="region-card summary-box">
            <div class="region-header">Top ${TOP_N} changes in ${latest}</div>
            <p><strong class="win-high">Entered:</strong> ${v.entered.join(', ') || 'none'}</p>
            <p><strong class="win-low">Left:</strong> ${v.left.join(', ') || 'none'}</p>
        </div>`;
}

if (typeof module !== 'undefined') {
    module.exports = { buildView };
} else {
    document.addEventListener('DOMContentLoaded', () => {
        const isProduction = window.location.hostname.includes('github.io');
        const base = isProduction ? 'data/' : '../data/';
        const json = f => fetch(base + f).then(res => (res.ok ? res.json() : null)).catch(() => null);

        Promise.all([json('data.json'), json('history.json')]).then(([data, history]) => {
            if (data && data.meta) {
                playerCount = data.meta.player_count ?? null;
                if (data.meta.ddragon_version) DDRAGON_VER = data.meta.ddragon_version;
            }
            historyData = history;
            render();
        });
    });
}
