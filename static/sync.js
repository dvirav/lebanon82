/* =====================================================================
   window.claude מעל WebSocket.
   בונה room ו־user עם אותו ממשק כמו ב־claude.ai, כך שהמנוע ב־index.html
   לא צריך לדעת שהוא רץ על אתר רגיל. נטען לפני הסקריפט הראשי.
   ===================================================================== */
(() => {
  const ls = {
    get: (s, k) => { try { return s.getItem(k); } catch (e) { return null; } },
    set: (s, k, v) => { try { s.setItem(k, v); } catch (e) {} },
  };
  const rand = () => (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36));

  // מזהה קבוע לטלפון: רענון לא מכפיל קול.
  let clientId = ls.get(localStorage, 'lb82-client') || rand();
  ls.set(localStorage, 'lb82-client', clientId);

  // המפתח מגיע ב־URL, נשמר ללשונית הזאת בלבד, ונמחק משורת הכתובת כדי שלא יופיע על המקרן.
  const url = new URL(location.href);
  const urlKey = url.searchParams.get('key');
  if (urlKey) ls.set(sessionStorage, 'lb82-key', urlKey);
  const key = urlKey || ls.get(sessionStorage, 'lb82-key') || '';
  const urlRole = url.searchParams.get('role');
  if (urlRole && ['screen', 'remote', 'viewer', 'local', 'script'].includes(urlRole)) ls.set(localStorage, 'lb82-role', urlRole);
  if (urlKey || urlRole) {
    url.searchParams.delete('key'); url.searchParams.delete('role');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
  }

  const handlers = {}, peerFns = [], connFns = [];
  let peers = [], admin = false, isConnected = false, ws = null, retry = 0, retryTimer = null;
  let myPresence = {};
  let welcomed; const welcome = new Promise(r => { welcomed = r; });

  const freezePeer = p => Object.freeze({
    peer: p.peer, by: null, isMe: p.peer === clientId, sameTab: p.peer === clientId,
    kind: 'viewer', guest: false, presence: Object.freeze({...(p.presence || {})}),
  });
  function setPeers(list, fromServer) {
    const before = new Set(peers.map(p => p.peer));
    peers = Object.freeze((Array.isArray(list) ? list : []).filter(p => p && typeof p.peer === 'string').map(freezePeer));
    const joined = fromServer ? peers.filter(p => !before.has(p.peer)) : [];
    const left = fromServer ? [...before].filter(id => !peers.some(p => p.peer === id)) : [];
    peerFns.forEach(f => { try { f({peers, joined, left, updated: []}); } catch (e) { console.error(e); } });
  }
  function setConnected(on) {
    if (on === isConnected) return;
    isConnected = on;
    connFns.forEach(f => { try { f(on); } catch (e) { console.error(e); } });
  }
  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(msg)); return true; }
    return false;
  }
  // ה־state שבא עם welcome מגיע לפני שהמנוע נרשם ל־on('state'), לכן שומרים אותו ומעבירים למי שנרשם.
  let lastState = null;
  const stateMsg = data => ({topic: 'state', data, peer: null, by: null, isMe: false, sameTab: false, kind: 'viewer', guest: false});
  function deliver(data) {
    lastState = data;
    (handlers.state || []).forEach(f => { try { f(stateMsg(data)); } catch (e) { console.error(e); } });
  }

  // מספר הפריטים ומפת הסקרים, מתוך הקבועים של הסקריפט הראשי. השרת משתמש בזה כדי לבדוק הצבעות.
  function meta() {
    try {
      const polls = {};
      for (const pid in POLL_IDX) polls[pid] = {step: POLL_IDX[pid] + 1, n: ITEMS[POLL_IDX[pid]].options.length};
      return {n: N, polls};
    } catch (e) { return null; }
  }

  function connect() {
    clearTimeout(retryTimer);
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    const sock = ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    sock.onopen = () => {
      sock.send(JSON.stringify({type: 'hello', clientId, key, role: myPresence.role, vote: myPresence.vote || null}));
    };
    sock.onmessage = ev => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (!m || typeof m !== 'object') return;
      if (m.type === 'welcome') {
        retry = 0;
        if (typeof m.clientId === 'string' && m.clientId !== clientId) { clientId = m.clientId; ls.set(localStorage, 'lb82-client', clientId); }
        admin = !!m.admin;
        welcomed(admin);
        setPeers(m.peers, true);
        setConnected(true);
        if (m.state) deliver(m.state);
      } else if (m.type === 'state') deliver(m.data);
      else if (m.type === 'peers') setPeers(m.peers, true);
    };
    sock.onclose = () => {
      if (ws !== sock) return;
      setConnected(false);
      retry = Math.min(retry + 1, 5);
      retryTimer = setTimeout(connect, retry * 1000);
    };
  }
  connect();
  // טלפון שחוזר מנעילת מסך: להתחבר מיד ולא לחכות לטיימר.
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') connect(); });
  addEventListener('online', connect);
  setInterval(() => send({type: 'ping'}), 25000);

  const room = Object.freeze({
    emit: async (topic, data) => {
      if (!admin) throw {code: 'not_permitted'};
      if (topic !== 'state') return;
      if (!send({type: 'state', data, meta: meta()})) throw {code: 'not_connected'};
    },
    on: (topic, fn) => {
      (handlers[topic] ||= []).push(fn);
      if (topic === 'state' && lastState) setTimeout(() => { if (lastState) fn(stateMsg(lastState)); }, 0);
      return () => { handlers[topic] = handlers[topic].filter(f => f !== fn); };
    },
    presence: async patch => {
      myPresence = {...myPresence, ...patch};
      for (const k in myPresence) if (myPresence[k] === null) delete myPresence[k];
      // עדכון מקומי מיידי, כדי שהצופה יראה את הקול שלו. השרת שולח אחר כך את הספירה האמיתית.
      const mine = {peer: clientId, presence: myPresence};
      setPeers([...peers.filter(p => p.peer !== clientId), mine], false);
      send({type: 'presence', role: myPresence.role, vote: myPresence.vote || null});
    },
    peers: () => peers,
    onPeers: fn => {
      peerFns.push(fn);
      setTimeout(() => fn({peers, joined: [], left: [], updated: []}), 0);
      return () => { const i = peerFns.indexOf(fn); if (i >= 0) peerFns.splice(i, 1); };
    },
    connected: () => isConnected,
    onConnection: fn => {
      connFns.push(fn);
      setTimeout(() => fn(isConnected), 0);
      return () => { const i = connFns.indexOf(fn); if (i >= 0) connFns.splice(i, 1); };
    },
  });

  // המנוע שואל פעם אחת, בעלייה, אם המכשיר של המציג. מחכים לתשובה מהשרת (עד 10 שניות).
  const canEdit = () => Promise.race([welcome, new Promise(r => setTimeout(() => r(admin), 10000))]);
  const user = Object.freeze({canEdit, isOwner: canEdit});

  window.claude = Object.freeze({use: async name => name === 'room' ? room : name === 'user' ? user : null});
})();
