/* AfterFlight - browse view.
 *
 * logbook.html is a static shell. The watcher is the only writer; this file
 * only reads.
 *
 * The index (logbook.json) carries one small summary per sortie - no tracks,
 * no legs, no prose - so it stays about a kilobyte per flight however much
 * history builds up. Opening a flight fetches its own detail file. Flights are
 * grouped by day and filtered by airframe rather than shown as one long scroll.
 */
(function () {
  "use strict";

  var LOGGER = "";
  var POLL_MS = 5000;
  // While a replay runs the progress bar needs a finer tick than the idle
  // poll. A replay is never happening while you are flying, so this costs
  // nothing in the window that matters.
  var POLL_MS_REPLAY = 1000;
  // A full rebake runs ~20 s. Poll faster while one is in flight so the
  // banner clears promptly rather than up to POLL_MS late.
  var POLL_MS_BUILD = 1000;
  var pollTimer = null;

  // Above this many buttons the airframe strip becomes a select. Counts the
  // "All airframes" chip, so 8 means seven aircraft plus All.
  var AIRFRAME_CHIP_MAX = 8;

  var state = {
    index: null,
    airframe: "all",
    month: null,       // the open month, YYYY-MM; null while searching
    months: {},        // ym -> fetched rows for that month
    loadingMonths: false,
    query: "",
    sort: "desc",      // desc = newest first
    expanded: {},      // sortie_id -> true
    detail: {},        // sortie_id -> fetched detail doc
    buildSeq: null,    // completed-build counter as of our last load()
    lastStateAt: 0,    // when /state last answered, for the banner watchdog
    sawBuild: false    // a rebuild was seen running while this page was open
  };

  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  // ------------------------------------------------------------ formatting

  function fmtDuration(s) {
    if (s === null || s === undefined || !isFinite(s)) return "—";
    s = Math.round(s);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + sec + "s";
    return sec + "s";
  }
  function fmtNm(n) {
    return (n === null || n === undefined || !isFinite(n)) ? "—" : n.toFixed(2) + " nm";
  }
  function fmtTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    return isNaN(d.getTime()) ? iso
      : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  function fmtRate(r) {
    return (r === null || r === undefined || !isFinite(r)) ? "—" : Math.round(r) + " fpm";
  }
  function dayLabel(ymd) {
    var d = new Date(ymd + "T12:00:00");
    if (isNaN(d.getTime())) return ymd;
    return d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  }
  function monthKey(ymd) { return (ymd || "").slice(0, 7); }
  function monthLabel(ym) {
    var d = new Date(ym + "-15T12:00:00");
    return isNaN(d.getTime()) ? ym : d.toLocaleDateString([], { month: "long", year: "numeric" });
  }

  // ------------------------------------------------------------ track map

  // A track is either [[lat,lon],...] or, once a leg has been removed,
  // [[[lat,lon],...],...] - one list per kept piece. Segments are stroked
  // separately so a gap never gets bridged by a line that was not flown.
  function asSegments(track) {
    if (!track || !track.length) return [];
    if (Array.isArray(track[0]) && Array.isArray(track[0][0])) {
      return track.filter(function (seg) { return seg && seg.length >= 2; });
    }
    return track.length >= 2 ? [track] : [];
  }

  function drawTrack(canvas, track) {
    var segs = asSegments(track);
    if (!canvas || !segs.length) return;
    var flat = [];
    segs.forEach(function (seg) { flat = flat.concat(seg); });
    track = flat;
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || 320, h = canvas.clientHeight || 180;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    var lats = track.map(function (p) { return p[0]; });
    var lons = track.map(function (p) { return p[1]; });
    var minLat = Math.min.apply(null, lats), maxLat = Math.max.apply(null, lats);
    var minLon = Math.min.apply(null, lons), maxLon = Math.max.apply(null, lons);
    var coslat = Math.max(Math.cos((minLat + maxLat) / 2 * Math.PI / 180), 1e-6);
    var pad = 12;
    var spanX = Math.max((maxLon - minLon) * coslat, 1e-6);
    var spanY = Math.max(maxLat - minLat, 1e-6);
    var sc = Math.min((w - 2 * pad) / spanX, (h - 2 * pad) / spanY);
    var offX = pad + ((w - 2 * pad) - spanX * sc) / 2;
    var offY = pad + ((h - 2 * pad) - spanY * sc) / 2;
    function proj(p) { return [offX + (p[1] - minLon) * coslat * sc, offY + (maxLat - p[0]) * sc]; }
    function dot(p, c) {
      var xy = proj(p);
      ctx.beginPath(); ctx.arc(xy[0], xy[1], 4, 0, Math.PI * 2); ctx.fillStyle = c; ctx.fill();
    }
    ctx.lineJoin = ctx.lineCap = "round";
    segs.forEach(function (seg) {
      ctx.beginPath();
      seg.forEach(function (p, i) {
        var xy = proj(p);
        if (i === 0) ctx.moveTo(xy[0], xy[1]); else ctx.lineTo(xy[0], xy[1]);
      });
      ctx.strokeStyle = "#224e7a"; ctx.lineWidth = 5; ctx.stroke();
      ctx.strokeStyle = "#5eb3ff"; ctx.lineWidth = 2; ctx.stroke();
      dot(seg[0], "#56d381");
      dot(seg[seg.length - 1], "#ff7a7a");
    });
  }

  function trackCanvas(track, cls) {
    if (!asSegments(track).length) return el("div", "trackmap empty", "no track");
    var c = el("canvas", cls || "trackmap");
    c._track = track;
    requestAnimationFrame(function () { drawTrack(c, track); });
    return c;
  }

  // A map keeps the same path across rebuilds, so a browser that has already
  // decoded it can show the old picture after a leg is removed. Stamp the URL
  // with the build time so a rebuilt map is always a new resource.
  function bust(url) {
    if (!url) return url;
    var v = (state.index && state.index.updated_at) || "";
    return v ? url + "?v=" + encodeURIComponent(v) : url;
  }

  function trackFigure(track, mapUrl, cls) {
    if (!mapUrl) return trackCanvas(track, cls);
    mapUrl = bust(mapUrl);
    var a = el("a", "map-figure");
    a.href = mapUrl; a.target = "_blank"; a.rel = "noopener"; a.title = "Open full size";
    var img = el("img", "trackmap" + (cls && cls.indexOf("wide") >= 0 ? " wide" : ""));
    img.src = mapUrl; img.loading = "lazy"; img.alt = "track map";
    img.onerror = function () {
      var c = trackCanvas(track, cls);
      if (a.parentNode) a.parentNode.replaceChild(c, a);
      if (c.tagName === "CANVAS") drawTrack(c, track);
    };
    a.appendChild(img);
    return a;
  }

  function redrawAll() {
    document.querySelectorAll("canvas.trackmap").forEach(function (c) {
      if (c._track) drawTrack(c, c._track);
    });
  }

  // ------------------------------------------------------------ removal

  // Removal hides; it never deletes a recording. The dialog says so, because
  // "Remove" on its own reads as destructive and the difference matters.
  function confirmRemoval(opts) {
    return new Promise(function (resolve) {
      var modal = $("#confirm");
      $("#confirm-title").textContent = opts.title;
      $("#confirm-body").textContent = opts.body;
      var ul = $("#confirm-points");
      ul.innerHTML = "";
      (opts.points || []).forEach(function (t) { ul.appendChild(el("li", null, t)); });
      var files = $("#confirm-files");
      if (files) files.remove();
      if (opts.files && opts.files.length) {
        var box = el("div", "files");
        box.id = "confirm-files";
        opts.files.forEach(function (f) { box.appendChild(el("div", null, f)); });
        ul.parentNode.insertBefore(box, ul.nextSibling);
      }
      var ack = $("#confirm-ack"), ackBox = $("#confirm-ack-box");
      ack.hidden = !opts.requireAck;
      ackBox.checked = false;
      var okBtn = $("#confirm-ok");
      okBtn.classList.toggle("danger", opts.danger === true);
      okBtn.disabled = !!opts.requireAck;
      function onAck() { okBtn.disabled = !ackBox.checked; }
      ackBox.addEventListener("change", onAck);

      $("#confirm-ok").textContent = opts.confirmLabel || "Remove";
      modal.hidden = false;
      (opts.requireAck ? ackBox : okBtn).focus();

      function done(result) {
        modal.hidden = true;
        ackBox.removeEventListener("change", onAck);
        okBtn.disabled = false;
        $("#confirm-ok").removeEventListener("click", ok);
        $("#confirm-cancel").removeEventListener("click", cancel);
        modal.removeEventListener("click", backdrop);
        document.removeEventListener("keydown", onKey);
        resolve(result);
      }
      function ok() { done(true); }
      function cancel() { done(false); }
      function backdrop(ev) { if (ev.target === modal) done(false); }
      function onKey(ev) { if (ev.key === "Escape") done(false); }

      $("#confirm-ok").addEventListener("click", ok);
      $("#confirm-cancel").addEventListener("click", cancel);
      modal.addEventListener("click", backdrop);
      document.addEventListener("keydown", onKey);
    });
  }

  async function hide(scope, sortieId, key) {
    var r = await postJson("/logbook/hide", { scope: scope, sortie_id: sortieId, key: key });
    if (!r || !r.ok) {
      setLive((r && r.error) || "could not remove", "err");
      return;
    }
    delete state.detail[sortieId];
    await load();
  }

  async function restore(scope, sortieId, key) {
    var r = await postJson("/logbook/restore", { scope: scope, sortie_id: sortieId, key: key });
    if (!r || !r.ok) {
      setLive((r && r.error) || "could not restore", "err");
      return;
    }
    delete state.detail[sortieId];
    await load();
  }

  async function purge(h) {
    // Ask the watcher what this would destroy, and show it verbatim.
    var pre = await postJson("/logbook/purge", {
      scope: h.scope, sortie_id: h.sortie_id, key: h.leg_key, dry_run: true
    });
    if (!pre || !pre.ok) {
      setLive((pre && pre.detail) || (pre && pre.error) || "could not read the plan", "err");
      return;
    }
    var plan = pre.plan || {};
    var kb = Math.round((plan.bytes || 0) / 1024);
    var what = h.scope === "sortie" ? "this whole flight" : "this leg";
    var ok = await confirmRemoval({
      title: h.scope === "sortie" ? "Delete this flight for good?" : "Delete this leg for good?",
      body: "This erases the recording behind " + what + ". It cannot be restored, "
            + "and the logbook cannot rebuild it.",
      points: (plan.notes || []).concat([
        (plan.files || []).length + " file(s) deleted, " + kb + " KB freed"
      ]),
      files: plan.files || [],
      requireAck: true,
      danger: true,
      confirmLabel: "Delete permanently"
    });
    if (!ok) return;
    var r = await postJson("/logbook/purge", {
      scope: h.scope, sortie_id: h.sortie_id, key: h.leg_key, dry_run: false
    });
    if (!r || !r.ok) {
      setLive((r && r.error) || "could not delete", "err");
      // A partial delete still moved events, tracks and caches, and the
      // flight is deliberately still hidden so it can be retried. Reload so
      // the page shows that rather than the world as it was before.
      delete state.detail[h.sortie_id];
      await load();
      return;
    }
    setLive("deleted " + ((r.deleted || []).length) + " file(s), "
            + Math.round((r.bytes || 0) / 1024) + " KB freed", "warn");
    delete state.detail[h.sortie_id];
    await load();
  }

  // Removed lives behind a button rather than above the logbook. It is a
  // place you go on purpose, once in a while, and it was sitting on top of
  // the flights on every visit - including for people who have never removed
  // anything. The button only appears when there is something behind it, so
  // an empty state is no state at all.
  function renderHidden(d) {
    var host = $("#hidden-list");
    var items = d.hidden || [];
    var btn = $("#open-removed");
    btn.hidden = !items.length;
    btn.textContent = "Removed";
    if (items.length) btn.appendChild(el("span", "n", items.length));
    if (!items.length) $("#removed-panel").hidden = true;
    host.innerHTML = "";
    items.forEach(function (h) {
      var row = el("div", "hidden-row");
      row.appendChild(el("span", null, h.scope === "sortie" ? "Flight" : "Leg"));
      var what = (h.aircraft || "") + (h.date ? " · " + String(h.date).slice(0, 16).replace("T", " ") : "");
      if (h.scope === "leg" && h.distance_nm != null) what += " · " + fmtNm(h.distance_nm);
      row.appendChild(el("span", "what", (what || h.sortie_id) + (h.purge_pending ? " · Deletion incomplete" : "")));
      var b = el("button", "spacer-x", "Restore");
      b.type = "button";
      b.disabled = !!h.purge_pending;
      if (h.purge_pending) b.title = "Deletion has started; finish it with Delete permanently.";
      b.addEventListener("click", function () { restore(h.scope, h.sortie_id, h.leg_key); });
      row.appendChild(b);
      var del = el("button", "danger", "Delete permanently");
      del.type = "button";
      del.title = "Erase the recording behind this. There is no undo.";
      del.addEventListener("click", function () { purge(h); });
      row.appendChild(del);
      host.appendChild(row);
    });
  }

  // ------------------------------------------------------------ legs

  // A bounced landing is graded on its firmest contact, so without this the
  // number looks like one clean arrival when it was two or more.
  function bouncePill(landing) {
    var n = (landing && landing.bounces) || 0;
    if (!n) return document.createTextNode("");
    var rates = (landing && landing.contact_rates_fpm) || [];
    var detail = (landing && landing.bounce_detail) || [];
    var pill = el("span", "bounce", n === 1 ? "1 bounce" : n + " bounces");
    var lines = [];
    if (rates.length) lines.push("Arrived at " + rates[0] + " fpm.");
    detail.forEach(function (b, i) {
      var h = (b.height_ft === null || b.height_ft === undefined)
        ? "height unknown" : b.height_ft + " ft";
      var r = (b.rate_fpm === null || b.rate_fpm === undefined)
        ? "?" : b.rate_fpm + " fpm";
      lines.push("Bounce " + (i + 1) + ": up " + h + ", back down at " + r + ".");
    });
    if (landing.contacts_derived) {
      // Say where the numbers came from: these are vertical speed recovered
      // from the clip, not the touchdown normal velocity the grade used.
      lines.push("Recovered from the recording (vertical speed), so these do "
                 + "not match the graded figure.");
    } else {
      lines.push("Graded on the firmest contact.");
    }
    pill.title = lines.join(" ");
    return pill;
  }

  // The letter is the headline; the score says what it was close to. A 79
  // and a 71 are both C and are not the same flight.
  function gradePill(g, name, score) {
    var b = el("span", "grade grade-" + (g || "none").toLowerCase(), g || "—");
    if (score !== null && score !== undefined && isFinite(score)) {
      b.appendChild(el("i", "score", String(Math.round(score))));
    }
    b.title = name || "no landing recorded";
    return b;
  }

  // What the overall pill says when you hover it: the three phases and
  // which one is holding the leg down, since the rollup is capped at one
  // band above the worst.
  function overallTitle(leg) {
    var pg = leg.phase_grade;
    var caveat = "";
    if (!pg || !pg.phases) return ("Overall for the leg") + caveat;
    var lines = ["Overall for the leg"];
    ["liftoff", "climb", "cruise", "descent"].forEach(function (n) {
      var ph = pg.phases[n];
      if (!ph) return;
      lines.push("  " + n + "   "
                 + (ph.score === null || ph.score === undefined
                    ? (ph.note || "not graded")
                    : ph.letter + " " + Math.round(ph.score) + "/100"));
    });
    // Only when the cap actually changed the grade. This used to print on
    // every leg, because a leg always has a weakest phase - so it announced a
    // cap that had not been applied and left the reader working out which
    // number it was talking about.
    var capped = pg.overall, raw = pg.overall_uncapped;
    if (typeof capped === "number" && typeof raw === "number" && raw - capped > 0.05) {
      lines.push("");
      lines.push("Held down to " + Math.round(capped) + " from " + Math.round(raw)
                 + ": a leg's overall grade is never more than one band - ten"
                 + " points - above its weakest phase, and " + (pg.worst_phase || "one phase")
                 + " scored " + Math.round(pg.phases[pg.worst_phase]
                                           ? pg.phases[pg.worst_phase].score : 0) + ".");
    } else if (pg.worst_phase) {
      lines.push("");
      lines.push("Weakest phase: " + pg.worst_phase + ". A leg's overall grade "
                 + "is never more than one band - ten points - above the "
                 + "weakest phase score, which did not bite here.");
    }
    return (lines.join("\n")) + caveat;
  }

  // One phase of a leg: a small label with its own pill.
  function phasePill(label, ph) {
    var box = el("span", "phase");
    box.appendChild(el("span", "phase-label", label));
    if (!ph || ph.score === null || ph.score === undefined) {
      var none = el("span", "grade grade-none", "—");
      none.title = (ph && ph.note) || "not enough of this phase to grade";
      box.appendChild(none);
      return box;
    }
    // The watcher supplies the wording and the measured value with it, so
    // nothing here has to know what "approach_vs_stdev_fpm" meant.
    var lines = [label.charAt(0).toUpperCase() + label.slice(1)
                 + "  " + ph.letter + " " + Math.round(ph.score) + "/100"
                 + (ph.seconds ? "  ·  " + fmtDuration(ph.seconds) : "")];
    if (ph.note) lines.push(ph.note);
    // Each line says what was measured, what it scored, how much it counts,
    // and what good and bad look like. Without that last part a reader is
    // told "6°, 33/100" and has to guess what would have been a good number,
    // which is most of what they wanted to know.
    (ph.parts || []).forEach(function (p) {
      var tail = [];
      if (p.cap) tail.push("a cap, not a weight");
      else if (p.weight_pct != null) tail.push(p.weight_pct + "% of this phase");
      if (p.band) tail.push(p.band);
      lines.push("  " + p.label
                 + (p.measured ? "   " + p.measured : "")
                 + "   " + Math.round(p.score) + "/100"
                 + (tail.length ? "   · " + tail.join(" · ") : ""));
      // Alignment is two measurements behind one number, and naming only the
      // total left "0.31 g slide" with nothing to say what it did. Break it
      // out so each half shows its own band and its share of the score.
      (p.sub || []).forEach(function (q) {
        lines.push("      " + q.label + "   " + q.measured
                   + "   " + Math.round(q.score) + "/100"
                   + "   · " + q.weight_pct + "% of alignment · " + q.band);
      });
      if (p.held) {
        lines.push("      held this phase down to " + Math.round(p.held_to)
                   + ", from " + Math.round(p.held_from));
      }
    });
    box.appendChild(gradePill(ph.letter, lines.join("\n"), ph.score));
    return box;
  }

  // KML and GPX come from the watcher, built on demand from the raw track.
  // The stored polyline in the logbook is decimated lat/lon with no altitude,
  // which is exactly what a 3D view needs and has not got.
  //
  // Google Maps gets a pin, not the path: its URL API carries a marker or
  // road-snapped directions, and there is no way to hand it a polyline. The
  // link says "area" so it does not promise a track it cannot draw.
  function exportLinks(sortieId, leg) {
    var box = el("div", "exports");
    box.appendChild(el("span", "lbl", "Export"));
    var q = "?sortie=" + encodeURIComponent(sortieId)
          + (leg ? "&leg=" + encodeURIComponent(leg.seq) : "");

    var kml = el("a", null, "Google Earth");
    kml.href = LOGGER + "/export" + q + "&fmt=kml";
    kml.title = "KML of the flown path with altitude. Opens in Google Earth "
              + "Pro; in Earth on the web, import it under Projects.";
    box.appendChild(kml);

    var gpx = el("a", null, "GPX");
    gpx.href = LOGGER + "/export" + q + "&fmt=gpx";
    gpx.title = "GPX track, for ForeFlight, SkyDemon, QGIS and the like";
    box.appendChild(gpx);

    return box;
  }

  // A dropped pin on one point. The search form is used rather than
  // map_action=map, which per Google's own documentation "returns a map with
  // no markers or directions" - a map of nothing, which is what this was
  // before. Multiple markers are not supported by the URL API at all, so each
  // end of a route is its own link.
  function mapsPin(lat, lon) {
    return "https://www.google.com/maps/search/?api=1&query="
         + lat.toFixed(6) + "," + lon.toFixed(6);
  }

  // The end of a leg, linked to whatever is actually there. This is the
  // question the logbook cannot answer on its own: a leg that ends at
  // A bare coordinate is a field, a strip or somebody's pasture, and only
  // a real map knows which. Named points link too - the name came from
  // places.json, not from knowing anything about the place.
  function placeLink(text, side) {
    var lat = side && side.lat, lon = side && side.lon;
    if (typeof lat !== "number" || typeof lon !== "number") {
      return document.createTextNode(text);
    }
    var a = el("a", "place", text);
    a.href = mapsPin(lat, lon);
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.title = "Look at " + text + " in Google Maps";
    a.addEventListener("click", function (ev) { ev.stopPropagation(); });
    return a;
  }

  function replayButton(label, kind, side, sortieId) {
    var btn = el("button", "replay-btn", label);
    btn.type = "button";
    if (side && side.clip_id) {
      btn.setAttribute("data-clip-id", side.clip_id);
      btn.setAttribute("data-flight-id", side.flight_id || sortieId);
      btn.setAttribute("data-leg", side.raw_leg === null || side.raw_leg === undefined ? "" : side.raw_leg);
      btn.setAttribute("data-kind", kind);
    } else {
      btn.setAttribute("data-kind", kind);
      btn.disabled = true;
      btn.title = "no clip recorded for this " + kind;
    }
    return btn;
  }

  // Both switches for one flight. The same control appears in the in-flight
  // bar and on the flight's own footer; this is the logbook half.
  async function postPrefs(sortieId, patch) {
    var body = { sortie_id: sortieId };
    if (patch.rating !== undefined) body.rating = patch.rating;
    if (patch.passenger !== undefined) body.passenger = patch.passenger;
    var r = await postJson("/logbook/prefs", body);
    if (!r || !r.ok) {
      setLive((r && r.error) || "could not save that switch", "err");
      return null;
    }
    return r.prefs;
  }

  function prefSwitch(label, title, on, onChange) {
    var wrap = el("label", "sw");
    wrap.title = title;
    var box = el("input");
    box.type = "checkbox";
    box.checked = !!on;
    var track = el("span", "track");
    wrap.appendChild(box);
    wrap.appendChild(track);
    wrap.appendChild(document.createTextNode(label));
    box.addEventListener("change", async function () {
      // Held until the watcher answers, so a failed save cannot leave the
      // switch showing a state the flight is not actually in.
      wrap.classList.add("busy");
      var want = box.checked;
      var got = await onChange(want);
      wrap.classList.remove("busy");
      if (!got) box.checked = !want;
    });
    return wrap;
  }

  function flightSwitches(doc) {
    var p = doc.prefs || { rating: true, passenger: true };
    var box = el("span", "flight-switches");
    box.appendChild(prefSwitch(
      "Rate this flight",
      "Letter grades, scores and phase grades. The touchdown rate stays either way.",
      p.rating !== false,
      async function (on) {
        var got = await postPrefs(doc.sortie_id, { rating: on });
        if (got) { doc.prefs = got; reloadAfterPrefs(doc.sortie_id); }
        return got;
      }));
    box.appendChild(prefSwitch(
      "Passenger notes",
      "The written assessment under each leg.",
      p.passenger !== false,
      async function (on) {
        var got = await postPrefs(doc.sortie_id, { passenger: on });
        if (got) { doc.prefs = got; reloadAfterPrefs(doc.sortie_id); }
        return got;
      }));
    return box;
  }

  // The watcher rebuilt the index by the time it answered, so drop this
  // flight's cached detail and redraw it from what was just written.
  async function reloadAfterPrefs(sortieId) {
    delete state.detail[sortieId];
    await load();
  }

  function renderLeg(sortie, leg) {
    var wrap = el("div", "leg");
    wrap.setAttribute("data-flight-id", sortie.sortie_id);

    var head = el("div", "leg-head");
    head.appendChild(el("span", "leg-no", "Leg " + leg.seq));
    // Ratings off: no pill at all rather than an empty one. A dash reads as
    // "we could not judge this", which is a different thing from "you asked
    // us not to".
    var rated = ((sortie || {}).prefs || {}).rating !== false;
    if (rated) {
      head.appendChild(gradePill(
        leg.grade || leg.landing_grade,
        overallTitle(leg),
        leg.score));
    }
    head.appendChild(bouncePill(leg.landing));
    var pg = rated ? leg.phase_grade : null;
    if (pg && pg.phases) {
      var ps = el("span", "phases");
      ps.appendChild(phasePill("lift-off", pg.phases.liftoff));
      ps.appendChild(phasePill("climb", pg.phases.climb));
      ps.appendChild(phasePill("cruise", pg.phases.cruise));
      ps.appendChild(phasePill("descent", pg.phases.descent));
      head.appendChild(ps);
    }
    var route = el("span", "route");
    var r = leg.route || {};
    route.appendChild(placeLink(r.from || "—", leg.takeoff));
    route.appendChild(document.createTextNode("  →  "));
    route.appendChild(placeLink(r.to || "—", leg.landing));
    if (!r.from_named && !r.to_named) {
      route.classList.add("coords");
      route.title = "Add places.json to name these points";
    }
    head.appendChild(route);
    if (leg.derived) {
      var d = el("span", "tag", "from track");
      d.title = "No event log for this leg; derived from on-ground transitions";
      head.appendChild(d);
    }
    var delLeg = el("button", "btn-quiet", "Remove leg");
    delLeg.type = "button";
    delLeg.addEventListener("click", async function () {
      var ok = await confirmRemoval({
        title: "Remove leg " + leg.seq + " from the logbook?",
        body: (sortie.aircraft || "This aircraft") + " · " +
              fmtDuration(leg.airborne_s) + " · " + fmtNm(leg.distance_nm) +
              (leg.landing_grade ? " · grade " + leg.landing_grade : ""),
        points: [
          "It moves to Removed - the button in the header - where you can "
            + "restore it or delete it for good.",
          "The flight's distance, airborne time and landings are recalculated without it.",
          "Its section leaves the flight's map, showing a gap rather than a false straight line.",
          "Nothing is deleted: the recording and its clips stay on disk."
        ],
        confirmLabel: "Remove from logbook"
      });
      if (ok) hide("leg", sortie.sortie_id, leg.key);
    });
    head.appendChild(delLeg);
    wrap.appendChild(head);

    var body = el("div", "leg-body");
    var mapCell = el("div", "leg-map");
    mapCell.appendChild(trackFigure(leg.track, leg.map));
    body.appendChild(mapCell);

    var stats = el("div", "leg-stats");
    function row(k, v, title) {
      var n = el("div", "stat");
      var kn = el("span", "k", k), vn = el("span", "v", v);
      n.appendChild(kn);
      n.appendChild(vn);
      // .stat is display:contents, so it generates no box and a title on it
      // is never hoverable. The spans are what the grid actually lays out.
      if (title) { kn.title = title; vn.title = title; }
      stats.appendChild(n);
    }
    // Only when a recording behind this leg is not whole. No row at all
    // otherwise: one saying "complete" on every leg would be noise. It goes
    // through row() because .leg-stats is a grid whose children are
    // display:contents, so a plain div dropped in here would not lay out.
    //
    // Both clips, not just the landing. A takeoff recording can be cut short
    // the same way, and reporting only half of them left a truncated takeoff
    // replaying short with nothing saying why.
    ["takeoff", "landing"].forEach(function (which) {
      var cs = (leg[which] || {}).clip_status;
      if (!cs || !(cs.incomplete || cs.unreadable)) return;
      var label = "Recording (" + which + ")";
      if (cs.unreadable) {
        row(label, "unreadable",
            "This recording could not be read at all, so there is no replay "
            + "of it. Nothing measured from the flight track is affected.");
        return;
      }
      // Two different faults, and they are not the same news. A clip can be
      // finished and still have records that will not parse, or be perfectly
      // readable and never have been marked finished. Saying "capture was
      // never marked finished" for both was wrong half the time.
      var damaged = cs.damaged_lines || 0;
      var parts = [];
      if (damaged) {
        parts.push(damaged + " record(s) in this recording could not be read, "
                   + "so the replay has a gap in it.");
      }
      if (!cs.complete) {
        parts.push("Capture was never marked finished, so the recording may "
                   + "stop early.");
      }
      if (which === "landing") {
        parts.push("The touchdown rate was not recovered from it.");
      }
      parts.push("Nothing measured from the flight track is affected.");
      row(label,
          damaged ? (cs.complete ? damaged + " bad record(s)"
                                 : "incomplete, " + damaged + " bad record(s)")
                  : "incomplete",
          parts.join(" "));
    });
    row("Takeoff", leg.takeoff ? fmtTime(leg.takeoff.at) : "—");
    row("Landing", leg.landing ? fmtTime(leg.landing.at) : "—");
    row("Airborne", fmtDuration(leg.airborne_s));
    row("Distance", fmtNm(leg.distance_nm));
    row("Touchdown", fmtRate(leg.landing_rate_fpm) +
        (leg.landing_grade ? "  " + leg.landing_grade : ""),
        "Vertical speed at the wheels. The letter beside it can be held down "
        + "by how square the aircraft arrived - see Alignment.");
    // Absent when the track carries no lateral accelerations, and
    // for helicopters, which are not scored on it. No row at all rather than
    // a dash: a dash would read as "measured, and it was nothing".
    var al = leg.landing_alignment;
    if (al && al.score != null) {
      // Plain words on the row, the arithmetic in the tooltip. "scrub 0.31 g"
      // means nothing on its own; "slid 0.31 g sideways" at least says what
      // happened, and the band underneath says whether that is a lot.
      var bits = [];
      if (al.bank_deg != null) bits.push(al.bank_deg.toFixed(1) + "° bank");
      if (al.scrub_g != null) bits.push(al.scrub_g.toFixed(2) + " g slide");
      var why = "How straight it arrived: " + Math.round(al.score) + " out of 100.\n"
        + "Bank is the furthest it rolled from the moment the wheels touched "
        + "through the rollout — full marks at 4°, nothing at 14°.\n"
        + "Slide is how hard it was still moving sideways once it was down — "
        + "full marks at 0.15 g, nothing at 0.55 g.\n"
        + (al.held_from
           ? "That held this landing down from " + al.held_from + " to "
             + (leg.landing_grade || "?")
             + ". A landing that touches gently and then slides is not a good "
             + "landing, however soft it felt.\n"
           : "Nothing was held down — it arrived straight.\n")
        + "Arriving straight never earns points; arriving crooked costs them.\n"
        + "These thresholds have not been checked against measured flights yet.";
      row("Alignment", bits.join(", ")
          + (al.held_from ? "  — held " + al.held_from + " to "
             + (leg.landing_grade || "?") : ""), why);
    }
    row("Max alt", leg.max_alt_ft != null ? Math.round(leg.max_alt_ft) + " ft" : "—");
    row("Max GS", leg.max_gs_kt != null ? Math.round(leg.max_gs_kt) + " kt" : "—");
    body.appendChild(stats);

    var actions = el("div", "leg-actions");
    actions.appendChild(replayButton("Replay takeoff", "takeoff", leg.takeoff, sortie.sortie_id));
    actions.appendChild(replayButton("Replay landing", "landing", leg.landing, sortie.sortie_id));
    var pause = el("button", "replay-pause", "Pause");
    pause.type = "button"; pause.disabled = true;
    pause.title = "Freeze the clip where it is; the camera knobs keep working";
    actions.appendChild(pause);
    var stop = el("button", "replay-stop", "Stop");
    stop.type = "button"; stop.disabled = true;
    stop.title = "End the replay and give the camera back";
    actions.appendChild(stop);
    actions.appendChild(el("div", "replay-status", ""));
    actions.appendChild(exportLinks(sortie.sortie_id, leg));
    body.appendChild(actions);
    wrap.appendChild(body);

    if (leg.passenger && leg.passenger.text) {
      var pax = el("div", "passenger");
      pax.appendChild(el("div", "pax-label", "Passenger"));
      pax.appendChild(el("p", null, leg.passenger.text));
      wrap.appendChild(pax);
    }
    return wrap;
  }

  // ------------------------------------------------------------ sortie rows

  function sortieRow(s) {
    var row = el("section", "sortie" + (state.expanded[s.sortie_id] ? " open" : ""));
    row.dataset.sortieId = s.sortie_id;

    var head = el("button", "sortie-head");
    head.type = "button";
    head.setAttribute("aria-expanded", state.expanded[s.sortie_id] ? "true" : "false");
    head.appendChild(el("span", "twist", "▸"));

    var when = el("span", "col when");
    when.appendChild(el("span", "t", fmtTime(s.started_at)));
    when.appendChild(el("span", "sub", s.legs + (s.legs === 1 ? " leg" : " legs")));
    head.appendChild(when);

    var who = el("span", "col who");
    who.appendChild(el("span", "t", s.aircraft || "unknown"));
    var rt = el("span", "sub route");
    rt.textContent = (s.route_from || s.route_to)
      ? (s.route_from || "—") + " → " + (s.route_to || "—")
      : "no route recorded";
    who.appendChild(rt);
    head.appendChild(who);

    var num = el("span", "col num");
    num.appendChild(el("span", "t", fmtDuration(s.airborne_s)));
    num.appendChild(el("span", "sub", fmtNm(s.distance_nm)));
    head.appendChild(num);

    var gr = el("span", "col grades");
    if (s.grades && s.grades.length) {
      s.grades.slice(0, 4).forEach(function (g, i) {
        gr.appendChild(gradePill(g, "Leg " + (i + 1) + " overall",
                                 (s.scores || [])[i]));
      });
      if (s.grades.length > 4) gr.appendChild(el("span", "sub", "+" + (s.grades.length - 4)));
    } else {
      gr.appendChild(el("span", "sub",
        (s.prefs && s.prefs.rating === false) ? "not rated" : "no landing"));
    }
    head.appendChild(gr);

    var flags = el("span", "col flags");
    if (s.edited) {
      var e = el("span", "tag", "edited");
      e.title = "A leg has been removed; totals and the map exclude it";
      flags.appendChild(e);
    }
    if (s.status === "open") flags.appendChild(el("span", "tag open", "open"));
    if (s.has_clips) {
      var c = el("span", "tag clips", "clips");
      c.title = "Takeoff or landing clips available to replay";
      flags.appendChild(c);
    }
    head.appendChild(flags);
    row.appendChild(head);

    var body = el("div", "sortie-body");
    if (state.expanded[s.sortie_id]) fillDetail(body, s);
    else body.hidden = true;
    row.appendChild(body);

    head.addEventListener("click", function () { toggleSortie(s, row, head, body); });
    return row;
  }

  function toggleSortie(s, row, head, body) {
    var open = !state.expanded[s.sortie_id];
    if (open) state.expanded[s.sortie_id] = true;
    else delete state.expanded[s.sortie_id];
    row.classList.toggle("open", open);
    head.setAttribute("aria-expanded", open ? "true" : "false");
    body.hidden = !open;
    if (open && !body.dataset.filled) fillDetail(body, s);
  }

  function fillDetail(body, s) {
    body.dataset.filled = "1";
    body.innerHTML = "";
    var cached = state.detail[s.sortie_id];
    if (cached) { renderDetail(body, cached); return; }
    body.appendChild(el("div", "muted pad", "Loading flight…"));
    fetch(LOGGER + "/" + s.detail, { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); })
      .then(function (doc) { state.detail[s.sortie_id] = doc; renderDetail(body, doc); })
      .catch(function (e) {
        // The filled flag is set before the fetch so a second click cannot
        // start a second request. On failure it has to come back off, or the
        // row is stuck on its error until the whole page is reloaded -
        // closing and reopening it just re-showed the same message.
        delete body.dataset.filled;
        body.innerHTML = "";
        var msg = el("div", "muted pad", "Could not load this flight: " + e.message + " ");
        var again = el("button", "btn-quiet", "Try again");
        again.onclick = function () { fillDetail(body, s); };
        msg.appendChild(again);
        body.appendChild(msg);
      });
  }

  function renderDetail(body, doc) {
    body.innerHTML = "";

    var legs = el("div", "legs");
    if (!doc.legs || !doc.legs.length) {
      legs.appendChild(el("div", "muted pad", "No takeoff or landing recorded on this flight."));
    } else {
      doc.legs.forEach(function (l) { legs.appendChild(renderLeg(doc, l)); });
    }

    var foot = el("div", "sortie-id");
    foot.appendChild(el("span", null, doc.sortie_id));
    if (doc.fragments > 1) {
      var f = el("span", "tag", doc.fragments + " fragments");
      f.title = "The watcher split this outing across " + doc.fragments +
                " flight records (reconnects or position jumps); they are one flight.";
      foot.appendChild(f);
    }
    if (doc.end_reason) foot.appendChild(el("span", "tag", doc.end_reason));
    foot.appendChild(exportLinks(doc.sortie_id, null));
    foot.appendChild(flightSwitches(doc));
    var delFlight = el("button", "btn-quiet", "Remove flight");
    delFlight.type = "button";
    delFlight.style.marginLeft = "auto";
    delFlight.addEventListener("click", async function () {
      var ok = await confirmRemoval({
        title: "Remove this flight from the logbook?",
        body: (doc.aircraft || "This aircraft") + " · " +
              (doc.legs ? doc.legs.length : 0) + " legs · " +
              fmtDuration(doc.airborne_s) + " · " + fmtNm(doc.distance_nm),
        points: [
          "It moves to Removed - the button in the header - where you can "
            + "restore it or delete it for good.",
          "Every leg goes with it, including " + (doc.landings || 0) + " landing(s).",
          "It leaves the totals, its day, and the airframe summary.",
          "Nothing is deleted: the recording and its clips stay on disk."
        ],
        confirmLabel: "Remove from logbook"
      });
      if (ok) hide("sortie", doc.sortie_id);
    });
    foot.appendChild(delFlight);
    // First in the panel: these act on the whole flight, so they belong above
    // the thing they act on rather than after every leg of it.
    body.appendChild(foot);

    var overview = el("div", "sortie-map");
    overview.appendChild(trackFigure(doc.track, doc.map, "trackmap wide"));
    body.appendChild(overview);
    body.appendChild(legs);

    if (window.MSFSReplay && window.MSFSReplay.hydrate) window.MSFSReplay.hydrate();
  }

  // ------------------------------------------------------- months on demand

  // The index carries one line per month and a compact row per flight for
  // searching; the flights themselves live in a file per month, fetched the
  // first time that month is opened and kept for the rest of the session.
  function monthMeta(ym) {
    return ((state.index || {}).months || []).filter(function (m) {
      return m.ym === ym;
    })[0] || null;
  }

  async function fetchMonth(ym) {
    if (state.months[ym]) return state.months[ym];
    var meta = monthMeta(ym);
    if (!meta) return [];
    try {
      var r = await fetch(bust(LOGGER + "/" + meta.file), { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      var doc = await r.json();
      state.months[ym] = doc.sorties || [];
    } catch (e) {
      setLive("could not load " + monthLabel(ym), "err");
      state.months[ym] = [];
    }
    return state.months[ym];
  }

  // Which months a filter or a search could possibly match, worked out from
  // the compact rows so only those months are fetched.
  function monthsForFilter() {
    var q = state.query.trim().toLowerCase();
    var want = {};
    ((state.index || {}).find || []).forEach(function (f) {
      if (state.airframe !== "all" && (f.a || "unknown") !== state.airframe) return;
      if (q) {
        var hay = [f.a, f.f, f.t, f.d, f.i].filter(Boolean).join(" ").toLowerCase();
        if (hay.indexOf(q) < 0) return;
      }
      if (f.m) want[f.m] = true;
    });
    return Object.keys(want).sort().reverse();
  }

  function filtering() {
    return state.airframe !== "all" || !!state.query.trim();
  }

  // A text search genuinely spans the whole logbook - you do not know which
  // month the thing you typed is in. An airframe filter does not: it narrows
  // what you are looking at, and paging by month is still how you move.
  // Treating the two the same disabled both month arrows and replaced the
  // month with "All months" the moment an airframe was picked, which reads as
  // the month selector having broken.
  function searching() {
    return !!state.query.trim();
  }

  // Months holding the selected airframe, newest first. Query ignored on
  // purpose: this answers "where can I step to", not "what matches".
  function monthsForAirframe() {
    var want = {};
    ((state.index || {}).find || []).forEach(function (f) {
      if (state.airframe !== "all" && (f.a || "unknown") !== state.airframe) return;
      if (f.m) want[f.m] = true;
    });
    return Object.keys(want).sort().reverse();
  }

  // Filtering to an airframe that never flew in the month you are on would
  // show an empty page and look like the filter had failed. Move to the
  // newest month that does have it.
  function syncMonthToFilter() {
    if (searching()) return;
    var list = navigableMonths();
    if (!list.length) return;
    if (list.indexOf(state.month) < 0) state.month = list[0];
  }

  // Everything the list needs, fetched and then rendered. Called instead of
  // renderList() wherever a change could need a month that is not loaded yet.
  async function showList() {
    var need = searching() ? monthsForFilter()
             : (state.month ? [state.month] : []);
    var missing = need.filter(function (ym) { return !state.months[ym]; });
    if (missing.length) {
      state.loadingMonths = true;
      renderList();
      // Sequential on purpose: a search across three years should not open
      // thirty-six requests at once against a single-threaded local server.
      for (var i = 0; i < missing.length; i++) await fetchMonth(missing[i]);
      state.loadingMonths = false;
    }
    renderList();
  }

  // ------------------------------------------------------------ filtering

  function visibleSorties() {
    // Only the months actually needed are loaded, so the list is built from
    // those rather than from one array holding the whole logbook.
    var source = searching() ? monthsForFilter()
               : (state.month ? [state.month] : []);
    var out = [];
    source.forEach(function (ym) {
      (state.months[ym] || []).forEach(function (s) { out.push(s); });
    });
    if (state.airframe !== "all") {
      out = out.filter(function (s) { return (s.aircraft || "unknown") === state.airframe; });
    }
    var q = state.query.trim().toLowerCase();
    if (q) {
      out = out.filter(function (s) {
        return [s.aircraft, s.route_from, s.route_to, s.date, s.sortie_id]
          .filter(Boolean).join(" ").toLowerCase().indexOf(q) >= 0;
      });
    }
    // The UI owns the order rather than inheriting whatever the builder wrote,
    // so a stale index cannot flip the logbook around underneath you.
    // Parse rather than compare the ISO strings as text: started_at carries a
    // local UTC offset, so across a DST change two flights an hour apart sort
    // by their literal digits instead of by when they happened.
    function whenOf(s) {
      var t = Date.parse(s.started_at || "");
      if (!isNaN(t)) return t;
      t = Date.parse(s.date || "");
      if (!isNaN(t)) return t;
      return 0;
    }
    out.sort(function (a, b) {
      var d = whenOf(a) - whenOf(b);
      if (d === 0) {
        var ia = a.sortie_id || "", ib = b.sortie_id || "";
        d = ia === ib ? 0 : (ia < ib ? -1 : 1);
      }
      return state.sort === "asc" ? d : -d;
    });
    return out;
  }

  function groupByDay(list) {
    var days = [], byDay = {};
    list.forEach(function (s) {
      var k = s.date || "unknown";
      if (!byDay[k]) { byDay[k] = { date: k, items: [] }; days.push(byDay[k]); }
      byDay[k].items.push(s);
    });
    days.forEach(function (d) {
      d.airborne = d.items.reduce(function (a, s) { return a + (s.airborne_s || 0); }, 0);
      d.distance = d.items.reduce(function (a, s) { return a + (s.distance_nm || 0); }, 0);
      d.landings = d.items.reduce(function (a, s) { return a + (s.landings || 0); }, 0);
      d.legs = d.items.reduce(function (a, s) { return a + (s.legs || 0); }, 0);
      d.bounced = d.items.reduce(function (a, s) { return a + (s.bounced || 0); }, 0);
      // Which aircraft, in the order first flown that day.
      d.airframes = [];
      d.items.slice().reverse().forEach(function (s) {
        var a = s.aircraft || "unknown";
        if (d.airframes.indexOf(a) < 0) d.airframes.push(a);
      });
    });
    return days;
  }

  function renderList() {
    var host = $("#sorties");
    host.innerHTML = "";
    var list = visibleSorties();
    var filtered = filtering();
    $("#result-count").textContent =
      list.length + (list.length === 1 ? " flight" : " flights") + (filtered ? " matching" : "");

    if (!list.length) {
      host.appendChild(el("div", "muted pad", state.loadingMonths
        ? "Loading…"
        : (filtering() ? "No flights match these filters."
                       : "No flights in " + (state.month ? monthLabel(state.month)
                                                         : "this month") + ".")));
      return;
    }
    groupByDay(list).forEach(function (day) {
      var grp = el("section", "daygroup");
      var h = el("header", "dayhead");
      h.appendChild(el("h2", null, dayLabel(day.date)));
      // The same value-over-label shape the whole-logbook summary uses, at
      // day scale. The old strip ran four bare numbers together - "1 flight
      // 33m 31s 43.29 nm 2 ldg" - which needs reading twice to work out which
      // number is which.
      var tot = el("div", "daytotals");
      function stat(value, label, title) {
        var box = el("span", "dstat");
        box.appendChild(el("span", "v", value));
        box.appendChild(el("span", "k", label));
        if (title) box.title = title;
        tot.appendChild(box);
      }
      stat(day.items.length, day.items.length === 1 ? "flight" : "flights");
      stat(day.legs || day.items.length, day.legs === 1 ? "leg" : "legs");
      stat(fmtDuration(day.airborne), "airborne");
      stat(fmtNm(day.distance), "distance");
      stat(day.landings, day.landings === 1 ? "landing" : "landings",
           day.bounced ? day.bounced + " of them bounced" : "");
      h.appendChild(tot);
      if (day.airframes.length) {
        var who = el("div", "dayframes");
        who.textContent = day.airframes.join(" \u00b7 ");
        h.appendChild(who);
      }
      grp.appendChild(h);
      day.items.forEach(function (s) { grp.appendChild(sortieRow(s)); });
      host.appendChild(grp);
    });
  }

  function renderFilters() {
    var d = state.index;
    var acHost = $("#airframe-filter");
    acHost.innerHTML = "";
    var frames = [{ id: "all", label: "All airframes", n: (d.find || []).length }]
      .concat((d.airframes || []).map(function (a) {
        return { id: a.aircraft, label: a.aircraft, n: a.sorties };
      }));
    // Chips read well while they fit on the row and badly once they do not:
    // at twenty airframes the strip is a thin sideways-scrolling gutter where
    // the one you want is almost always out of sight. Past the threshold the
    // same choice becomes a select, which stays one slot for ever.
    if (frames.length > AIRFRAME_CHIP_MAX) {
      var sel = el("select");
      sel.id = "airframe-select";
      sel.title = "Airframe";
      frames.forEach(function (f) {
        var o = el("option", null, f.label + "  (" + f.n + ")");
        o.value = f.id;
        sel.appendChild(o);
      });
      sel.value = state.airframe;
      sel.addEventListener("change", function (ev) {
        state.airframe = ev.target.value;
        syncMonthToFilter();
        renderFilters();
        showList();
      });
      acHost.classList.add("as-select");
      acHost.appendChild(sel);
      renderMonthNav();
      return;
    }
    acHost.classList.remove("as-select");
    frames.forEach(function (f) {
      var b = el("button", "chip" + (state.airframe === f.id ? " on" : ""));
      b.type = "button";
      b.appendChild(el("span", null, f.label));
      b.appendChild(el("span", "n", f.n));
      b.addEventListener("click", function () {
        state.airframe = f.id; syncMonthToFilter();
        renderFilters(); showList();
      });
      acHost.appendChild(b);
      // Keep the chosen airframe in view; it may be off-screen in a long strip.
      if (state.airframe === f.id) {
        setTimeout(function () {
          try { b.scrollIntoView({ block: "nearest", inline: "nearest" }); } catch (e) {}
        }, 0);
      }
    });

    renderMonthNav();
  }

  // ---- month navigation ---------------------------------------------------

  var MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // Months that actually hold flights, newest first. The picker shows every
  // month of every year the logbook spans; this is what you can step to.
  function navigableMonths() {
    // Under an airframe filter, stepping through months that never flew it
    // would page through empty screens.
    var ok = monthsForAirframe();
    return ((state.index || {}).months || [])
      .filter(function (m) { return m.sorties && ok.indexOf(m.ym) >= 0; })
      .map(function (m) { return m.ym; })
      .sort().reverse();
  }

  function stepMonth(delta) {
    var list = navigableMonths();
    var at = list.indexOf(state.month);
    var next = list[at + delta];
    if (at < 0 || !next) return;
    state.month = next;
    closeMonthPop();
    renderFilters();
    showList();
  }

  function renderMonthNav() {
    var meta = monthMeta(state.month);
    var label = $("#month-open");
    label.innerHTML = "";
    if (searching()) {
      // Search spans every month, so there is no single month to show.
      var hits = monthsForFilter().length;
      label.classList.add("searching-now");
      label.appendChild(document.createTextNode("All months"));
      label.appendChild(el("span", "n", hits));
      label.title = hits + (hits === 1 ? " month has matches" : " months have matches");
    } else {
      label.classList.remove("searching-now");
      label.appendChild(document.createTextNode(
        state.month ? monthLabel(state.month) : "No flights"));
      if (meta) label.appendChild(el("span", "n", meta.sorties));
      label.title = meta
        ? (fmtDuration(meta.airborne_s) + " · " + fmtNm(meta.distance_nm)
           + " · " + meta.landings + " ldg")
        : "";
    }
    var list = navigableMonths();
    var at = list.indexOf(state.month);
    // Newest first, so the left arrow goes back in time - which is further
    // down the list, not up it.
    $("#month-next").disabled = searching() || at <= 0;
    $("#month-prev").disabled = searching() || at < 0 || at >= list.length - 1;
    if (!$("#month-pop").hidden) renderMonthPop();
  }

  function renderMonthPop() {
    var pop = $("#month-pop");
    pop.innerHTML = "";
    var months = ((state.index || {}).months || []);
    var have = {};
    months.forEach(function (m) { have[m.ym] = m; });
    // Under an airframe filter the picker has to count that airframe, not
    // every flight: offering "September, 5 flights" and then showing one is
    // the same broken feeling as the arrows going dead.
    var only = state.airframe !== "all" ? {} : null;
    if (only) {
      ((state.index || {}).find || []).forEach(function (f) {
        if ((f.a || "unknown") !== state.airframe || !f.m) return;
        only[f.m] = (only[f.m] || 0) + 1;
      });
    }
    var years = [];
    months.forEach(function (m) {
      var y = m.ym.slice(0, 4);
      if (years.indexOf(y) < 0) years.push(y);
    });
    years.sort().reverse();
    years.forEach(function (y) {
      var box = el("div", "mp-year");
      box.appendChild(el("h4", null, y));
      var grid = el("div", "mp-grid");
      for (var mo = 1; mo <= 12; mo++) {
        var ym = y + "-" + String(mo < 10 ? "0" + mo : mo);
        var m = have[ym];
        var n = only ? (only[ym] || 0) : (m ? m.sorties : 0);
        var cell = el("button", "mp-cell" + (state.month === ym ? " on" : ""));
        cell.type = "button";
        cell.appendChild(el("span", null, MONTH_SHORT[mo - 1]));
        cell.appendChild(el("span", "n", n || "—"));
        if (!m || !n) {
          cell.disabled = true;
          cell.title = only
            ? "No " + state.airframe + " in " + MONTH_SHORT[mo - 1] + " " + y
            : "No flights in " + MONTH_SHORT[mo - 1] + " " + y;
        } else {
          cell.title = n + (n === 1 ? " flight" : " flights")
                       + (only ? " in the " + state.airframe
                               : " · " + fmtDuration(m.airborne_s));
          (function (pick) {
            cell.addEventListener("click", function () {
              state.month = pick;
              // Choosing a month is choosing to browse, not to search.
              if (state.query.trim()) { state.query = ""; $("#search").value = ""; }
              closeMonthPop();
              renderFilters();
              showList();
            });
          })(ym);
        }
        grid.appendChild(cell);
      }
      box.appendChild(grid);
      pop.appendChild(box);
    });
  }

  function closeMonthPop() {
    $("#month-pop").hidden = true;
    $("#month-open").setAttribute("aria-expanded", "false");
  }

  function toggleMonthPop() {
    var pop = $("#month-pop");
    var open = pop.hidden;
    pop.hidden = !open;
    $("#month-open").setAttribute("aria-expanded", open ? "true" : "false");
    if (open) renderMonthPop();
  }


  function renderSummary(d) {
    var t = d.totals || {};
    $("#t-sorties").textContent = t.sorties || 0;
    $("#t-days").textContent = t.days || 0;
    $("#t-landings").textContent = t.landings || 0;
    $("#t-distance").textContent = fmtNm(t.distance_nm);
    $("#t-airborne").textContent = fmtDuration(t.airborne_s);
    $("#updated").textContent = d.updated_at ? new Date(d.updated_at).toLocaleString() : "—";
    // "no maps in this build" is not the same as "Pillow is missing"; an
    // index-only rebuild mid-flight bakes nothing and used to report the wrong
    // reason entirely.
    var note = $("#maps-note");
    if (d.maps_baked) {
      note.hidden = true;
    } else if (d.pillow === false) {
      note.hidden = false;
      note.textContent = "Pillow not installed — maps are drawn in the page only.";
    } else if (d.maps_pending) {
      note.hidden = false;
      note.textContent = "Track maps are baked once the aircraft is parked.";
    } else {
      note.hidden = true;
    }
  }

  // ------------------------------------------------------------ live state

  // Why a rebuild is running, in the user's words rather than the code's.
  var BUILD_REASONS = {
    requested: "you asked for it",
    settings: "settings changed",
    landing: "a landing was recorded",
    "flight end": "a flight ended"
  };

  // seq counts completed builds. Recording it means a build that finishes
  // while the page is open is noticed even if every 'building' sample fell
  // between polls - the page reloads on the counter, not on catching the act.
  // The banner asserts that a build is running at this moment. That is only
  // true if the watcher said so recently, so it is withdrawn whenever the
  // last confirmation goes stale - whatever the reason. Polling stops while
  // the tab is in the background, a poll can fail, and a page left open
  // across an upgrade keeps running whatever it loaded at the time. None of
  // those should be able to leave a spinner running for ever.
  var BANNER_STALE_MS = 10000;

  // The bar is shown only while something is being recorded. Its switches
  // write to the same store the logbook footer does, keyed by sortie, so a
  // choice made in the air is already the one the builder reads on landing.
  var liveP = { id: null, busy: false };

  function syncInFlight(j) {
    var bar = $("#inflight");
    var p = j && j.flight_prefs;
    var cur = (j && j.current) || {};
    var flying = !!(p && p.sortie_id && cur.state === "in_flight");
    bar.hidden = !flying;
    if (!flying) { liveP.id = null; return; }
    liveP.id = p.sortie_id;
    $("#inflight-what").textContent =
      "Recording " + (cur.aircraft || "this flight");
    // Do not fight the user mid-click: skip while a write is in the air.
    if (liveP.busy) return;
    $("#live-rating").checked = p.rating !== false;
    $("#live-passenger").checked = p.passenger !== false;
  }

  function wireLiveSwitches() {
    [["#live-rating", "rating"], ["#live-passenger", "passenger"]]
      .forEach(function (pair) {
        var box = $(pair[0]);
        box.addEventListener("change", async function () {
          if (!liveP.id) return;
          var want = box.checked;
          var patch = {};
          patch[pair[1]] = want;
          liveP.busy = true;
          box.disabled = true;
          var got = await postPrefs(liveP.id, patch);
          box.disabled = false;
          liveP.busy = false;
          if (!got) { box.checked = !want; return; }
          // The flight may already be in the logbook - a second leg of an
          // outing that has landed once - so redraw rather than assume not.
          reloadAfterPrefs(liveP.id);
        });
      });
  }

  function bannerWatchdog() {
    var bar = $("#rebuilding");
    if (!bar || bar.hidden) return;
    if (Date.now() - state.lastStateAt > BANNER_STALE_MS) bar.hidden = true;
  }

  function syncRebuild(lb) {
    var bar = $("#rebuilding");
    // No block at all means no build we can see. Returning early here left
    // the banner up permanently against an older or partial /state.
    if (!lb) { bar.hidden = true; return false; }
    if (lb.building) {
      var why = BUILD_REASONS[lb.reason] || lb.reason;
      var what = lb.maps ? "Refreshing the logbook and maps…"
                         : "Refreshing the logbook…";
      if (lb.elapsed_s >= 3) what += " (" + Math.round(lb.elapsed_s) + "s)";
      var t = $("#rebuilding-text");
      t.textContent = what;
      if (why) t.appendChild(el("span", "why", " — " + why));
      bar.hidden = false;
      state.sawBuild = true;
      return true;
    }
    bar.hidden = true;
    // First sighting: adopt the counter without reloading, since load()
    // has just run anyway.
    var first = state.buildSeq === null;
    var advanced = !first && lb.seq !== state.buildSeq;
    state.buildSeq = lb.seq;
    // sawBuild covers the ordinary case a seq comparison misses: the page
    // was open before the build started, so there was no earlier counter
    // to compare against, and the reload would never fire.
    if (advanced || state.sawBuild) {
      state.sawBuild = false;
      load();
    }
    return false;
  }

  function setLive(text, cls) {
    var n = $("#live");
    n.textContent = text;
    n.className = "live " + (cls || "");
  }

  // The watcher keeps the lock between replays, so the box has to mirror it
  // whenever we hear from it - not only while a clip is playing, or a watcher
  // left following would behave locked with the box clear.
  function syncLock(chase) {
    var lock = $("#chase-lock");
    if (!lock || !chase || document.activeElement === lock) return;
    lock.checked = !!chase.locked;
    $("#bearing-row").hidden = !chase.locked;
    // Say which mode is active rather than leaving the unticked box to imply it.
    var hint = $("#chase-modehint");
    if (hint) hint.textContent = chase.locked ? "following" : "planted";
    if (chase.lock_bearing) $("#chase-bearing").value = chase.lock_bearing;
  }

  function clock(sec) {
    var s = Math.max(0, Math.round(sec || 0));
    return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
  }

  function syncProgress(rp) {
    var box = document.getElementById("replay-progress");
    if (!box) return;
    var dur = rp && rp.duration_s;
    if (!rp || !rp.active || !dur) { box.hidden = true; return; }
    var el_s = rp.elapsed_s || 0;
    box.hidden = false;
    box.querySelector(".rp-fill").style.width =
      Math.max(0, Math.min(100, (el_s / dur) * 100)) + "%";
    box.querySelector(".rp-time").textContent = clock(el_s) + "/" + clock(dur);
  }

  function syncPauseButtons(paused, running) {
    document.querySelectorAll(".replay-pause").forEach(function (b) {
      b.textContent = paused ? "Resume" : "Pause";
      b.disabled = !running;
    });
  }

  // Stop is not the opposite of Pause, and it outlives playback. When a clip
  // reaches its end the watcher sets active false but holding true, and a
  // loop goes on re-asserting the camera at the event - the shot is meant to
  // stay there so it can be looked at. Stop is the only thing that hands the
  // camera back, so tying it to "a clip is playing" would strand a held
  // camera with no way to release it.
  function syncStopButtons(rp) {
    rp = rp || {};
    var live = !!(rp.active || rp.holding
                  || (rp.chase && rp.chase.camera_acquired));
    document.querySelectorAll(".replay-stop").forEach(function (b) {
      b.disabled = !live;
      b.title = live ? "End the replay and give the camera back"
                     : "Nothing is playing and the camera is not held";
    });
  }

  function setChaseStatus(text) {
    document.querySelectorAll(".chase-status").forEach(function (n) { n.textContent = text || ""; });
  }

  async function postJson(path, body) {
    var r = await fetch(LOGGER + path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
    return r.json();
  }

  function measureStickyStack() {
    var f = document.querySelector(".filters");
    if (!f) return;
    document.documentElement.style.setProperty(
      "--filters-h", Math.round(f.getBoundingClientRect().height) + "px");
  }

  function schedulePoll(ms) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(pollState, ms);
  }

  async function pollState() {
    if (document.visibilityState !== "visible") { schedulePoll(POLL_MS); return; }
    var nextPoll = POLL_MS;
    try {
      var r = await fetch(LOGGER + "/state", { cache: "no-store" });
      var j = await r.json();
      state.lastStateAt = Date.now();
      var cur = j.current || {};
      var rp = j.replay || {};
      var vEl = $("#version");
      if (vEl && j.version) {
        // A watcher goes on running the code it started with. Saying so where
        // the version already is beats finding out from behavior that stopped
        // matching the source - which is how a stale watcher once rewrote the
        // logbook in an old format with no hint why.
        var vtxt = j.version + (j.code_stale ? "  (restart: code changed since this started)" : "");
        if (vEl.textContent !== vtxt) vEl.textContent = vtxt;
        vEl.classList.toggle("stale", !!j.code_stale);
      }
      // Deliberately no placeholder hint here: gray placeholder text in an
      // empty box reads as a value that has been filled in, and it has not -
      // nothing is sent unless it is actually typed. The watcher falls back to
      // the aircraft loaded now, so the box only matters for the odd case.
      syncLock(rp.chase);
      if (window.MSFSReplay && window.MSFSReplay.syncChase) {
        window.MSFSReplay.syncChase(rp.chase);
      }
      syncProgress(rp);
      // One call, from the poll, rather than beside every syncPauseButtons:
      // Stop follows the watcher's state and not the branch the UI happens to
      // be in, so a held camera keeps its Stop no matter which branch drew it.
      syncStopButtons(rp);
      syncInFlight(j);
      nextPoll = syncRebuild(j.logbook) ? POLL_MS_BUILD
               : (rp.active ? POLL_MS_REPLAY : POLL_MS);
      if (!j.connected) {
        syncPauseButtons(false, false);
        setLive("watcher up, sim not connected", "warn");
      } else if (rp.active) {
        syncPauseButtons(!!rp.paused, true);
        setLive((rp.paused ? "paused at " : "replaying ") + (rp.clip_id || "clip") +
                (rp.chase && rp.chase.camera_acquired ? " — camera at the event" : ""),
                rp.paused ? "warn" : "ok");
      } else if (rp.holding) {
        syncPauseButtons(false, false);
        setLive("clip ended — camera still at the event, press Stop to come back", "warn");
      } else if (cur.state === "in_flight") {
        syncPauseButtons(false, false);
        var hz = (j.sampler || {}).sample_hz;
        setLive("recording " + (cur.aircraft || "") + (hz ? " · " + hz + " Hz" : ""), "ok");
      } else {
        syncPauseButtons(false, false);
        setLive("connected, idle", "ok");
      }
    } catch (e) {
      setLive("watcher not running on 127.0.0.1:8742", "err");
      // We cannot see a build from here, so stop claiming one is running.
      $("#rebuilding").hidden = true;
    } finally {
      // Always: no failure above can leave the page frozen mid-poll.
      schedulePoll(nextPoll);
    }
  }

  // ------------------------------------------------------------ settings

  var settingsSpec = [];

  function settingRow(item) {
    var row = el("div", "srow");
    row.setAttribute("data-key", item.key);
    var left = el("div");
    left.appendChild(el("span", "slabel", item.label));
    if (item.note) left.appendChild(el("span", "snote", item.note));
    var def = item.type === "bool" ? (item.default ? "on" : "off") : item.default;
    left.appendChild(el("span", "sdefault", "default " + def));
    row.appendChild(left);

    var input;
    if (item.type === "bool") {
      input = el("input"); input.type = "checkbox"; input.checked = !!item.value;
    } else if (item.type === "choice") {
      input = el("select");
      (item.choices || []).forEach(function (c) {
        var o = el("option", null, c); o.value = c; input.appendChild(o);
      });
      input.value = item.value;
    } else {
      input = el("input"); input.type = "number"; input.value = item.value;
      if (item.min !== undefined && item.min !== null) input.min = item.min;
      if (item.max !== undefined && item.max !== null) input.max = item.max;
      input.step = "any";
    }
    input.className = "sinput";
    // Mark what differs from default so a changed logbook is never a mystery.
    function mark() {
      var v = item.type === "bool" ? input.checked
            : (item.type === "choice" ? input.value : parseFloat(input.value));
      var d = item.default;
      row.classList.toggle("changed", item.type === "float" ? (v !== +d) : (v !== d));
    }
    input.addEventListener("input", mark);
    input.addEventListener("change", mark);
    mark();
    row.appendChild(input);
    return row;
  }

  function renderSettings(doc) {
    settingsSpec = doc.settings || [];
    var host = $("#settings-body");
    host.innerHTML = "";
    var groups = [], byGroup = {};
    settingsSpec.forEach(function (it) {
      var g = it.group || "Other";
      if (!byGroup[g]) { byGroup[g] = { name: g, items: [] }; groups.push(byGroup[g]); }
      byGroup[g].items.push(it);
    });
    groups.forEach(function (g) {
      var box = el("div", "sgroup");
      box.appendChild(el("h3", null, g.name));
      g.items.forEach(function (it) { box.appendChild(settingRow(it)); });
      host.appendChild(box);
    });
    var d = doc.derived || {};
    $("#settings-derived").textContent = d.buffer_sec
      ? "Recording buffer sized to " + d.buffer_sec + " s to cover the clip windows."
      : "";
    $("#settings-error").textContent = "";
  }

  function collectSettings() {
    var out = {};
    settingsSpec.forEach(function (it) {
      var row = document.querySelector('.srow[data-key="' + it.key + '"]');
      if (!row) return;
      var input = row.querySelector(".sinput");
      if (it.type === "bool") out[it.key] = input.checked;
      else if (it.type === "choice") out[it.key] = input.value;
      else {
        var v = parseFloat(input.value);
        if (!isNaN(v)) out[it.key] = v;
      }
    });
    return out;
  }

  // Saving settings should show its effect at once. Waiting for the next poll
  // means up to POLL_MS of stale numbers in the camera boxes, and none at all
  // while the tab is in the background.
  async function refreshFromState() {
    try {
      var r = await fetch(LOGGER + "/state", { cache: "no-store" });
      var j = await r.json();
      var rp = j.replay || {};
      syncLock(rp.chase);
      if (window.MSFSReplay && window.MSFSReplay.syncChase) {
        window.MSFSReplay.syncChase(rp.chase);
      }
      syncProgress(rp);
    } catch (e) { /* the next poll will report it */ }
  }

  // Rendered entirely from /grading, which the watcher builds from the
  // grading profile. Nothing about thresholds or wording is written here,
  // so the explanation cannot drift from the code that does the grading.
  // Rendered entirely from /grading, which the watcher builds from the
  // grading profiles. No thresholds or wording live here, so the explanation
  // cannot drift from the code that does the grading.
  function renderGradingProfile(d, prof, box) {
    box.innerHTML = "";
    (prof.notes || []).forEach(function (n) {
      box.appendChild(el("p", "gx-warn", n));
    });

    var scale = el("div", "gx-scale");
    (d.letters || []).forEach(function (x) {
      scale.appendChild(gradePill(x.letter, null, x.at_least));
      scale.appendChild(el("span", "gx-band",
        x.at_least === null ? "below 60" : x.at_least + " and up"));
    });
    box.appendChild(scale);

    (prof.phases || []).forEach(function (ph) {
      var sec = el("div", "gx-phase");
      var head = el("div", "gx-head");
      head.appendChild(el("h3", null, ph.label));
      head.appendChild(el("span", "gx-weight",
                          ph.weight_pct + "% of the leg grade"));
      sec.appendChild(head);
      sec.appendChild(el("p", "gx-blurb", ph.blurb));
      // Cited once per phase, not per metric: the phase is the smallest unit
      // where the answer is the same for everything in it, and a citation on
      // every row would be noise rather than provenance.
      if (ph.source) sec.appendChild(el("p", "gx-source", ph.source));
      (ph.metrics || []).forEach(function (m) {
        var row = el("div", "gx-metric");
        row.appendChild(el("b", null, m.label + "  " + m.weight_pct + "%"));
        var right = el("span", "gx-why", m.why || "");
        if (m.best !== null && m.best !== undefined) {
          right.appendChild(el("div", "gx-band",
            "full marks at " + m.best + ", zero at " + m.worst
            + (m.unit && m.unit.indexOf("%") < 0 ? " " + m.unit.replace("%.0f ", "")
                                                          .replace("%.2f ", "") : "")));
        }
        // The touchdown curve, spelled out. It carries 45% of the descent and
        // is the number anyone actually looks at, so the steps are worth the
        // room rather than hiding behind "full marks at 0".
        if (m.steps) {
          right.appendChild(el("div", "gx-band",
            m.steps.map(function (s) { return s.at + " fpm = " + s.score; }).join("  ·  ")));
        }
        row.appendChild(right);
        sec.appendChild(row);
      });
      box.appendChild(sec);
    });

    if (d.alignment_note) box.appendChild(el("p", "gx-note", d.alignment_note));
    if (d.g_note) box.appendChild(el("p", "gx-note", d.g_note));
    if (d.cap_note) box.appendChild(el("p", "gx-note", d.cap_note));
    if ((d.limits || []).length) {
      box.appendChild(el("p", "gx-note", "What this cannot see:"));
      var ul = el("ul", "gx-limits");
      d.limits.forEach(function (t) { ul.appendChild(el("li", null, t)); });
      box.appendChild(ul);
    }
  }

  function renderGrading(d) {
    var host = $("#grading-body");
    host.innerHTML = "";
    host.appendChild(el("p", "gx-lead",
      "Each leg is split into phases and scored out of 100 on how smoothly it "
      + "was flown, then given a letter. Different aircraft are judged "
      + "differently, so pick the type below."));
    if (d.detection) host.appendChild(el("p", "gx-lead", d.detection));

    var tabs = el("div", "gx-tabs");
    var body = el("div");
    var profiles = d.profiles || [];
    profiles.forEach(function (prof, i) {
      var b = el("button", "gx-tab", prof.label);
      b.type = "button";
      b.setAttribute("aria-selected", i === 0 ? "true" : "false");
      b.addEventListener("click", function () {
        [].forEach.call(tabs.children, function (o) {
          o.setAttribute("aria-selected", "false");
        });
        b.setAttribute("aria-selected", "true");
        renderGradingProfile(d, prof, body);
        host.scrollTop = 0;
      });
      tabs.appendChild(b);
    });
    host.appendChild(tabs);
    host.appendChild(body);
    if (profiles.length) renderGradingProfile(d, profiles[0], body);
  }

  async function openGrading() {
    try {
      var r = await fetch(LOGGER + "/grading", { cache: "no-store" });
      var d = await r.json();
      if (!d.ok) throw new Error(d.error || "no grading description");
      renderGrading(d);
      $("#grading-panel").hidden = false;
    } catch (e) {
      setLive("could not load the grading description: " + e.message, "err");
    }
  }

  async function openSettings() {
    try {
      var r = await fetch(LOGGER + "/settings", { cache: "no-store" });
      var j = await r.json();
      if (!j.ok) { setLive((j.error || "settings unavailable"), "err"); return; }
      renderSettings(j);
      $("#settings-panel").hidden = false;
    } catch (e) {
      setLive("watcher not running on 127.0.0.1:8742", "err");
    }
  }

  async function saveSettings() {
    $("#settings-error").textContent = "";
    try {
      var j = await postJson("/settings", collectSettings());
      if (!j.ok) { $("#settings-error").textContent = j.error || "could not save"; return; }
      renderSettings(j);
      await refreshFromState();
      $("#settings-panel").hidden = true;
      setLive(j.rebuild_scheduled
        ? "settings saved — rebuilding the logbook"
        : "settings saved", "ok");
      // No fixed timer here any more: syncRebuild reloads on the build
      // counter, which is right however long the rebuild actually takes.
      if (j.rebuild_scheduled) schedulePoll(POLL_MS_BUILD);
    } catch (e) {
      $("#settings-error").textContent = "watcher not reachable";
    }
  }

  async function resetSettings() {
    var body = {};
    settingsSpec.forEach(function (it) { body[it.key] = it.default; });
    try {
      var j = await postJson("/settings", body);
      if (!j.ok) { $("#settings-error").textContent = j.error || "could not reset"; return; }
      renderSettings(j);
    } catch (e) {
      $("#settings-error").textContent = "watcher not reachable";
    }
  }

  // ------------------------------------------------------------ boot

  async function load() {
    try {
      var r = await fetch(LOGGER + "/logbook.json", { cache: "no-store" });
      if (!r.ok) throw new Error("http " + r.status);
      state.index = await r.json();
      state.detail = {};
      // A rebuild rewrites the month files, so anything held from before it
      // is stale. Cheaper and safer than working out which months moved.
      state.months = {};
      var months = state.index.months || [];
      // A watcher still running an older build writes the previous shape, and
      // the difference is one missing key - which rendered as an empty logbook
      // with no hint why. Say so instead.
      if (!state.index.months) {
        $("#sorties").innerHTML = "";
        $("#sorties").appendChild(el("div", "muted pad",
          "The watcher is running an older build than this page: it wrote a "
          + "logbook without months in it. Restart the watcher from the tray "
          + "and this will fill in."));
        renderSummary(state.index);
        return;
      }
      // Open on the newest month that has flights, and stay where the reader
      // was if that month still exists after a rebuild.
      if (!state.month || !monthMeta(state.month)) {
        var first = months.filter(function (m) { return m.sorties; })[0]
                    || months[0] || null;
        state.month = first ? first.ym : null;
      }
      renderSummary(state.index);
      renderHidden(state.index);
      renderFilters();
      await showList();
    } catch (e) {
      $("#sorties").innerHTML = "";
      // A first run has no logbook.json yet. Asking 'is the watcher
      // running?' while the watcher is right there building it is both
      // wrong and alarming.
      if (state.sawBuild) {
        $("#sorties").appendChild(el("div", "muted pad",
          "Building the logbook for the first time… This page will fill in when it finishes."));
      } else {
        $("#sorties").appendChild(el("div", "muted pad",
          "Could not load the logbook. Is the watcher running? " + e.message));
      }
    }
  }

  async function rebuild() {
    var btn = $("#rebuild");
    btn.disabled = true;
    btn.textContent = "Rebuilding…";
    try {
      var j = await (await fetch(LOGGER + "/logbook/rebuild", { method: "POST" })).json();
      if (j && j.deferred) setLive("rebuild held until the flight ends", "warn");
      await load();
    } catch (e) { /* load() reports it */ }
    btn.disabled = false;
    btn.textContent = "Rebuild";
  }

  function start() {
    wireLiveSwitches();
    $("#rebuild").addEventListener("click", rebuild);
    $("#open-removed").addEventListener("click", function () {
      $("#removed-panel").hidden = false;
    });
    $("#removed-close").addEventListener("click", function () {
      $("#removed-panel").hidden = true;
    });
    $("#open-grading").addEventListener("click", openGrading);
    $("#grading-close").addEventListener("click", function () {
      $("#grading-panel").hidden = true;
    });
    $("#open-settings").addEventListener("click", openSettings);
    $("#settings-cancel").addEventListener("click", function () {
      $("#settings-panel").hidden = true;
    });
    $("#settings-save").addEventListener("click", saveSettings);
    $("#settings-reset").addEventListener("click", resetSettings);
    $("#refresh").addEventListener("click", load);
    $("#month-open").addEventListener("click", function (ev) {
      ev.stopPropagation();
      toggleMonthPop();
    });
    $("#month-prev").addEventListener("click", function () { stepMonth(1); });
    $("#month-next").addEventListener("click", function () { stepMonth(-1); });
    document.addEventListener("click", function (ev) {
      var pop = $("#month-pop");
      if (!pop.hidden && !pop.contains(ev.target)) closeMonthPop();
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && !$("#month-pop").hidden) closeMonthPop();
    });
    $("#search").addEventListener("input", function (ev) {
      state.query = ev.target.value;
      renderFilters();
      showList();
    });
    var sortSel = $("#sort-order");
    try {
      var saved = localStorage.getItem("logbookSort");
      if (saved === "asc" || saved === "desc") state.sort = saved;
    } catch (e) {}
    sortSel.value = state.sort;
    sortSel.addEventListener("change", function (ev) {
      state.sort = ev.target.value === "asc" ? "asc" : "desc";
      try { localStorage.setItem("logbookSort", state.sort); } catch (e) {}
      renderList();
    });
    $("#expand-all").addEventListener("click", function () {
      var list = visibleSorties();
      var anyClosed = list.some(function (s) { return !state.expanded[s.sortie_id]; });
      list.forEach(function (s) {
        if (anyClosed) state.expanded[s.sortie_id] = true;
        else delete state.expanded[s.sortie_id];
      });
      renderList();
    });

    function applyLock() {
      var on = $("#chase-lock").checked;
      $("#bearing-row").hidden = !on;
      var hint = $("#chase-modehint");
      if (hint) hint.textContent = on ? "following" : "planted";
      postJson("/replay/lock", { locked: on, bearing: $("#chase-bearing").value })
        .then(function (j) {
          if (j && j.note) { setChaseStatus(j.note); return; }
          if (!on) { setChaseStatus("camera stays put; the ghost flies past"); return; }
          var c = (j && j.chase) || {};
          setChaseStatus("locked " + Math.round(c.distance || 0) + " m out, "
            + Math.round(c.height || 0) + " m up — "
            + ($("#chase-bearing").value === "world"
                ? "holding its bearing" : "turning with the aircraft"));
        })
        .catch(function () { setChaseStatus("watcher not reachable"); });
    }
    $("#chase-lock").addEventListener("change", applyLock);
    $("#chase-bearing").addEventListener("change", function () {
      if ($("#chase-lock").checked) applyLock();
    });
    $("#recenter").addEventListener("click", function () {
      setChaseStatus("recentering…");
      postJson("/replay/recenter", {}).then(function (j) {
        setChaseStatus(j && j.ok ? "camera moved to the ghost" : ((j && j.error) || "no replay running"));
      }).catch(function () { setChaseStatus("watcher not reachable"); });
    });

    document.addEventListener("click", function (ev) {
      if (ev.target.closest(".replay-btn")) setTimeout(pollState, 600);
      var b = ev.target.closest(".replay-pause");
      if (b && !b.disabled) {
        ev.preventDefault();
        postJson("/replay/pause", {}).then(function (j) {
          if (j && j.ok) syncPauseButtons(j.paused, true);
          else setChaseStatus((j && j.error) || "no replay running");
        }).catch(function () { setChaseStatus("watcher not reachable"); });
      }
    });

    window.addEventListener("resize", redrawAll);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") pollState();
    });

    measureStickyStack();
    window.addEventListener("resize", measureStickyStack);
    load();
    // The camera knobs carry the HTML defaults until something tells them
    // otherwise, and pollState skips entirely while the tab is in the
    // background - so sync once on load rather than depending on poll timing.
    refreshFromState();
    pollState();
    schedulePoll(POLL_MS);
    // Independent of the poll loop on purpose: if that loop is the thing
    // that has stopped, it cannot be the thing that notices.
    setInterval(bannerWatchdog, 2000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
