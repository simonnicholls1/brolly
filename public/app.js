const $ = (id) => document.getElementById(id);
const REFRESH_MS = 5 * 60 * 1000;

// ---------- small helpers ----------
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};

function clientId() {
  let id = store.get('brolly:client');
  if (!id) {
    id = (crypto.randomUUID?.() ?? Math.random().toString(36).slice(2) + Date.now().toString(36));
    store.set('brolly:client', id);
  }
  return id;
}

const WMO = {
  0: 'Clear', 1: 'Mostly clear', 2: 'Partly cloudy', 3: 'Overcast', 45: 'Fog', 48: 'Freezing fog',
  51: 'Light drizzle', 53: 'Drizzle', 55: 'Heavy drizzle', 56: 'Freezing drizzle', 57: 'Freezing drizzle',
  61: 'Light rain', 63: 'Rain', 65: 'Heavy rain', 66: 'Freezing rain', 67: 'Freezing rain',
  71: 'Light snow', 73: 'Snow', 75: 'Heavy snow', 77: 'Snow grains',
  80: 'Showers', 81: 'Showers', 82: 'Heavy showers', 85: 'Snow showers', 86: 'Snow showers',
  95: 'Thunderstorm', 96: 'Thunderstorm, hail', 99: 'Thunderstorm, hail',
};

function skyFor(w) {
  if (!w) return 'cloudy';
  const c = w.code;
  if (c >= 95) return 'storm';
  if ([71, 73, 75, 77, 85, 86].includes(c) || w.snowfall > 0) return 'snow';
  if ((c >= 51 && c <= 67) || (c >= 80 && c <= 82) || w.precip >= 0.1) return 'rain';
  if (c === 45 || c === 48) return 'fog';
  if (c === 3) return 'cloudy';
  if (c === 2 || c === 1) return w.is_day ? 'partly-day' : 'partly-night';
  return w.is_day ? 'clear-day' : 'clear-night';
}

// ---------- background particles (rain / snow / stars) ----------
const particles = (() => {
  const canvas = $('particles');
  const ctx = canvas.getContext('2d');
  let mode = null, items = [], raf = 0, intensity = 1;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

  function resize() {
    canvas.width = innerWidth * devicePixelRatio;
    canvas.height = innerHeight * devicePixelRatio;
  }
  addEventListener('resize', resize);
  resize();

  function spawn(initial) {
    const W = canvas.width, H = canvas.height, d = devicePixelRatio;
    if (mode === 'rain') return { x: Math.random() * W * 1.2, y: initial ? Math.random() * H : -20 * d, v: (14 + Math.random() * 8) * d, l: (12 + Math.random() * 14) * d };
    if (mode === 'snow') return { x: Math.random() * W, y: initial ? Math.random() * H : -10 * d, v: (0.6 + Math.random() * 1.2) * d, r: (1.5 + Math.random() * 2.5) * d, p: Math.random() * 6.28 };
    if (mode === 'stars') return { x: Math.random() * W, y: Math.random() * H * 0.7, r: Math.random() * 1.4 * d, p: Math.random() * 6.28 };
  }

  function frame() {
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (mode === 'rain') {
        ctx.strokeStyle = 'rgba(210,225,255,0.45)';
        ctx.lineWidth = devicePixelRatio;
        ctx.beginPath(); ctx.moveTo(it.x, it.y); ctx.lineTo(it.x - it.l * 0.25, it.y + it.l); ctx.stroke();
        it.y += it.v; it.x -= it.v * 0.25;
        if (it.y > H) items[i] = spawn(false);
      } else if (mode === 'snow') {
        it.p += 0.02;
        ctx.fillStyle = 'rgba(255,255,255,0.85)';
        ctx.beginPath(); ctx.arc(it.x + Math.sin(it.p) * 6, it.y, it.r, 0, 6.28); ctx.fill();
        it.y += it.v;
        if (it.y > H) items[i] = spawn(false);
      } else if (mode === 'stars') {
        it.p += 0.015;
        ctx.fillStyle = `rgba(255,255,255,${0.35 + Math.sin(it.p) * 0.3})`;
        ctx.beginPath(); ctx.arc(it.x, it.y, it.r, 0, 6.28); ctx.fill();
      }
    }
    if (!reduced) raf = requestAnimationFrame(frame);
  }

  return {
    set(newMode, level = 1) {
      if (newMode === mode && level === intensity) return;
      mode = newMode; intensity = level;
      cancelAnimationFrame(raf);
      const area = (innerWidth * innerHeight) / (390 * 844);
      const n = { rain: 140, snow: 90, stars: 120 }[mode] ?? 0;
      items = Array.from({ length: Math.round(n * intensity * Math.max(area, 0.6)) }, () => spawn(true));
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (mode) frame();
    },
  };
})();

function setSky(w) {
  const sky = skyFor(w);
  document.body.dataset.sky = sky;
  const heavy = w && (w.precip >= 2 || [65, 82, 75].includes(w.code)) ? 1.8 : w && w.precip < 0.5 ? 0.7 : 1;
  particles.set(
    sky === 'rain' || sky === 'storm' ? 'rain' : sky === 'snow' ? 'snow' : sky.endsWith('night') ? 'stars' : null,
    sky === 'clear-night' || sky === 'partly-night' ? (sky === 'partly-night' ? 0.4 : 1) : heavy,
  );
  const meta = document.querySelector('meta[name="theme-color"]');
  meta.content = getComputedStyle(document.body).getPropertyValue('--sky-top').trim() || '#1d2b3a';
}

// ---------- state ----------
let place = null; // {lat, lon, label}
let refreshTimer = 0;

function showSearch(reason) {
  $('search').hidden = false;
  $('search-reason').textContent = reason;
  $('answer').textContent = '?';
  $('headline').textContent = 'Where are you?';
  $('where').textContent = '';
  setSky(null);
  $('search-input').focus({ preventScroll: true });
}

function locate() {
  $('headline').textContent = 'Finding where you are';
  if (!('geolocation' in navigator)) return showSearch('Your browser can’t share its location. Search for a place instead.');
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      place = { lat: pos.coords.latitude, lon: pos.coords.longitude, label: 'Near you' };
      $('search').hidden = true;
      load();
    },
    (err) => {
      const saved = store.get('brolly:place');
      if (saved) { place = saved; $('search').hidden = true; return load(); }
      showSearch(
      err.code === err.PERMISSION_DENIED
        ? 'Location is switched off for this site. Search for a place instead.'
        : 'We couldn’t find your location. Search for a place instead.',
      );
    },
    { enableHighAccuracy: false, timeout: 12000, maximumAge: 10 * 60 * 1000 },
  );
}

async function load() {
  clearTimeout(refreshTimer);
  if (!place) return;
  try {
    const r = await fetch(`/api/verdict?lat=${place.lat}&lon=${place.lon}`);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `Error ${r.status}`);
    render(await r.json());
  } catch (e) {
    $('answer').textContent = '?';
    $('headline').textContent = 'Couldn’t work it out right now';
    $('where').textContent = e.message;
  }
  refreshTimer = setTimeout(load, REFRESH_MS);
}

// ---------- rendering ----------
function render(d) {
  const ans = $('answer');
  const text = d.answer === 'yes' ? 'Yes' : 'No';
  if (ans.textContent !== text) { ans.classList.remove('pop'); void ans.offsetWidth; ans.classList.add('pop'); }
  ans.textContent = text;
  ans.style.color = d.answer === 'yes' ? 'var(--yes)' : 'var(--no)';
  $('headline').textContent = d.headline;
  const w = d.weather;
  $('where').textContent = [place.label, w && `${Math.round(w.temp)}°C`, w && WMO[w.code]].filter(Boolean).join(' · ');
  document.title = `${text}, ${d.answer === 'yes' ? 'bring a coat' : 'no coat needed'} · Do I need a coat?`;
  setSky(w);

  $('stats').hidden = false;
  $('vote').hidden = false;
  $('change-place').hidden = false;

  // meter
  const pct = Math.round(d.probability * 100);
  $('meter-fill').style.left = `${pct}%`;
  $('meter-pct').textContent = `${pct}% coat · ${d.confidence}`;

  // signals
  $('signals').replaceChildren(...d.signals.map(signalRow));

  // timeline
  const tl = $('timeline');
  tl.querySelectorAll('.dot').forEach((n) => n.remove());
  const win = d.live.window_minutes;
  for (const v of d.live.votes) {
    const dot = document.createElement('span');
    dot.className = `dot ${v.needed ? 'yes' : 'no'}`;
    dot.style.left = `calc(10px + (100% - 20px) * ${1 - v.minutes_ago / win})`;
    dot.style.opacity = String(0.35 + 0.65 * (1 - v.minutes_ago / win));
    dot.title = `${v.needed ? 'Needed a coat' : 'No coat'} · ${v.minutes_ago} min ago`;
    tl.append(dot);
  }
  const yes = d.live.votes.filter((v) => v.needed).length;
  const no = d.live.votes.length - yes;
  $('live-sub').textContent = `within ${d.live.radius_km} km`;
  $('live-note').textContent = d.live.votes.length
    ? `🟡 ${yes} needed one · 🟢 ${no} didn’t. Older votes fade out and drop off after 2 hours.`
    : 'Nobody nearby has voted in the last 2 hours, so this is based on the weather. Be the first!';

  // weather grid
  const cells = w ? [
    ['Feels like', `${Math.round(w.feels_like)}°C`],
    ['Temperature', `${Math.round(w.temp)}°C`],
    ['Rain chance (3h)', w.rain_chance != null ? `${Math.round(w.rain_chance)}%` : '–'],
    ['Wind', w.wind != null ? `${Math.round(w.wind)} km/h` : '–'],
    ['Conditions', WMO[w.code] ?? '–'],
  ] : [['Weather', 'Unavailable']];
  $('weather-grid').replaceChildren(...cells.map(([k, v]) => {
    const div = document.createElement('div');
    div.innerHTML = '<dt></dt><dd></dd>';
    div.children[0].textContent = k; div.children[1].textContent = v;
    return div;
  }));

  const c = d.climate;
  $('climate-text').textContent = c
    ? `Around this date it’s usually ${c.temp_mean}°C${c.feels_like_mean != null ? ` (feels like ${c.feels_like_mean}°C)` : ''}, with rain on ${Math.round(c.wet_day_pct * 100)}% of days. That’s from ${c.years} years of records. Today is ${w ? describeDiff(w.temp - c.temp_mean) : 'unknown'}.`
    : 'No historical records available for here.';

  renderVoteState();
}

function describeDiff(diff) {
  const a = Math.abs(diff).toFixed(0);
  if (Math.abs(diff) < 1.5) return 'about normal';
  return diff > 0 ? `${a}°C warmer than usual` : `${a}°C colder than usual`;
}

function signalRow(s) {
  const li = document.createElement('li');
  li.className = `signal${s.share > 0 ? '' : ' off'}`;
  const d = s.detail || {};
  let why = '';
  if (s.key === 'live') why = d.count ? `${d.yes} said yes, ${d.no} said no` : 'No votes nearby in the last 2 hours';
  else if (s.key === 'forecast') why = (d.reasons || []).join(' · ');
  else if (s.key === 'similar') why = d.count ? `${d.yes} of ${d.count} people needed one when it felt like this` : 'Not enough past votes in weather like this yet';
  else if (s.key === 'climate') why = s.p != null ? `Usually ${d.temp_mean}°C here on this date` : 'No records';
  const lean = s.p == null ? 'no data' : `${s.p >= 0.5 ? 'coat' : 'no coat'} ${Math.round((s.p >= 0.5 ? s.p : 1 - s.p) * 100)}%`;
  li.innerHTML = '<span class="name"></span><span class="lean"></span><span class="bar"><span></span></span><span class="why"></span>';
  li.querySelector('.name').textContent = s.label;
  li.querySelector('.lean').textContent = s.share > 0 ? `${lean} · ${Math.round(s.share * 100)}% say` : lean;
  li.querySelector('.bar span').style.width = `${Math.round(s.share * 100)}%`;
  li.querySelector('.why').textContent = why;
  return li;
}

// ---------- voting ----------
function renderVoteState() {
  const last = store.get('brolly:lastVote');
  const fresh = last && Date.now() - last.at < 2 * 60 * 60 * 1000;
  document.querySelectorAll('.vote-buttons button').forEach((b) => {
    b.classList.toggle('selected', !!fresh && String(last.needed) === b.dataset.needed);
  });
  $('vote-status').textContent = fresh
    ? `You said you ${last.needed ? 'needed' : 'didn’t need'} one ${Math.max(1, Math.round((Date.now() - last.at) / 60000))} min ago. Tap to change it.`
    : 'Did you need a coat?';
}

document.querySelectorAll('.vote-buttons button').forEach((btn) => {
  btn.addEventListener('click', async () => {
    if (!place) return;
    const needed = btn.dataset.needed === 'true';
    const buttons = document.querySelectorAll('.vote-buttons button');
    buttons.forEach((b) => (b.disabled = true));
    try {
      const r = await fetch('/api/vote', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ lat: place.lat, lon: place.lon, needed, client_id: clientId() }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Vote failed');
      store.set('brolly:lastVote', { at: Date.now(), needed });
      await load();
      $('vote-status').textContent = 'Thanks! Your vote counts for the next 2 hours.';
    } catch (e) {
      $('vote-status').textContent = e.message;
    } finally {
      buttons.forEach((b) => (b.disabled = false));
    }
  });
});

// ---------- search ----------
$('search-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = $('search-input').value.trim();
  const list = $('search-results');
  list.replaceChildren();
  try {
    const r = await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
    if (!r.ok) throw new Error('Search is unavailable right now');
    const { results } = await r.json();
    if (!results.length) { list.innerHTML = '<li class="muted">No places found</li>'; return; }
    list.replaceChildren(...results.map((p) => {
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = p.region ? `${p.name}, ${p.region}` : p.name;
      b.onclick = () => {
        place = { lat: p.lat, lon: p.lon, label: p.name };
        store.set('brolly:place', place);
        $('search').hidden = true;
        list.replaceChildren();
        load();
      };
      li.append(b);
      return li;
    }));
  } catch (err) {
    list.innerHTML = '';
    const li = document.createElement('li'); li.className = 'muted'; li.textContent = err.message; list.append(li);
  }
});

$('retry-geo').addEventListener('click', locate);
$('change-place').addEventListener('click', () => showSearch('Search for a place, or use your location.'));
document.addEventListener('visibilitychange', () => { if (!document.hidden && place) load(); });

locate();
