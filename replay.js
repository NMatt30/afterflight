(function () {
  var LOGGER = "http://127.0.0.1:8742";
  var chase = { distance: 25, height: 6, orbit: 20 };
  var chaseTimer = null;

  // Some of what this line says is a fact about the leg - no clip recorded,
  // the sim is in a menu - and stays true until something changes. The rest
  // is an event that has finished happening, and a line reading "stopped"
  // twenty minutes after the fact is not status, it is litter. Those fade.
  var STATUS_FADE_MS = 4000;

  function setStatus(el, text, fades, ms) {
    if (el && el._fade) { clearTimeout(el._fade); el._fade = null; }
    if (fades && el && text) {
      el._fade = setTimeout(function () {
        el._fade = null;
        if (el.textContent === text) el.textContent = "";
      }, ms || STATUS_FADE_MS);
    }
    return setStatusText(el, text);
  }

  function setStatusText(el, text) {
    if (el) el.textContent = text || "";
  }

  async function parseJson(resp) {
    var text = await resp.text();
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  async function postReplay(body) {
    var resp = await fetch(LOGGER + "/replay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
    var j = await parseJson(resp);
    if (!j) throw new Error("sim not running");
    return j;
  }

  async function stopReplay() {
    var resp = await fetch(LOGGER + "/replay/stop", { method: "POST" });
    return parseJson(resp);
  }

  async function postChase(params) {
    var resp = await fetch(LOGGER + "/replay/chase", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params || {})
    });
    return parseJson(resp);
  }

  async function listClips() {
    var resp = await fetch(LOGGER + "/clips", { cache: "no-store" });
    var j = await parseJson(resp);
    if (!j || !j.ok) return [];
    return j.clips || [];
  }

  function matchClip(clips, flightId, leg, kind) {
    var wantLeg = String(leg);
    for (var i = 0; i < clips.length; i++) {
      var c = clips[i];
      if (c.flight_id === flightId && String(c.leg) === wantLeg && c.kind === kind) return c;
    }
    return null;
  }

  function chaseStatusEls() {
    return Array.prototype.slice.call(document.querySelectorAll(".chase-status"));
  }

  function setChaseStatus(text) {
    chaseStatusEls().forEach(function (el) { el.textContent = text || ""; });
  }

  function applyChaseInputs(name, value) {
    Array.prototype.slice.call(document.querySelectorAll("[data-chase='" + name + "']")).forEach(function (el) {
      if (el.value !== String(value)) el.value = String(value);
    });
  }

  // The camera knobs never read anything back from the watcher, so a default
  // changed in Settings left the old number sitting in the boxes until a
  // reload. Sync from /state, but never fight the control being used.
  function syncChaseInputs(serverChase) {
    if (!serverChase) return;
    ["distance", "height", "orbit"].forEach(function (name) {
      var v = serverChase[name];
      if (typeof v !== "number" || !isFinite(v)) return;
      var active = document.activeElement;
      if (active && active.getAttribute && active.getAttribute("data-chase") === name) return;
      chase[name] = v;
      applyChaseInputs(name, v);
    });
  }

  function readChaseFromInputs() {
    function one(name, fallback) {
      var el = document.querySelector("[data-chase='" + name + "']");
      if (!el) return fallback;
      var n = parseFloat(el.value);
      return isFinite(n) ? n : fallback;
    }
    chase.distance = one("distance", chase.distance);
    chase.height = one("height", chase.height);
    chase.orbit = one("orbit", chase.orbit);
    return { distance: chase.distance, height: chase.height, orbit: chase.orbit };
  }

  function scheduleChasePost() {
    if (chaseTimer) clearTimeout(chaseTimer);
    chaseTimer = setTimeout(function () {
      var body = readChaseFromInputs();
      postChase(body).then(function (j) {
        if (!j) {
          setChaseStatus("chase unavailable");
          return;
        }
        if (j.chase) {
          if (typeof j.chase.distance === "number") {
            chase.distance = j.chase.distance;
            applyChaseInputs("distance", j.chase.distance);
          }
          if (typeof j.chase.height === "number") {
            chase.height = j.chase.height;
            applyChaseInputs("height", j.chase.height);
          }
          if (typeof j.chase.orbit === "number") {
            chase.orbit = j.chase.orbit;
            applyChaseInputs("orbit", j.chase.orbit);
          }
        }
        if (!j.ok) {
          setChaseStatus(j.error === "chase unavailable" ? "chase unavailable" : (j.error || "chase unavailable"));
          return;
        }
        if (j.chase && j.chase.camera_acquired) setChaseStatus("chase on ghost AI");
        else setChaseStatus("chase params saved");
      }).catch(function () {
        setChaseStatus("chase unavailable");
      });
    }, 80);
  }

  function bindChasePanel(root) {
    if (!root) return;
    if (root.dataset.chaseBound === "1") return;
    root.dataset.chaseBound = "1";
    root.addEventListener("input", function (ev) {
      var el = ev.target;
      if (!el || !el.getAttribute) return;
      var name = el.getAttribute("data-chase");
      if (!name) return;
      var n = parseFloat(el.value);
      if (!isFinite(n)) return;
      chase[name] = n;
      applyChaseInputs(name, n);
      scheduleChasePost();
    });
  }

  async function onReplayClick(btn) {
    var root = btn.closest("[data-flight-id]") || btn.closest(".clip-block") || document;
    var status = (root.querySelector && root.querySelector(".replay-status")) || document.getElementById("replay-status");
    var clipId = btn.getAttribute("data-clip-id");
    setStatus(status, "starting…");
    try {
      var body = clipId ? { clip_id: clipId } : null;
      if (!body) {
        var fid = btn.getAttribute("data-flight-id");
        var leg = btn.getAttribute("data-leg");
        var kind = btn.getAttribute("data-kind");
        var clips = await listClips();
        var hit = matchClip(clips, fid, leg, kind);
        if (!hit) {
          setStatus(status, "no clip recorded");
          return;
        }
        body = { clip_id: hit.id };
      }
      var j = await postReplay(body);
      if (!j.ok) {
        var err = j.error || "replay failed";
        if (err === "sim not running") setStatus(status, "sim not running");
        else if (err === "ghost unavailable") setStatus(status, "ghost unavailable");
        else if (err === "clip not found") setStatus(status, "no clip recorded");
        else if (err === "sim is in a menu") setStatus(status, "sim is in a menu - go back to the flight view");
        else setStatus(status, err);
        return;
      }
      // Replays open held at the first frame so the shot can be set up first.
      var msg = j.paused
        ? "held at the first frame - set your shot, then press Resume"
        : "ghost playing - camera stays until you press Stop";
      if (j.aircraft && j.aircraft_requested && j.aircraft !== j.aircraft_requested) {
        msg += " · " + j.aircraft_requested + " not installed, showing " + j.aircraft;
      } else if (j.livery && j.livery_source === "current aircraft") {
        msg += " · no livery in this clip, using " + j.livery + " from the sim";
      } else if (j.livery) {
        msg += " · livery " + j.livery;
      } else {
        msg += " · no livery recorded, and nothing loaded to borrow one from";
      }
      setStatus(status, msg, true, 12000);
      var chaseBody = readChaseFromInputs();
      var cj = await postChase(chaseBody);
      if (!cj || !cj.ok) {
        setChaseStatus((cj && cj.error) || "chase unavailable");
      } else if (cj.chase && cj.chase.camera_acquired) {
        setChaseStatus("chase on ghost AI");
      } else {
        setChaseStatus("chase unavailable");
      }
    } catch (e) {
      setStatus(status, "sim not running");
    }
  }

  async function onStopClick(btn) {
    var root = btn.closest("[data-flight-id]") || btn.closest(".clip-block") || document;
    var status = (root.querySelector && root.querySelector(".replay-status")) || document.getElementById("replay-status");
    try {
      await stopReplay();
      setStatus(status, "stopped", true);
      setChaseStatus("");
    } catch (e) {
      setStatus(status, "sim not running");
    }
  }

  async function hydrate() {
    document.querySelectorAll(".chase-panel").forEach(bindChasePanel);
    var buttons = Array.prototype.slice.call(document.querySelectorAll(".replay-btn[data-flight-id]"));
    if (!buttons.length) return;
    var clips = [];
    var loggerUp = false;
    try {
      clips = await listClips();
      loggerUp = true;
    } catch (e) {
      loggerUp = false;
    }
    buttons.forEach(function (btn) {
      var status = btn.parentElement && btn.parentElement.querySelector(".replay-status");
      var preset = btn.getAttribute("data-clip-id");
      var hit = matchClip(
        clips,
        btn.getAttribute("data-flight-id"),
        btn.getAttribute("data-leg"),
        btn.getAttribute("data-kind")
      );
      if (preset || hit) {
        if (hit) btn.setAttribute("data-clip-id", hit.id);
        btn.disabled = false;
        // Nothing at rest. A leg with a clip has two enabled Replay buttons
        // sitting next to this line, which is the same information said
        // better, and it was on every leg of every flight at once.
        if (status && (!status.textContent || status.textContent === "no clip recorded"
                       || status.textContent === "ready")) {
          setStatus(status, "");
        }
      } else {
        btn.disabled = true;
        setStatus(status, "no clip recorded");
      }
    });
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".replay-btn");
    if (btn) {
      ev.preventDefault();
      if (!btn.disabled) onReplayClick(btn);
      return;
    }
    var stop = ev.target.closest(".replay-stop");
    if (stop) {
      ev.preventDefault();
      onStopClick(stop);
    }
  });

  // The logbook renders its rows from logbook.json after this file loads, so
  // it needs to re-run hydrate once the buttons actually exist.
  window.MSFSReplay = { hydrate: hydrate, stop: stopReplay,
                        syncChase: syncChaseInputs };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", hydrate);
  } else {
    hydrate();
  }
})();
