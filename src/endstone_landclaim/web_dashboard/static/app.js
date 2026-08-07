/* ===================================================
   Land Claim Dashboard - JavaScript Application v2
   Updated for new card-based layout
   =================================================== */

// State
let claims = [];
let requests = [];
let playerMapping = {};
let currentEditKey = null;
let importData = null;
let currentRequestId = null;
let currentDimensionFilter = '';

// DOM Ready
document.addEventListener('DOMContentLoaded', () => {
    checkAuth();
    setupEventListeners();
});

// =====================================================
// Authentication
// =====================================================

async function checkAuth() {
    try {
        const response = await fetch('/auth/me');
        const user = await response.json();

        if (user && user.id) {
            showDashboard(user);
        } else {
            await loadAuthMethods();
            showLoginScreen();
        }
    } catch (error) {
        console.error('Auth check failed:', error);
        await loadAuthMethods();
        showLoginScreen();
    }
}

async function loadAuthMethods() {
    try {
        const resp = await fetch('/auth/info');
        const info = await resp.json();

        const discordSection = document.getElementById('login-discord-section');
        const passwordSection = document.getElementById('login-password-section');
        const divider = document.getElementById('login-divider');
        const footer = document.getElementById('login-footer-text');

        if (info.discord) discordSection.style.display = 'block';
        if (info.password) passwordSection.style.display = 'block';
        if (info.discord && info.password) divider.style.display = 'flex';

        // Update footer text
        if (info.discord && info.password) {
            footer.textContent = 'Protected by Discord OAuth2 or Master Password';
        } else if (info.discord) {
            footer.textContent = 'Protected by Discord OAuth2';
        } else if (info.password) {
            footer.textContent = 'Protected by Master Password';
        } else {
            footer.textContent = 'No authentication methods configured';
        }
    } catch (e) {
        console.error('Failed to load auth methods:', e);
        // Fallback: show Discord button only
        document.getElementById('login-discord-section').style.display = 'block';
        document.getElementById('login-footer-text').textContent = 'Protected by Discord OAuth2';
    }
}

async function handlePasswordLogin(e) {
    e.preventDefault();
    const input = document.getElementById('master-password-input');
    const errorEl = document.getElementById('password-error');
    const btn = document.getElementById('btn-password-login');

    errorEl.style.display = 'none';
    btn.disabled = true;
    btn.textContent = 'Signing in...';

    try {
        const resp = await fetch('/auth/password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: input.value })
        });

        const data = await resp.json();

        if (resp.ok && data.success) {
            showDashboard(data.user);
        } else {
            errorEl.textContent = data.error || 'Login failed';
            errorEl.style.display = 'block';
            input.classList.add('shake');
            setTimeout(() => input.classList.remove('shake'), 600);
        }
    } catch (error) {
        console.error('Password login failed:', error);
        errorEl.textContent = 'Connection error';
        errorEl.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M11 7l-1.41 1.41L12.17 11H2v2h10.17l-2.58 2.59L11 17l5-5-5-5z"/></svg> Sign In`;
    }
}

function showLoginScreen() {
    document.getElementById('login-screen').style.display = 'flex';
    document.getElementById('dashboard').style.display = 'none';
}

function showDashboard(user) {
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('dashboard').style.display = 'flex';
    document.getElementById('user-name').textContent = user.username;

    // Also set mobile username
    const mobileUserName = document.getElementById('user-name-mobile');
    if (mobileUserName) {
        mobileUserName.textContent = user.username;
    }

    // Load initial data
    loadClaims();
    loadRequests();
    loadPlayerMapping();
}

// Toggle sidebar for mobile
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.querySelector('.sidebar-overlay');

    if (sidebar) {
        sidebar.classList.toggle('open');
    }
    if (overlay) {
        overlay.classList.toggle('active');
    }
}

// =====================================================
// Data Loading
// =====================================================

async function loadClaims() {
    try {
        const response = await fetch('/api/claims');
        if (response.status === 401) {
            showLoginScreen();
            return;
        }
        claims = await response.json();
        renderClaims();
        updateStats();
    } catch (error) {
        console.error('Failed to load claims:', error);
        showToast('Failed to load claims', 'error');
    }
}

async function loadRequests() {
    try {
        const response = await fetch('/api/requests');
        if (response.status === 401) return;
        requests = await response.json();
        renderRequests();
        updateStats();
    } catch (error) {
        console.error('Failed to load requests:', error);
    }
}

async function loadPlayerMapping() {
    try {
        const response = await fetch('/api/player-mapping');
        if (response.status === 401) return;
        playerMapping = await response.json();
        updateStats();
    } catch (error) {
        console.error('Failed to load player mapping:', error);
    }
}

function updateStats() {
    document.getElementById('stat-total-claims').textContent = claims.length;

    const protectedCount = claims.filter(c =>
        c.flags && (!c.flags.block_break || !c.flags.block_place || !c.flags.interact)
    ).length;
    document.getElementById('stat-protected').textContent = protectedCount;

    document.getElementById('stat-pending').textContent = requests.length;
    document.getElementById('stat-players').textContent = Object.keys(playerMapping).length;

    // Update nav badges
    const claimsCount = document.getElementById('nav-claims-count');
    const requestsCount = document.getElementById('nav-requests-count');
    if (claimsCount) claimsCount.textContent = claims.length;
    if (requestsCount) requestsCount.textContent = requests.length;
}

// =====================================================
// Claims Rendering - Card Grid
// =====================================================

function renderClaims() {
    const grid = document.getElementById('claims-grid');
    const searchTerm = document.getElementById('claim-search').value.toLowerCase();

    let filtered = claims;

    if (searchTerm) {
        filtered = filtered.filter(c => c.name.toLowerCase().includes(searchTerm));
    }

    if (currentDimensionFilter) {
        filtered = filtered.filter(c => c.dimension === currentDimensionFilter);
    }

    if (filtered.length === 0) {
        grid.innerHTML = `
            <div class="empty-state" style="grid-column: 1 / -1;">
                <div class="empty-visual">
                    <span class="empty-icon">📋</span>
                    <div class="empty-rings"></div>
                </div>
                <h3>No Claims Found</h3>
                <p>${searchTerm || currentDimensionFilter ? 'Try adjusting your filters' : 'Create your first claim to get started'}</p>
            </div>
        `;
        return;
    }

    grid.innerHTML = filtered.map(claim => {
        const isProtected = claim.flags && (!claim.flags.block_break || !claim.flags.block_place);
        const owners = claim.allowed_names || [];
        const pending = claim.pending_names || [];
        const allOwners = [...owners, ...pending.filter(p => !owners.map(o => o.toLowerCase()).includes(p.toLowerCase()))];

        return `
            <div class="claim-card">
                <div class="claim-header">
                    <span class="claim-name">${escapeHtml(claim.name)}</span>
                    <span class="claim-dimension ${claim.dimension}">${getDimensionLabel(claim.dimension)}</span>
                </div>
                
                <div class="claim-coords">
                    📍 (${claim.x1}, ${claim.y1}, ${claim.z1}) → (${claim.x2}, ${claim.y2}, ${claim.z2})
                </div>
                
                <div class="claim-owners">
                    ${allOwners.slice(0, 4).map((o, i) => {
            const isPending = pending.map(p => p.toLowerCase()).includes(o.toLowerCase()) &&
                !owners.map(own => own.toLowerCase()).includes(o.toLowerCase());
            return `<span class="owner-tag ${isPending ? 'pending' : ''}">${escapeHtml(o)}</span>`;
        }).join('')}
                    ${allOwners.length > 4 ? `<span class="owner-tag">+${allOwners.length - 4}</span>` : ''}
                    ${allOwners.length === 0 ? '<span class="owner-tag" style="opacity: 0.5;">No owners</span>' : ''}
                </div>
                
                <div class="claim-footer">
                    <div class="claim-status">
                        <span class="status-dot ${isProtected ? 'protected' : 'open'}"></span>
                        ${isProtected ? 'Protected' : 'Open'}
                    </div>
                    <div class="claim-actions">
                        <button class="btn btn-secondary btn-icon" onclick="editClaim('${escapeHtml(claim._key)}')" title="Edit">
                            ✏️
                        </button>
                        <button class="btn btn-danger btn-icon" onclick="deleteClaim('${escapeHtml(claim._key)}')" title="Delete">
                            🗑️
                        </button>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function getDimensionLabel(dimension) {
    const labels = {
        'overworld': '🌍 Overworld',
        'nether': '🔥 Nether',
        'the_end': '🌌 The End'
    };
    return labels[dimension] || dimension;
}

// =====================================================
// Requests Rendering
// =====================================================

function renderRequests() {
    const grid = document.getElementById('requests-grid');
    const emptyState = document.getElementById('requests-empty');

    if (requests.length === 0) {
        if (emptyState) emptyState.style.display = 'block';
        return;
    }

    if (emptyState) emptyState.style.display = 'none';

    grid.innerHTML = requests.map(req => {
        const date = new Date(req.timestamp * 1000).toLocaleDateString();
        const area = Math.abs((req.x2 - req.x1 + 1) * (req.z2 - req.z1 + 1));

        return `
            <div class="request-card">
                <div class="request-info">
                    <h4>${escapeHtml(req.claim_name)}</h4>
                    <p>Requested by <strong>${escapeHtml(req.player_name)}</strong></p>
                    <div class="request-meta">
                        <span class="claim-dimension ${req.dimension}">${getDimensionLabel(req.dimension)}</span>
                        <span style="margin-left: 12px;">📐 ${area.toLocaleString()} blocks</span>
                        <span style="margin-left: 12px;">📅 ${date}</span>
                    </div>
                </div>
                <div class="actions-cell">
                    <button class="btn btn-primary btn-sm" onclick="reviewRequest('${req.request_id}')">
                        Review
                    </button>
                </div>
            </div>
        `;
    }).join('');
}

// =====================================================
// Claim Modal
// =====================================================

function openCreateClaimModal() {
    currentEditKey = null;
    document.getElementById('modal-title').textContent = 'Create New Claim';
    document.getElementById('claim-form').reset();
    document.getElementById('claim-edit-key').value = '';

    // Reset toggles to defaults
    document.getElementById('flag-block-break').checked = true;
    document.getElementById('flag-block-place').checked = true;
    document.getElementById('flag-interact').checked = true;
    document.getElementById('flag-particles').checked = true;
    document.getElementById('flag-kill-passive').checked = false;
    document.getElementById('flag-kill-hostile').checked = false;
    document.getElementById('flag-ignore-named').checked = true;

    // Reset parent/child fields
    document.getElementById('claim-is-child').checked = false;
    document.getElementById('parent-claim-group').style.display = 'none';
    populateParentDropdown();

    document.getElementById('claim-modal').style.display = 'flex';
}

function editClaim(key) {
    const claim = claims.find(c => c._key === key);
    if (!claim) return;

    currentEditKey = key;
    document.getElementById('modal-title').textContent = 'Edit Claim';
    document.getElementById('claim-edit-key').value = key;

    // Populate parent/child fields first (needed before populating dropdown)
    populateParentDropdown(key);  // Exclude current claim from parent options
    document.getElementById('claim-is-child').checked = claim.is_child || false;
    document.getElementById('parent-claim-group').style.display = claim.is_child ? 'block' : 'none';
    document.getElementById('claim-parent').value = claim.parent_claim || '';

    // Populate form
    document.getElementById('claim-name').value = claim.name;
    document.getElementById('claim-dimension').value = claim.dimension;
    document.getElementById('claim-x1').value = claim.x1;
    document.getElementById('claim-y1').value = claim.y1;
    document.getElementById('claim-z1').value = claim.z1;
    document.getElementById('claim-x2').value = claim.x2;
    document.getElementById('claim-y2').value = claim.y2;
    document.getElementById('claim-z2').value = claim.z2;

    // Populate owners
    const allOwners = [...(claim.allowed_names || []), ...(claim.pending_names || [])];
    const uniqueOwners = [...new Set(allOwners.map(o => o.toLowerCase()))].map(o => {
        return allOwners.find(x => x.toLowerCase() === o) || o;
    });
    document.getElementById('claim-owners').value = uniqueOwners.join('\n');

    // Populate flags
    if (claim.flags) {
        document.getElementById('flag-block-break').checked = claim.flags.block_break;
        document.getElementById('flag-block-place').checked = claim.flags.block_place;
        document.getElementById('flag-interact').checked = claim.flags.interact;
        document.getElementById('flag-particles').checked = claim.flags.perimeter_particles;
        document.getElementById('flag-kill-passive').checked = claim.flags.kill_passive_mobs;
        document.getElementById('flag-kill-hostile').checked = claim.flags.kill_hostile_mobs;
        document.getElementById('flag-ignore-named').checked = claim.flags.ignore_named_mobs;
    }

    document.getElementById('claim-modal').style.display = 'flex';
}

// Toggle parent claim dropdown visibility
function toggleParentSelect() {
    const isChild = document.getElementById('claim-is-child').checked;
    const parentGroup = document.getElementById('parent-claim-group');
    parentGroup.style.display = isChild ? 'block' : 'none';
}

// Populate parent claim dropdown with parent claims (non-child claims)
function populateParentDropdown(excludeKey = null) {
    const select = document.getElementById('claim-parent');
    const parentClaims = claims.filter(c => !c.is_child && c._key !== excludeKey);

    select.innerHTML = '<option value="">-- Select Parent --</option>';
    parentClaims.forEach(claim => {
        const option = document.createElement('option');
        option.value = claim._key;
        option.textContent = claim.name;
        select.appendChild(option);
    });
}

function closeModal() {
    document.getElementById('claim-modal').style.display = 'none';
    currentEditKey = null;
}

async function saveClaim() {
    const name = document.getElementById('claim-name').value.trim();
    if (!name) {
        showToast('Claim name is required', 'error');
        return;
    }

    const ownersText = document.getElementById('claim-owners').value;
    const ownersList = ownersText.split('\n').map(o => o.trim()).filter(o => o);

    const data = {
        name: name,
        dimension: document.getElementById('claim-dimension').value,
        x1: parseInt(document.getElementById('claim-x1').value) || 0,
        y1: parseInt(document.getElementById('claim-y1').value) || -64,
        z1: parseInt(document.getElementById('claim-z1').value) || 0,
        x2: parseInt(document.getElementById('claim-x2').value) || 0,
        y2: parseInt(document.getElementById('claim-y2').value) || 320,
        z2: parseInt(document.getElementById('claim-z2').value) || 0,
        allowed_names: ownersList,
        flags: {
            block_break: document.getElementById('flag-block-break').checked,
            block_place: document.getElementById('flag-block-place').checked,
            interact: document.getElementById('flag-interact').checked,
            perimeter_particles: document.getElementById('flag-particles').checked,
            kill_passive_mobs: document.getElementById('flag-kill-passive').checked,
            kill_hostile_mobs: document.getElementById('flag-kill-hostile').checked,
            ignore_named_mobs: document.getElementById('flag-ignore-named').checked
        },
        is_child: document.getElementById('claim-is-child').checked,
        parent_claim: document.getElementById('claim-is-child').checked ?
            document.getElementById('claim-parent').value || null : null
    };

    try {
        let response;
        if (currentEditKey) {
            response = await fetch(`/api/claims/${encodeURIComponent(currentEditKey)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
        } else {
            response = await fetch('/api/claims', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
        }

        if (response.ok) {
            closeModal();
            loadClaims();
            showToast(currentEditKey ? 'Claim updated' : 'Claim created', 'success');
        } else {
            const error = await response.json();
            showToast(error.error || 'Failed to save claim', 'error');
        }
    } catch (error) {
        console.error('Failed to save claim:', error);
        showToast('Failed to save claim', 'error');
    }
}

async function deleteClaim(key) {
    if (!confirm('Are you sure you want to delete this claim?')) return;

    try {
        const response = await fetch(`/api/claims/${encodeURIComponent(key)}`, {
            method: 'DELETE'
        });

        if (response.ok) {
            loadClaims();
            showToast('Claim deleted', 'success');
        } else {
            showToast('Failed to delete claim', 'error');
        }
    } catch (error) {
        console.error('Failed to delete claim:', error);
        showToast('Failed to delete claim', 'error');
    }
}

// =====================================================
// Request Review Modal
// =====================================================

function reviewRequest(requestId) {
    const req = requests.find(r => r.request_id === requestId);
    if (!req) return;

    currentRequestId = requestId;

    const area = Math.abs((req.x2 - req.x1 + 1) * (req.z2 - req.z1 + 1));
    const date = new Date(req.timestamp * 1000).toLocaleString();

    document.getElementById('request-details').innerHTML = `
        <div class="form-group">
            <label>Claim Name</label>
            <p><strong>${escapeHtml(req.claim_name)}</strong></p>
        </div>
        <div class="form-group">
            <label>Requested By</label>
            <p>${escapeHtml(req.player_name)}</p>
        </div>
        <div class="form-row">
            <div class="form-group">
                <label>Dimension</label>
                <p><span class="claim-dimension ${req.dimension}">${getDimensionLabel(req.dimension)}</span></p>
            </div>
            <div class="form-group">
                <label>Area</label>
                <p>${area.toLocaleString()} blocks</p>
            </div>
        </div>
        <div class="form-group">
            <label>Coordinates</label>
            <p class="claim-coords" style="display: inline-block;">(${req.x1}, ${req.y1}, ${req.z1}) → (${req.x2}, ${req.y2}, ${req.z2})</p>
        </div>
        <div class="form-group">
            <label>Requested</label>
            <p>${date}</p>
        </div>
    `;

    document.getElementById('request-modal').style.display = 'flex';
}

function closeRequestModal() {
    document.getElementById('request-modal').style.display = 'none';
    currentRequestId = null;
}

async function approveRequest() {
    if (!currentRequestId) return;

    const fullProtection = document.getElementById('req-flag-protected').checked;
    const particles = document.getElementById('req-flag-particles').checked;

    try {
        const response = await fetch(`/api/requests/${currentRequestId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action: 'approve',
                flags: {
                    block_break: !fullProtection,
                    block_place: !fullProtection,
                    interact: !fullProtection,
                    perimeter_particles: particles
                }
            })
        });

        if (response.ok) {
            closeRequestModal();
            loadRequests();
            loadClaims();
            showToast('Request approved', 'success');
        } else {
            showToast('Failed to approve request', 'error');
        }
    } catch (error) {
        console.error('Failed to approve request:', error);
        showToast('Failed to approve request', 'error');
    }
}

async function denyRequest() {
    if (!currentRequestId) return;

    try {
        const response = await fetch(`/api/requests/${currentRequestId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'deny' })
        });

        if (response.ok) {
            closeRequestModal();
            loadRequests();
            showToast('Request denied', 'info');
        } else {
            showToast('Failed to deny request', 'error');
        }
    } catch (error) {
        console.error('Failed to deny request:', error);
        showToast('Failed to deny request', 'error');
    }
}

// =====================================================
// Bulk Operations
// =====================================================

async function exportClaims() {
    try {
        const response = await fetch('/api/bulk/export');
        const data = await response.json();

        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `claims_export_${new Date().toISOString().split('T')[0]}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        showToast('Claims exported successfully', 'success');
    } catch (error) {
        console.error('Failed to export claims:', error);
        showToast('Failed to export claims', 'error');
    }
}

function downloadTemplate() {
    const template = `name,dimension,x1,y1,z1,x2,y2,z2,owners,protected
Example Town,overworld,100,-64,200,200,320,300,"player1,player2",true
Example Farm,overworld,300,-64,400,400,320,500,player3,false`;

    const blob = new Blob([template], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'claims_template.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

function handleFileSelect(file) {
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (e) => {
        try {
            if (file.name.endsWith('.json')) {
                importData = JSON.parse(e.target.result);
            } else if (file.name.endsWith('.csv')) {
                importData = { csv: true, content: e.target.result, file: file };
            } else {
                showToast('Unsupported file format', 'error');
                return;
            }

            showImportPreview();
        } catch (error) {
            showToast('Failed to parse file', 'error');
        }
    };
    reader.readAsText(file);
}

function showImportPreview() {
    const preview = document.getElementById('import-preview');
    const content = document.getElementById('import-preview-content');

    let claimCount = 0;
    if (importData.csv) {
        const lines = importData.content.split('\n').filter(l => l.trim());
        claimCount = Math.max(0, lines.length - 1);
    } else {
        claimCount = (importData.claims || []).length;
    }

    content.innerHTML = `
        <p><strong>${claimCount}</strong> claims found in file</p>
        <p>Mode: <strong>${document.querySelector('input[name="import-mode"]:checked').value}</strong></p>
    `;

    preview.style.display = 'block';
}

function cancelImport() {
    importData = null;
    document.getElementById('import-preview').style.display = 'none';
    document.getElementById('import-file').value = '';
}

async function confirmImport() {
    if (!importData) return;

    const mode = document.querySelector('input[name="import-mode"]:checked').value;

    try {
        let response;
        if (importData.csv) {
            const formData = new FormData();
            formData.append('file', importData.file);
            response = await fetch(`/api/bulk/import?mode=${mode}`, {
                method: 'POST',
                body: formData
            });
        } else {
            response = await fetch(`/api/bulk/import?mode=${mode}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(importData)
            });
        }

        if (response.ok) {
            const result = await response.json();
            cancelImport();
            loadClaims();
            showToast(`Imported ${result.imported} claims (${result.skipped} skipped)`, 'success');
        } else {
            const error = await response.json();
            showToast(error.error || 'Import failed', 'error');
        }
    } catch (error) {
        console.error('Import failed:', error);
        showToast('Import failed', 'error');
    }
}

// =====================================================
// Event Listeners
// =====================================================

function setupEventListeners() {
    // Sidebar navigation
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

            item.classList.add('active');
            const tabId = `tab-${item.dataset.tab}`;
            document.getElementById(tabId).classList.add('active');

            // Close sidebar on mobile after navigation
            const sidebar = document.getElementById('sidebar');
            const overlay = document.querySelector('.sidebar-overlay');
            if (sidebar) sidebar.classList.remove('open');
            if (overlay) overlay.classList.remove('active');
        });
    });

    // Search
    document.getElementById('claim-search').addEventListener('input', renderClaims);

    // Filter chips
    document.querySelectorAll('.filter-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
            chip.classList.add('active');
            currentDimensionFilter = chip.dataset.dimension;
            renderClaims();
        });
    });

    // File upload
    const dropzone = document.getElementById('import-dropzone');
    const fileInput = document.getElementById('import-file');

    if (dropzone) {
        dropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropzone.classList.add('dragover');
        });

        dropzone.addEventListener('dragleave', () => {
            dropzone.classList.remove('dragover');
        });

        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            handleFileSelect(e.dataTransfer.files[0]);
        });
    }

    if (fileInput) {
        fileInput.addEventListener('change', () => {
            handleFileSelect(fileInput.files[0]);
        });
    }

    // Modal close on overlay click
    document.querySelectorAll('.modal-overlay').forEach(overlay => {
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) {
                overlay.style.display = 'none';
            }
        });
    });

    // Escape key to close modals
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            document.querySelectorAll('.modal-overlay').forEach(m => m.style.display = 'none');
        }
    });
}

// =====================================================
// Utilities
// =====================================================

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    const icons = {
        success: '✓',
        error: '✕',
        info: 'ℹ'
    };

    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <span class="toast-message">${escapeHtml(message)}</span>
    `;

    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}
