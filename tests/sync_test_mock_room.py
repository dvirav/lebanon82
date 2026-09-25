import json, sys
from playwright.sync_api import sync_playwright

URL = "file:///home/claude/lebanon82/index.html"
SP = "/tmp/claude-0/-home-claude/4a99d292-7a1b-5d15-8c78-52edbce52b93/scratchpad/"

MOCK = r"""
(() => {
  const ADMIN = %s;
  const peer = Math.random().toString(36).slice(2,10);
  const ch = new BroadcastChannel('mockroom');
  const handlers = {}, peerFns = [], connFns = [];
  const peers = new Map();
  let myPresence = {};
  const snapshot = () => Object.freeze([...peers.values()]);
  function notify(joined){ const p = snapshot(); peerFns.forEach(f=>f({peers:p, joined:joined||[], left:[], updated:[]})); }
  function upsert(pid, presence, isMe){ const existed = peers.has(pid);
    const obj = Object.freeze({peer:pid, by:null, isMe, sameTab:isMe, kind:'viewer', guest:false, presence:Object.freeze(presence), updatedAt:Date.now()});
    peers.set(pid, obj); notify(existed?[]:[obj]); }
  upsert(peer, {}, true);
  ch.onmessage = (ev) => { const m = ev.data;
    if(m.type==='emit'){ (handlers[m.topic]||[]).forEach(f=>f({topic:m.topic, data:m.data, peer:m.peer, by:null, isMe:false, sameTab:false, kind:'viewer', guest:false})); }
    if(m.type==='presence'){ upsert(m.peer, m.presence, false); }
    if(m.type==='hello'){ ch.postMessage({type:'presence', peer, presence:myPresence}); if(!peers.has(m.peer)) upsert(m.peer, {}, false); }
  };
  const room = Object.freeze({
    emit: async (topic, data) => { if(!ADMIN) throw {code:'not_permitted'}; ch.postMessage({type:'emit', topic, data: JSON.parse(JSON.stringify(data)), peer}); (handlers[topic]||[]).forEach(f=>f({topic, data, peer, isMe:true, sameTab:true})); },
    on: (topic, fn) => { (handlers[topic] ||= []).push(fn); return ()=>{}; },
    presence: async (patch) => { myPresence = Object.assign({}, myPresence, patch); for(const k in myPresence) if(myPresence[k]===null) delete myPresence[k]; upsert(peer, myPresence, true); ch.postMessage({type:'presence', peer, presence: myPresence}); },
    peers: () => snapshot(),
    onPeers: (fn) => { peerFns.push(fn); setTimeout(()=>fn({peers:snapshot(), joined:snapshot(), left:[], updated:[]}),0); return ()=>{}; },
    connected: () => true,
    onConnection: (fn) => { setTimeout(()=>fn(true), 10); return ()=>{}; },
  });
  const user = Object.freeze({ canEdit: async()=>ADMIN, isOwner: async()=>ADMIN });
  window.claude = { use: async (n) => { await new Promise(r=>setTimeout(r,20)); if(n==='room') { setTimeout(()=>ch.postMessage({type:'hello', peer}), 30); return room; } if(n==='user') return user; return null; } };
})();
"""

errors = []
def watch(pg, name):
    pg.on("pageerror", lambda e: errors.append(f"{name}: {e}"))
    pg.on("console", lambda m: m.type=="error" and "Failed to load resource" not in m.text and errors.append(f"{name} console: {m.text}"))

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width":1280,"height":800})
    ctx.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    ctx.route("**/fonts.gstatic.com/**", lambda r: r.abort())

    # screen (admin)
    screen = ctx.new_page(); watch(screen, "screen")
    screen.add_init_script(MOCK % "true")
    screen.goto(URL); screen.wait_for_timeout(400)
    screen.click("#roleScreen"); screen.wait_for_timeout(300)
    screen.screenshot(path=SP+"01_screen_cover.png")

    # remote (admin, phone size)
    ctx2 = ctx
    remote = ctx.new_page(); watch(remote, "remote")
    remote.set_viewport_size({"width":390,"height":844})
    remote.add_init_script(MOCK % "true")
    remote.goto(URL); remote.wait_for_timeout(400)
    if remote.is_visible("#picker") is False:
        remote.evaluate("showPicker()")
    remote.click("#roleRemote"); remote.wait_for_timeout(300)
    remote.screenshot(path=SP+"02_remote_start.png")

    # viewer (non-admin, phone)
    viewer = ctx.new_page(); watch(viewer, "viewer")
    viewer.set_viewport_size({"width":390,"height":844})
    viewer.add_init_script(MOCK % "false")
    viewer.goto(URL); viewer.wait_for_timeout(600)
    print("viewer mode:", viewer.evaluate("mode"), "wait:", viewer.inner_text("#waitMsg"))

    # advance from remote to first poll
    p1 = viewer.evaluate("POLL_IDX.p1")
    remote.click("#rStart")
    for _ in range(p1+1):
        remote.click("#rNext"); remote.wait_for_timeout(80)
    screen.wait_for_timeout(900); viewer.wait_for_timeout(900)
    print("steps remote/screen/viewer:", remote.evaluate("state.step"), screen.evaluate("state.step"), viewer.evaluate("state.step"), "expected", p1+1)
    # viewer votes option 1
    viewer.click("#poll-p1 .opt[data-i='0']"); viewer.wait_for_timeout(300)
    # remote adds 2 hands to option 2
    remote.click("#rTally .pm[data-i='1'][data-d='1']"); remote.click("#rTally .pm[data-i='1'][data-d='1']")
    remote.wait_for_timeout(600); screen.wait_for_timeout(300)
    print("screen tally p1:", screen.evaluate("tally('p1').total"), "remote tally:", remote.evaluate("tally('p1').total"), "viewer:", viewer.evaluate("tally('p1').total"))
    screen.screenshot(path=SP+"03_screen_poll.png")
    remote.screenshot(path=SP+"04_remote_poll.png")
    viewer.screenshot(path=SP+"05_viewer_poll.png")

    # close poll -> frozen
    remote.click("#rNext"); remote.wait_for_timeout(700)
    print("frozen p1 on screen:", screen.evaluate("state.frozen.p1"), "hint:", screen.inner_text("#hint-p1"))

    # late joiner viewer
    late = ctx.new_page(); watch(late, "late")
    late.set_viewport_size({"width":390,"height":844})
    late.add_init_script(MOCK % "false")
    late.goto(URL); late.wait_for_timeout(1500)
    print("late joiner step:", late.evaluate("state.step"), "vs", remote.evaluate("state.step"))

    # screen keyboard control also drives remote
    screen.keyboard.press("Space"); screen.wait_for_timeout(900)
    print("after screen key: screen", screen.evaluate("state.step"), "remote", remote.evaluate("state.step"))

    # run to the end via remote
    n = remote.evaluate("N")
    while remote.evaluate("state.step") < n:
        remote.click("#rNext"); remote.wait_for_timeout(40)
    screen.wait_for_timeout(900)
    print("end step:", screen.evaluate("state.step"), "lesson visible:", screen.is_visible("#lesson"))
    screen.screenshot(path=SP+"06_screen_closing.png")
    # back to lesson 2
    remote.click("#rPrev"); remote.click("#rPrev"); screen.wait_for_timeout(700)
    screen.screenshot(path=SP+"07_screen_lesson2.png")
    remote.screenshot(path=SP+"08_remote_lesson.png")
    # back into chat (before lessons)
    for _ in range(3): remote.click("#rPrev")
    screen.wait_for_timeout(800)
    print("back in chat: lesson hidden:", not screen.is_visible("#lesson"), "rows:", screen.locator("#chat .row").count())
    screen.screenshot(path=SP+"09_screen_chat_end.png")

    # reset
    remote.on("dialog", lambda d: d.accept())
    remote.click("#rReset"); screen.wait_for_timeout(800)
    print("after reset:", screen.evaluate("state.step"), screen.evaluate("state.started"), "cover:", screen.is_visible("#cover"))

    # script view
    remote.evaluate("showPicker()"); remote.click("#roleScript"); remote.wait_for_timeout(300)
    remote.set_viewport_size({"width":900,"height":1200})
    remote.screenshot(path=SP+"10_script.png", full_page=True)
    print("ERRORS:", errors)
    b.close()

# standalone file (no window.claude)
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width":1280,"height":800}); watch(pg,"local")
    pg.goto(URL); pg.wait_for_timeout(400)
    vis = [pg.is_visible(s) for s in ["#roleScreen","#roleRemote","#roleViewer","#roleLocal","#roleScript"]]
    print("local picker visible roles:", vis)
    pg.click("#roleLocal"); pg.wait_for_timeout(200)
    for _ in range(13): pg.keyboard.press("ArrowRight"); pg.wait_for_timeout(650)
    print("local step:", pg.evaluate("state.step"), "rows:", pg.locator("#chat .row").count())
    pg.click("#poll-p1 .opt[data-i='2']"); pg.wait_for_timeout(200)
    print("local manual:", pg.evaluate("tally('p1').total"))
    pg.screenshot(path=SP+"11_local.png")
    print("ERRORS:", errors)
    b.close()
