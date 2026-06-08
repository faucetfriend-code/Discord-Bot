/* ─── Unity Oracle Aggregator — Shared Interactions ─── */

/* ============================
   DATA: Analysts + Signals
   ============================ */
var ANALYSTS = [
  { id:'caleb', name:'#i-caleb', initials:'CL', role:'Discord Analyst', followers:8600, color:'#ff6b35' },
  { id:'nurse-neil', name:'#i-nurse-neil', initials:'NN', role:'Discord Analyst', followers:7200, color:'#dc3545' },
  { id:'soul', name:'#i-soul', initials:'SL', role:'Discord Analyst', followers:5400, color:'#6c5ce7' },
  { id:'sveezy', name:'#i-sveezy', initials:'SV', role:'Discord Analyst', followers:9100, color:'#17a34a' },
  { id:'wilsauce', name:'#i-wilsauce', initials:'WS', role:'Discord Analyst', followers:3800, color:'#0ea5e9' },
  { id:'badillusion', name:'#i-badillusion', initials:'BI', role:'Discord Analyst', followers:6700, color:'#f59e0b' },
  { id:'prestige', name:'#i-prestige', initials:'PR', role:'Discord Analyst', followers:12300, color:'#ec4899' },
  { id:'grasady', name:'#i-grasady', initials:'GR', role:'Discord Analyst', followers:4900, color:'#8b5cf6' },
  { id:'ajmal', name:'#i-ajmal', initials:'AJ', role:'Discord Analyst', followers:2800, color:'#14b8a6' },
  { id:'sherlock', name:'#i-sherlock', initials:'SH', role:'Discord Analyst', followers:10500, color:'#f97316' },
  { id:'buying-volatility', name:'#i-buying-volatility', initials:'BV', role:'Volatility Trader', followers:15800, color:'#a855f7' },
  { id:'selling-volatility', name:'#i-selling-volatility', initials:'SV', role:'Volatility Trader', followers:14200, color:'#5865F2' },
  { id:'rsi-extreme', name:'#i-rsi-extreme', initials:'RE', role:'Technical Analyst', followers:7500, color:'#64748b' }
];
var AUTO_SOURCES = [
  { id:'onchain-monitor', name:'On-Chain Monitor', initials:'OC', role:'Automated', followers:0, color:'#64748b' },
  { id:'funding-tracker', name:'Funding Rate Tracker', initials:'FR', role:'Automated', followers:0, color:'#94a3b8' },
  { id:'liquidation-warn', name:'Liq. Cascade Warning', initials:'LC', role:'Automated', followers:0, color:'#475569' }
];
var ALL_SOURCES = ANALYSTS.concat(AUTO_SOURCES);

var SIGNAL_DATA = [
  { id:'s1', analyst:'buying-volatility', token:'BTC', signal:'buy', conviction:'high', price:67421, target:74000, stop:62500, rrw:'1:2.8', reason:'On-chain accumulation detected across 12 exchange wallets. BTC reserve risk at 6-month low. Funding rates neutral — no overcrowding.', time:'2h ago' },
  { id:'s2', analyst:'selling-volatility', token:'ETH', signal:'sell', conviction:'medium', price:3482, target:3150, stop:3650, rrw:'1:2.1', reason:'Exchange inflow spike — 142k ETH moved to exchanges in 24h. Gas fees declining, L2 activity shifting from ETH to SOL.', time:'4h ago' },
  { id:'s3', analyst:'sveezy', token:'SOL', signal:'buy', conviction:'high', price:145.82, target:175, stop:128, rrw:'1:2.4', reason:'Active addresses at ATH — 2.1M daily. Break above $142 resistance with strong volume. Perp funding flipped positive.', time:'1h ago' },
  { id:'s4', analyst:'soul', token:'LINK', signal:'hold', conviction:'low', price:16.24, target:'17.50 - 18.00', stop:14.80, rrw:'1:1.3', reason:'Consolidating in a tight range ($15.80 — $16.80) for 14 days. No clear catalyst. TVD growing but price not responding.', time:'6h ago' },
  { id:'s5', analyst:'prestige', token:'BTC', signal:'buy', conviction:'high', price:67421, target:72000, stop:64800, rrw:'1:2.0', reason:'Whale cluster accumulation at $65k-$67k range. Three wallets accumulated 8,400 BTC in the past 48h. Exchange reserves dropping.', time:'3h ago' },
  { id:'s6', analyst:'wilsauce', token:'ARB', signal:'buy', conviction:'medium', price:0.84, target:1.05, stop:0.74, rrw:'1:1.9', reason:'Arbitrum TVL growing 12% MoM. New gaming partnership pipeline. GMX v2 migration driving increased volume on L2.', time:'5h ago' },
  { id:'s7', analyst:'nurse-neil', token:'ETH', signal:'buy', conviction:'medium', price:3482, target:3800, stop:3300, rrw:'1:1.8', reason:'Options open interest at $8.2B — max pain at $3,600 for June expiry. Call skew building steadily. Dealer hedging implies upward pressure.', time:'7h ago' },
  { id:'s8', analyst:'badillusion', token:'SOL', signal:'hold', conviction:'medium', price:145.82, target:'150 - 160', stop:135, rrw:'1:0.7', reason:'Sentiment divergence: social bullish (72%) but on-chain volume distribution shows profit-taking. Mixed signals warrant caution.', time:'2h ago' },
  { id:'s9', analyst:'rsi-extreme', token:'BTC', signal:'hold', conviction:'low', price:67421, target:'68k - 70k', stop:65800, rrw:'1:0.5', reason:'MVRV ratio at 2.8 — historically signaling late bull phase. STH-SOPR declining. Not yet a sell but upside limited in short term.', time:'8h ago' },
  { id:'s10', analyst:'caleb', token:'LINK', signal:'buy', conviction:'medium', price:16.24, target:20.00, stop:14.50, rrw:'1:2.3', reason:'Chainlink CCIP adoption accelerating — 3 major banks integrated this quarter. 2024 revenue projected +40% YoY. Current valuation discounts growth.', time:'10h ago' },
  { id:'s11', analyst:'grasady', token:'DOT', signal:'buy', conviction:'medium', price:7.82, target:10.00, stop:6.80, rrw:'1:2.1', reason:'Polkadot 2.0 runtime upgrade pending. Cross-chain messaging volume up 28% MoM. Asynchronous backing reducing block times by 60%.', time:'12h ago' },
  { id:'s12', analyst:'selling-volatility', token:'OP', signal:'sell', conviction:'medium', price:2.84, target:2.40, stop:3.10, rrw:'1:1.6', reason:'OP token unlocks: 24M tokens entering circulation next week. Historical unlock events preceded -15% to -22% price action across L2 tokens.', time:'9h ago' },
  { id:'s13', analyst:'sherlock', token:'BTC', signal:'hold', conviction:'medium', price:67421, target:'sideways', stop:null, rrw:'N/A', reason:'Basis trade unwinding across CME futures. Open interest declining while spot premium fades. Derivatives market cooling suggests consolidation phase.', time:'11h ago' },
  { id:'s14', analyst:'ajmal', token:'ETH', signal:'buy', conviction:'high', price:3482, target:4200, stop:3200, rrw:'1:2.4', reason:'ETH/BTC ratio at 2-year low (0.052) — mean reversion play. Dencun upgrade benefits accruing. Spot ETF flows turning positive again.', time:'6h ago' },
  { id:'s15', analyst:'buying-volatility', token:'SOL', signal:'sell', conviction:'low', price:145.82, target:130, stop:152, rrw:'1:1.8', reason:'Whale distribution detected: top 10 non-exchange wallets decreased SOL holdings by 3.2% in 48h. Cluster of sell orders at $148-$150.', time:'4h ago' },
  { id:'s16', analyst:'prestige', token:'AAVE', signal:'buy', conviction:'high', price:142.50, target:185, stop:125, rrw:'1:2.3', reason:'Macro tailwind for lending protocols as rate cut narrative strengthens. Aave v4 governance passed. Revenue up 65% QoQ across all deployments.', time:'14h ago' },
  { id:'s17', analyst:'wilsauce', token:'UNI', signal:'buy', conviction:'medium', price:9.84, target:12.50, stop:8.60, rrw:'1:2.0', reason:'Uniswap v4 hooks ecosystem launching. Fee switch governance proposal gaining traction. Cross-chain deployment on Celo and Polygon driving volume.', time:'16h ago' },
  { id:'s18', analyst:'rsi-extreme', token:'BTC', signal:'buy', conviction:'high', price:67421, target:76000, stop:63000, rrw:'1:1.8', reason:'Record low put/call ratio (0.38). $75k and $80k call open interest building. Market makers delta-hedged long — gamma squeeze setup above $68k.', time:'5h ago' },
  { id:'s19', analyst:'badillusion', token:'PEPE', signal:'sell', conviction:'low', price:0.000012, target:0.000008, stop:0.000015, rrw:'1:1.3', reason:'Meme coin sentiment rolling over. Social volume down 40% from peak. Exchange wallet balances increasing — distribution phase likely.', time:'18h ago' },
  { id:'s20', analyst:'grasady', token:'ATOM', signal:'hold', conviction:'low', price:9.12, target:'9.50 - 10.00', stop:8.40, rrw:'1:0.6', reason:'ICS-121 cross-chain messaging live but adoption slow. Staking yields declining to 14% APY. Ecosystem grants paused. Wait for catalyst.', time:'20h ago' },
  { id:'s21', analyst:'sveezy', token:'PENDLE', signal:'buy', conviction:'high', price:6.42, target:9.00, stop:5.20, rrw:'1:2.0', reason:'Pendle PT/YT trading volume up 340% in 30 days. EigenLayer restaking narrative driving demand for yield tokenization. TVL at ATH.', time:'13h ago' },
  { id:'s22', analyst:'caleb', token:'LDO', signal:'buy', conviction:'medium', price:2.42, target:3.20, stop:2.00, rrw:'1:2.4', reason:'Lido staking share recovering after Shanghai withdrawal shock. stETH/ETH peg stable at 0.998. New institutional staking product launching next month.', time:'22h ago' },
  { id:'s23', analyst:'onchain-monitor', token:'BTC', signal:'buy', conviction:'high', price:67421, target:69000, stop:null, rrw:'N/A', reason:'AUTO: 8,200 BTC withdrawn from exchanges in past 6h — largest daily outflow in 3 months. Accumulation trend intensifies across all wallet cohorts.', time:'30m ago' },
  { id:'s24', analyst:'funding-tracker', token:'ETH', signal:'sell', conviction:'medium', price:3482, target:3300, stop:null, rrw:'N/A', reason:'AUTO: ETH perpetual funding rate at -0.008% (negative for 3 consecutive days). Last time this persisted, price corrected -12%. Short bias accumulating.', time:'45m ago' },
  { id:'s25', analyst:'liquidation-warn', token:'DOGE', signal:'sell', conviction:'high', price:0.14, target:0.11, stop:null, rrw:'N/A', reason:'AUTO: Liquidation cluster of $42M at $0.135 — 8.4x average leverage on long positions. Cascading liquidation risk if support breaks.', time:'15m ago' },
  { id:'s26', analyst:'funding-tracker', token:'SOL', signal:'buy', conviction:'high', price:145.82, target:155, stop:null, rrw:'N/A', reason:'AUTO: SOL funding flipping positive (+0.012%) after 4 days of neutral/negative. Open interest up $240M in 12h — new longs entering with conviction.', time:'1h ago' },
  { id:'s27', analyst:'onchain-monitor', token:'UNI', signal:'buy', conviction:'medium', price:9.84, target:11.00, stop:null, rrw:'N/A', reason:'AUTO: UNI large transactions (>$100k) up 340% in 24h. Smart money addresses accumulating — top 50 holders increased positions by 1.8% today.', time:'25m ago' },
  { id:'s28', analyst:'liquidation-warn', token:'SOL', signal:'hold', conviction:'low', price:145.82, target:null, stop:null, rrw:'N/A', reason:'AUTO: SOL leverage ratio elevated (0.72). Estimated liquidation cascade at $138 (-5.4%) could trigger $28M in forced closures. Reduce position size.', time:'3h ago' },
  { id:'s29', analyst:'nurse-neil', token:'MKR', signal:'buy', conviction:'high', price:2980, target:3800, stop:2600, rrw:'1:2.3', reason:'MakerDAO Endgame plan generating real revenue — Sky USDS stablecoin launched. Balance sheet restructuring reducing DSR burden. Buyback program active.', time:'1d ago' },
  { id:'s30', analyst:'soul', token:'SEI', signal:'buy', conviction:'medium', price:0.62, target:0.85, stop:0.52, rrw:'1:2.0', reason:'Sei v2 upgrade with EVM compatibility driving developer inflow. Cross-chain TVL bridging from Ethereum up 8x since v2 announcement.', time:'2d ago' }
];

/* ============================
   AUTH SYSTEM
   ============================ */
function getAuth() {
  try { return JSON.parse(localStorage.getItem('ua:auth') || 'null'); } catch(e) { return null; }
}
function setAuth(data) { try { localStorage.setItem('ua:auth', JSON.stringify(data)); } catch(e) {} }
function clearAuth() { try { localStorage.removeItem('ua:auth'); } catch(e) {} }
function canAccessPremium() { var a = getAuth(); return a && a.premium === true; }

function initAuth() {
  var overlay = document.getElementById('authOverlay');
  if (!overlay) return;
  var loginBtn = document.getElementById('loginBtn');
  var logoutBtn = document.getElementById('logoutBtn');
  var authUser = document.getElementById('authUser');
  var guestView = document.getElementById('guestView');
  var auth = getAuth();

  if (auth) {
    if (guestView) guestView.style.display = 'none';
    if (authUser) { authUser.style.display = 'block'; authUser.querySelector('.auth-name').textContent = auth.name; }
    if (logoutBtn) logoutBtn.style.display = '';
    if (loginBtn) loginBtn.style.display = 'none';
    var badge = document.querySelector('.premium-badge');
    if (badge) badge.style.display = auth.premium ? '' : 'none';
  } else {
    if (guestView) guestView.style.display = '';
    if (authUser) authUser.style.display = 'none';
    if (logoutBtn) logoutBtn.style.display = 'none';
    if (loginBtn) loginBtn.style.display = '';
    var badge = document.querySelector('.premium-badge');
    if (badge) badge.style.display = 'none';
  }

  if (loginBtn) {
    loginBtn.addEventListener('click', function() { overlay.classList.add('open'); });
  }
  var discordBtns = overlay.querySelectorAll('[data-auth="discord"]');
  discordBtns.forEach(function(btn) {
    btn.addEventListener('click', function() {
      var mockUser = {
        name: 'Jamie Duval',
        discordId: '428391029384',
        avatar: null,
        premium: true,
        joined: '2026-03-15'
      };
      setAuth(mockUser);
      overlay.classList.remove('open');
      initAuth();
      updatePremiumUI();
    });
  });
  overlay.addEventListener('click', function(e) {
    if (e.target === overlay) overlay.classList.remove('open');
  });
  if (logoutBtn) {
    logoutBtn.addEventListener('click', function() {
      clearAuth();
      initAuth();
      updatePremiumUI();
    });
  }
}

function updatePremiumUI() {
  document.querySelectorAll('.premium-gate').forEach(function(el) {
    el.style.display = canAccessPremium() ? '' : 'none';
  });
  document.querySelectorAll('.premium-upsell').forEach(function(el) {
    el.style.display = canAccessPremium() ? 'none' : '';
  });
}

/* ============================
   NOTIFICATION CENTER
   ============================ */
var NOTIFICATIONS = [
  { id:'n1', type:'alert', category:'price', title:'BTC Volatility Spike', desc:'BTC dropped 4.2% in 15 minutes — liquidation cluster detected near $66k', time:'2m ago', read:false },
  { id:'n2', type:'signal', category:'portfolio', title:'SOL Breakout Confirmed', desc:'SOL broke above $145 resistance with volume surge — +5.6% in 1h', time:'15m ago', read:false },
  { id:'n3', type:'info', category:'system', title:'Positions Report Ready', desc:'Weekly positions PDF report available for download', time:'1h ago', read:false },
  { id:'n4', type:'alert', category:'price', title:'ETH Whale Movement', desc:'142k ETH moved to exchanges — potential distribution signaled', time:'3h ago', read:false },
  { id:'n5', type:'signal', category:'portfolio', title:'BTC Accumulation Alert', desc:'8,200 BTC withdrawn from exchanges — largest outflow in 3 months', time:'4h ago', read:true },
  { id:'n6', type:'signal', category:'analyst', title:'New Signal from Alex Korsakov', desc:'Bullish on BTC — on-chain accumulation detected', time:'5h ago', read:true },
  { id:'n7', type:'info', category:'system', title:'Data Source Updated', desc:'Pyth network feed synchronized — all prices current', time:'8h ago', read:true },
  { id:'n8', type:'alert', category:'price', title:'LINK Volume Anomaly', desc:'On-chain volume spike 340% above 30d average — investigating', time:'10h ago', read:true },
  { id:'n9', type:'info', category:'analyst', title:'Maria Chen New Analysis', desc:'Published derivatives market report for Q3 outlook', time:'14h ago', read:true },
  { id:'n10', type:'signal', category:'portfolio', title:'Position Liquidation Warning', desc:'ETH short approaching liquidation at $4,120 — review margin', time:'18h ago', read:true },
  { id:'n11', type:'info', category:'system', title:'Subscription Renewed', desc:'Premium plan renewed — next billing July 1, 2026', time:'1d ago', read:true },
  { id:'n12', type:'alert', category:'price', title:'DOGE Liquidation Cascade Risk', desc:'$42M in leveraged longs at risk if DOGE breaks $0.135 support', time:'2d ago', read:true }
];

function renderNotifications(filter) {
  filter = filter || 'all';
  var list = document.getElementById('notifList');
  if (!list) return;
  var filtered = filter === 'all' ? NOTIFICATIONS : NOTIFICATIONS.filter(function(n) { return n.category === filter; });
  if (filtered.length === 0) {
    list.innerHTML = '<div class="notif-empty">No notifications in this category</div>';
    return;
  }
  list.innerHTML = filtered.map(function(n) {
    var badgeClass = n.type === 'alert' ? 'badge-negative' : n.type === 'signal' ? 'badge-positive' : 'badge-accent';
    var badgeLabel = n.type.charAt(0).toUpperCase() + n.type.slice(1);
    return '<div class="ni' + (n.read ? '' : ' unread') + '" data-id="' + n.id + '" onclick="markNotifRead(\'' + n.id + '\')">' +
      '<div class="ni-top">' +
      '<span class="ni-badge ' + badgeClass + '">' + badgeLabel + '</span>' +
      '<span class="ni-time">' + n.time + '</span>' +
      '</div>' +
      '<div class="ni-title">' + n.title + '</div>' +
      '<div class="ni-desc">' + n.desc + '</div>' +
      '</div>';
  }).join('');
  updateNotifBadge();
}

function markNotifRead(id) {
  var n = NOTIFICATIONS.find(function(x) { return x.id === id; });
  if (n) n.read = true;
  var activeCat = document.querySelector('.nf-cat.active');
  renderNotifications(activeCat ? activeCat.getAttribute('data-cat') : 'all');
}

function markAllRead() {
  NOTIFICATIONS.forEach(function(n) { n.read = true; });
  var activeCat = document.querySelector('.nf-cat.active');
  renderNotifications(activeCat ? activeCat.getAttribute('data-cat') : 'all');
}

function updateNotifBadge() {
  var unread = NOTIFICATIONS.filter(function(n) { return !n.read; }).length;
  document.querySelectorAll('.notif-badge-dot').forEach(function(dot) {
    dot.style.display = unread > 0 ? '' : 'none';
  });
  document.querySelectorAll('.notif-unread-count').forEach(function(el) {
    el.textContent = unread;
    el.style.display = unread > 0 ? '' : 'none';
  });
}

function initNotifications() {
  var bell = document.getElementById('notifBell');
  var overlay = document.getElementById('notifOverlay');
  var modal = document.getElementById('notifModal');
  if (!bell || !overlay || !modal) return;

  bell.addEventListener('click', function(e) {
    e.stopPropagation();
    overlay.classList.add('open');
    modal.classList.add('open');
    renderNotifications('all');
  });

  overlay.addEventListener('click', function() {
    overlay.classList.remove('open');
    modal.classList.remove('open');
  });

  document.querySelectorAll('.nf-cat').forEach(function(cat) {
    cat.addEventListener('click', function() {
      document.querySelectorAll('.nf-cat').forEach(function(c) { c.classList.remove('active'); });
      this.classList.add('active');
      renderNotifications(this.getAttribute('data-cat'));
    });
  });

  var markBtn = document.getElementById('markAllRead');
  if (markBtn) markBtn.addEventListener('click', markAllRead);

  updateNotifBadge();
}

/* ============================
   WATCHLIST (localStorage)
   ============================ */
function getWatchlist() {
  try { return JSON.parse(localStorage.getItem('ua:watchlist') || '[]'); } catch(e) { return []; }
}
function saveWatchlist(list) {
  try { localStorage.setItem('ua:watchlist', JSON.stringify(list)); } catch(e) {}
}
function addToWatchlist(symbol) {
  var list = getWatchlist();
  symbol = symbol.toUpperCase().trim();
  if (!symbol || list.indexOf(symbol) !== -1) return false;
  list.push(symbol);
  saveWatchlist(list);
  return true;
}
function removeFromWatchlist(symbol) {
  var list = getWatchlist();
  symbol = symbol.toUpperCase().trim();
  var idx = list.indexOf(symbol);
  if (idx === -1) return false;
  list.splice(idx, 1);
  saveWatchlist(list);
  return true;
}

function initWatchlist() {
  var addBtn = document.getElementById('wlAddBtn');
  var input = document.getElementById('wlInput');
  var list = document.getElementById('wlList');
  if (!addBtn || !input || !list) return;

  function renderWatchlist() {
    var symbols = getWatchlist();
    list.innerHTML = symbols.map(function(sym) {
      var randPct = (Math.random() * 10 - 3).toFixed(2);
      var isUp = parseFloat(randPct) >= 0;
      return '<div class="glass-sm row-between" style="padding:12px 16px;margin-bottom:8px;">' +
        '<div class="row gap-3">' +
        '<span class="wl-symbol" style="font-family:var(--font-mono);font-weight:700;">' + sym + '</span>' +
        '<span class="badge badge-muted">Tracking</span></div>' +
        '<div class="row gap-2">' +
        '<span class="badge ' + (isUp ? 'badge-positive' : 'badge-negative') + '">' + (isUp ? '+' : '') + randPct + '%</span>' +
        '<button class="btn btn-ghost btn-sm wl-remove" data-sym="' + sym + '" style="color:var(--negative);border-color:transparent;">✕</button></div></div>';
    }).join('');
    // Re-bind remove buttons
    list.querySelectorAll('.wl-remove').forEach(function(btn) {
      btn.addEventListener('click', function(e) {
        e.stopPropagation();
        removeFromWatchlist(this.getAttribute('data-sym'));
        renderWatchlist();
        updateWlCount();
      });
    });
    updateWlCount();
  }

  function updateWlCount() {
    var count = document.getElementById('wlCount');
    var symbols = getWatchlist();
    if (count) count.textContent = symbols.length + ' asset' + (symbols.length !== 1 ? 's' : '');
  }

  addBtn.addEventListener('click', function() {
    if (addToWatchlist(input.value)) {
      renderWatchlist();
      input.value = '';
    }
  });
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') addBtn.click();
  });

  // Suggestion token clicks
  document.querySelectorAll('.suggest-token').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.getElementById('wlInput').value = this.getAttribute('data-symbol');
      addBtn.click();
    });
  });

  renderWatchlist();
}

/* ============================
   ANALYST PAGE
   ============================ */
function initAnalystPage() {
  var params = new URLSearchParams(window.location.search);
  var tokenFilter = params.get('token');
  var analystFilter = params.get('analyst');
  var container = document.getElementById('signalContainer');
  var gridEl = document.getElementById('analystGrid');
  var headerEl = document.getElementById('coinHeader');
  if (!container) return;

  // Render analyst grid
  if (gridEl && !gridEl.hasChildNodes()) {
    ALL_SOURCES.forEach(function(a) {
      var isAuto = AUTO_SOURCES.indexOf(a) !== -1;
      var card = document.createElement('a');
      card.className = 'glass-sm analyst-card' + (isAuto ? ' auto-source' : '') + (a.id === analystFilter ? ' selected' : '');
      card.href = 'analyst.html' + (a.id ? '?analyst=' + a.id : '');
      if (tokenFilter) card.href += (a.id ? '&' : '?') + 'token=' + tokenFilter;
      card.innerHTML =
        '<div class="ac-avatar" style="background:' + a.color + ';">' + a.initials + '</div>' +
        '<div class="ac-info">' +
        '<div class="ac-name">' + a.name + '</div>' +
        '<div class="ac-role">' + a.role + '</div>' +
        (isAuto ? '<div class="ac-auto-badge">Automated</div>' :
          '<div class="ac-followers">' + (a.followers / 1000).toFixed(1) + 'k followers</div>') +
        '</div>';
      gridEl.appendChild(card);
    });
  }

  // Coin header
  var TOKEN_META = {
    BTC: { name:'Bitcoin', color:'#ff6b35', price:67421 },
    ETH: { name:'Ethereum', color:'#6c5ce7', price:3482 },
    SOL: { name:'Solana', color:'#ffc107', price:145.82 },
    LINK: { name:'Chainlink', color:'#17a34a', price:16.24 },
    ARB: { name:'Arbitrum', color:'#0ea5e9', price:0.84 },
    OP: { name:'Optimism', color:'#dc3545', price:2.84 },
    DOT: { name:'Polkadot', color:'#ec4899', price:7.82 },
    AAVE: { name:'Aave', color:'#8b5cf6', price:142.50 },
    UNI: { name:'Uniswap', color:'#f97316', price:9.84 },
    PEPE: { name:'Pepe', color:'#14b8a6', price:0.000012 },
    ATOM: { name:'Cosmos', color:'#a855f7', price:9.12 },
    PENDLE: { name:'Pendle', color:'#f59e0b', price:6.42 },
    LDO: { name:'Lido DAO', color:'#5865F2', price:2.42 },
    DOGE: { name:'Dogecoin', color:'#64748b', price:0.14 },
    MKR: { name:'Maker', color:'#475569', price:2980 },
    SEI: { name:'Sei', color:'#94a3b8', price:0.62 }
  };

  if (headerEl && tokenFilter && TOKEN_META[tokenFilter]) {
    var t = TOKEN_META[tokenFilter];
    headerEl.style.display = '';
    headerEl.innerHTML =
      '<div class="ch-icon" style="background:' + t.color + ';color:#fff;">◉</div>' +
      '<div class="ch-info">' +
      '<h2>' + tokenFilter + ' <span style="font-size:var(--text-base);font-weight:400;color:var(--muted);">' + t.name + '</span></h2>' +
      '<div class="ch-price">$' + Number(t.price).toLocaleString() + '</div>' +
      '</div>' +
      '<div style="display:flex;gap:var(--space-2);">' +
      '<a href="analyst.html" class="btn btn-ghost btn-sm">Clear filter</a>' +
      '</div>';
  } else if (headerEl) {
    headerEl.style.display = 'none';
  }

  // Filter signals
  var filtered = SIGNAL_DATA.filter(function(s) {
    if (tokenFilter && s.token !== tokenFilter) return false;
    if (analystFilter && s.analyst !== analystFilter) return false;
    return true;
  });

  if (filtered.length === 0) {
    container.innerHTML = '<div class="glass" style="padding:var(--space-8);text-align:center;"><div style="font-size:var(--text-2xl);margin-bottom:var(--space-3);color:var(--muted);">—</div><div style="color:var(--muted);">No signals match this filter</div></div>';
    return;
  }

  container.innerHTML = filtered.map(function(s) {
    var analyst = ALL_SOURCES.find(function(a) { return a.id === s.analyst; });
    var analystName = analyst ? analyst.name : s.analyst;
    var analystInitials = analyst ? analyst.initials : '?';
    var analystColor = analyst ? analyst.color : '#666';
    var sigLabel = s.signal.charAt(0).toUpperCase() + s.signal.slice(1);
    var sigClass = s.signal === 'buy' ? 'badge-positive' : s.signal === 'sell' ? 'badge-negative' : 'badge-muted';
    var convClass = s.conviction === 'high' ? 'badge-accent' : 'badge-muted';
    var convLabel = s.conviction === 'high' ? 'High conviction' : s.conviction === 'medium' ? 'Medium conviction' : 'Low conviction';
    var isAuto = AUTO_SOURCES.some(function(a) { return a.id === s.analyst; });

    return '<div class="signal-card glass" data-signal="' + s.signal + '">' +
      '<div class="signal-top">' +
      '<div style="display:flex;align-items:center;gap:var(--space-3);">' +
      '<div class="sq-icon" style="width:44px;height:44px;border-radius:50%;background:' + analystColor + ';color:#fff;display:grid;place-items:center;font-size:var(--text-sm);font-weight:700;flex-shrink:0;">' + analystInitials + '</div>' +
      '<div>' +
      '<div style="display:flex;align-items:center;gap:var(--space-2);">' +
      '<a href="analyst.html?analyst=' + s.analyst + '" style="font-weight:600;font-size:var(--text-sm);color:var(--fg);">' + analystName + '</a>' +
      (isAuto ? '<span class="badge badge-muted" style="font-size:9px;">🤖 AUTO</span>' : '') +
      '</div>' +
      '<div style="font-size:var(--text-xs);color:var(--muted);">' + s.time + ' · <a href="analyst.html?token=' + s.token + '" style="color:var(--accent);">' + s.token + '</a></div>' +
      '</div></div>' +
      '<div class="row gap-2">' +
      '<span class="badge ' + sigClass + '">' + sigLabel + '</span>' +
      '<span class="badge ' + convClass + '">' + convLabel + '</span>' +
      '</div></div>' +
      '<div class="signal-body">' +
      '<div class="signal-metric"><div class="label">Current Price</div><div class="value">$' + Number(s.price).toLocaleString() + '</div></div>' +
      '<div class="signal-metric"><div class="label">Target</div><div class="value" style="' + (s.signal === 'buy' ? 'color:var(--positive)' : s.signal === 'sell' ? 'color:var(--negative)' : '') + '">' + (typeof s.target === 'number' ? '$' + Number(s.target).toLocaleString() : s.target) + '</div></div>' +
      '<div class="signal-metric"><div class="label">Stop Loss</div><div class="value" style="' + (s.signal === 'sell' ? 'color:var(--positive)' : s.signal === 'buy' ? 'color:var(--negative)' : '') + '">' + (s.stop ? '$' + Number(s.stop).toLocaleString() : '—') + '</div></div>' +
      '<div class="signal-metric"><div class="label">Risk / Reward</div><div class="value">' + s.rrw + '</div></div>' +
      '</div>' +
      '<div class="signal-reason">' + s.reason + '</div>' +
      '</div>';
  }).join('');

  // Filter button interactivity
  document.querySelectorAll('.filter-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
      this.classList.add('active');
      var filter = this.getAttribute('data-filter');
      document.querySelectorAll('.signal-card').forEach(function(card) {
        card.style.display = (filter === 'all' || card.getAttribute('data-signal') === filter) ? '' : 'none';
      });
    });
  });
}

/* ============================
   POSITION DETAIL TOGGLE
   ============================ */
function initPositions() {
  document.querySelectorAll('.pos-card').forEach(function(card) {
    card.addEventListener('click', function(e) {
      if (e.target.closest('button')) return;
      var detail = this.querySelector('.pos-detail');
      if (detail) detail.classList.toggle('open');
    });
  });
}

/* ============================
   SETTINGS PERSISTENCE
   ============================ */
function initSettings() {
  document.querySelectorAll('.toggle input[type="checkbox"]').forEach(function(cb) {
    cb.addEventListener('change', function() {
      var key = 'setting:' + (this.id || this.name);
      try { localStorage.setItem(key, this.checked ? '1' : '0'); } catch(e) {}
    });
    var key = 'setting:' + (cb.id || cb.name);
    try {
      var saved = localStorage.getItem(key);
      if (saved !== null) cb.checked = saved === '1';
    } catch(e) {}
  });
  // Select persistence
  document.querySelectorAll('.setting-row select').forEach(function(sel) {
    var key = 'setting:' + (sel.id || sel.name);
    sel.addEventListener('change', function() {
      try { localStorage.setItem(key, this.value); } catch(e) {}
    });
    try {
      var saved = localStorage.getItem(key);
      if (saved !== null) sel.value = saved;
    } catch(e) {}
  });
}

/* ============================
   PORTFOLIO IMPORT MODAL
   ============================ */
function initImportModal() {
  var overlay = document.getElementById('importOverlay');
  var openBtn = document.getElementById('importPortfolioBtn');
  var closeBtn = document.getElementById('importCloseBtn');
  if (!overlay || !openBtn) return;

  openBtn.addEventListener('click', function() { overlay.classList.add('open'); });
  if (closeBtn) closeBtn.addEventListener('click', function() { overlay.classList.remove('open'); });
  overlay.addEventListener('click', function(e) {
    if (e.target === overlay) overlay.classList.remove('open');
  });

  // Tab switching
  document.querySelectorAll('.import-tabs .it').forEach(function(tab) {
    tab.addEventListener('click', function() {
      document.querySelectorAll('.import-tabs .it').forEach(function(t) { t.classList.remove('active'); });
      this.classList.add('active');
    });
  });

  // Mock import action
  var mockImport = document.getElementById('mockImportBtn');
  if (mockImport) {
    mockImport.addEventListener('click', function() {
      var preview = document.querySelector('.import-preview');
      if (preview) {
        preview.classList.add('show');
        preview.innerHTML =
          '<div class="section-header" style="margin-bottom:var(--space-3);"><span style="font-weight:600;">Imported Holdings</span><span class="badge badge-accent">4 assets</span></div>' +
          '<table><thead><tr><th>Asset</th><th>Amount</th><th>Avg Price</th></tr></thead><tbody>' +
          '<tr><td>BTC</td><td class="mono">0.5</td><td class="mono">$62,400</td></tr>' +
          '<tr><td>ETH</td><td class="mono">5.0</td><td class="mono">$3,350</td></tr>' +
          '<tr><td>SOL</td><td class="mono">30</td><td class="mono">$128.00</td></tr>' +
          '<tr><td>ARB</td><td class="mono">1000</td><td class="mono">$0.88</td></tr>' +
          '</tbody></table>' +
          '<div style="margin-top:var(--space-4);display:flex;gap:var(--space-2);">' +
          '<button class="btn btn-primary btn-sm" onclick="document.getElementById(\'importOverlay\').classList.remove(\'open\');">Confirm Import</button>' +
          '<button class="btn btn-ghost btn-sm">Cancel</button></div>';
      }
    });
  }
}

/* ============================
   LOGO DROPDOWN
   ============================ */
function initLogoDropdown() {
  var logo = document.querySelector('.nav-bar .logo');
  var dropdown = document.getElementById('logoDropdown');
  if (!logo || !dropdown) return;
  logo.addEventListener('click', function(e) {
    e.stopPropagation();
    dropdown.classList.toggle('open');
  });
  document.addEventListener('click', function() { dropdown.classList.remove('open'); });
  dropdown.addEventListener('click', function(e) { e.stopPropagation(); });
}

/* ============================
   SUBSCRIPTION
   ============================ */
function initSubscription() {
  var subCards = document.querySelectorAll('.sub-card');
  subCards.forEach(function(card) {
    var btn = card.querySelector('.sub-btn');
    if (btn) {
      btn.addEventListener('click', function() {
        if (canAccessPremium()) {
          alert('You already have Premium access!');
        } else {
          // Mock upgrade — set premium
          var auth = getAuth();
          if (auth) {
            auth.premium = true;
            setAuth(auth);
            initAuth();
            updatePremiumUI();
            alert('Upgraded to Premium! 🎉');
          } else {
            document.getElementById('authOverlay').classList.add('open');
          }
        }
      });
    }
  });
}

/* ============================
   PULSE EFFECTS
   ============================ */
function initPulseEffects() {
  document.querySelectorAll('[data-pulse]').forEach(function(el) {
    var interval = parseInt(el.getAttribute('data-pulse'), 10) || 2000;
    el.classList.add('pulse-glow');
  });
}

/* ============================
   INIT ALL
   ============================ */
document.addEventListener('DOMContentLoaded', function() {
  initLogoDropdown();
  initNotifications();
  initWatchlist();
  initPositions();
  initSettings();
  initAuth();
  updatePremiumUI();
  initPulseEffects();
  initImportModal();
  initSubscription();
  // Analyst page only
  if (document.getElementById('signalContainer')) initAnalystPage();
});
