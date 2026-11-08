/* ==========================================================================
   Queue Sniper — App Dashboard
   Vanilla JS. Runs in two modes:
     • desktop  — inside QueueSniperDashboard.exe (pywebview bridge)
     • preview  — plain browser, demo controls so the layout can be reviewed
   ========================================================================== */

(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var store = {
    get: function (key, fallback) {
      try {
        var raw = localStorage.getItem(key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (err) {
        return fallback;
      }
    },
    set: function (key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch (err) { /* private mode */ }
    }
  };

  var DEFAULT_SERVERS = [
    {
      id: "srv-ranked",
      name: "Ranked Queue",
      tag: "QUEUE",
      tagClass: "tag-blue",
      initials: "RQ",
      avatar: "linear-gradient(135deg, #3b82f6, #8b5cf6)",
      meta: "2 channels · 4.2k members",
      selected: true,
      custom: false
    },
    {
      id: "srv-scrim",
      name: "Scrim Hub",
      tag: "SCRIM",
      tagClass: "tag-purple",
      initials: "SH",
      avatar: "linear-gradient(135deg, #a855f7, #ec4899)",
      meta: "1 channel · 860 members",
      selected: true,
      custom: false
    },
    {
      id: "srv-event",
      name: "Event Drops",
      tag: "EVENT",
      tagClass: "tag-amber",
      initials: "ED",
      avatar: "linear-gradient(135deg, #f59e0b, #ef4444)",
      meta: "3 channels · 12.4k members",
      selected: false,
      custom: false
    }
  ];

  var MODE_LABELS = {
    turbo: "Turbo",
    patience: "Patience",
    safe: "Safe",
    smart: "Smart"
  };

  var HELP_STEPS = [
    {
      title: "GET STARTED",
      sub: "Queue Sniper",
      html:
        '<p><strong>Queue Sniper</strong> watches the Discord channels you choose and reacts the moment a queue opens.</p>' +
        '<p>This window is the control panel. Keep it open while the sniper runs — closing it stops the sniper.</p>' +
        '<div class="callout"><strong>First run:</strong> add <span class="kbd">DISCORD_TOKEN</span> to <span class="kbd">.env</span>, pick your channels, then press <strong>START</strong>. A loud alarm plays when a queue is detected.</div>'
    },
    {
      title: "CONNECT ACCOUNT",
      sub: "Token & auth",
      html:
        '<p>Open <span class="kbd">.env</span> next to the app and paste your Discord token next to <span class="kbd">DISCORD_TOKEN=</span>.</p>' +
        '<p>The sniper keeps an authenticated connection warm so the click path stays fast — usually <strong>300–355 ms</strong> round-trip from this region.</p>' +
        '<div class="callout"><strong>Heads up:</strong> automating a user account is against Discord\'s Terms of Service. Use an alt account you can afford to lose.</div>'
    },
    {
      title: "WATCH CHANNELS",
      sub: "Servers & modes",
      html:
        '<p>Select channels in the <strong>Watchlist</strong>, then hit <strong>SAVE SETTINGS</strong>. Use <span class="kbd">+ Custom</span> to add another channel by link or ID.</p>' +
        '<p>Pick a click mode: <strong>Turbo</strong> is fastest, while <strong>Safe</strong> trades speed for rate-limit protection.</p>' +
        '<div class="callout">Run <strong>Diagnostics → RUN</strong> to verify your token, clock offset, gateway and channel access in one pass.</div>'
    }
  ];

  var DIAG_LINES = [
    ["$ queue-sniper doctor", "dim"],
    ["[1/5] reading .env .................. token found", "ok"],
    ["[2/5] clock offset .................. -0.80 s (applied)", "ok"],
    ["[3/5] gateway handshake ............. 15 ms", "ok"],
    ["[4/5] channel access ................ 3 servers / 8 channels", "ok"],
    ["[5/5] click path .................... 312 ms origin RTT", "ok"],
    ["ALL GOOD — ready to snipe", "ok"]
  ];

  var state = {
    running: false,
    filter: "all",
    option: "A",
    mode: "turbo",
    helpStep: 0,
    verbose: false,
    servers: [],
    diagnostics: null
  };

  var bridge = null;
  var isDesktop = false;
  var backend = {
    ready: false,
    status: "offline",
    seq: 0,
    lastEventSeq: 0,
    clearedAt: 0,
    tokenPresent: false,
    clockOffset: 0,
    latency: -1,
    watchCount: 0,
    stats: {},
    ui: null,
    pollWarned: false
  };

  function normalizeServers(raw) {
    if (!Array.isArray(raw)) return null;
    var cleaned = raw.filter(function (s) {
      return s && typeof s.name === "string" && s.name.trim();
    }).map(function (s, i) {
      return {
        id: typeof s.id === "string" ? s.id : "srv-" + i,
        name: s.name.slice(0, 40),
        tag: typeof s.tag === "string" ? s.tag.slice(0, 10) : "CUSTOM",
        tagClass: /^tag-(blue|purple|amber|green|muted)$/.test(s.tagClass) ? s.tagClass : "tag-green",
        initials: typeof s.initials === "string" ? s.initials.slice(0, 3) : "QS",
        avatar: typeof s.avatar === "string" ? s.avatar : "linear-gradient(135deg, #22c55e, #0ea5e9)",
        meta: typeof s.meta === "string" ? s.meta.slice(0, 64) : "Custom server",
        selected: !!s.selected,
        custom: !!s.custom
      };
    });
    return cleaned.length ? cleaned : null;
  }

  var savedServers = normalizeServers(store.get("qs-servers", null));
  state.servers = savedServers || DEFAULT_SERVERS.map(function (s) {
    return Object.assign({}, s);
  });

  /* ----------------------------------------------------------------------
     Toasts + activity log
     ---------------------------------------------------------------------- */

  var toastStack = $("#toastStack");

  function toast(message, kind) {
    var el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = message;
    toastStack.appendChild(el);
    requestAnimationFrame(function () { el.classList.add("show"); });
    setTimeout(function () {
      el.classList.remove("show");
      setTimeout(function () { el.remove(); }, 220);
    }, 2800);
  }

  var activityLog = $("#activityLog");
  var logEmpty = $("#logEmpty");
  var TAG_TEXT = { ok: "OK", info: "INFO", warn: "WARN", err: "ERR" };

  function log(tag, message, timeText) {
    if (logEmpty) logEmpty.hidden = true;

    var line = document.createElement("div");
    line.className = "log-line";

    var time = document.createElement("span");
    time.className = "log-time";
    time.textContent = timeText || new Date().toLocaleTimeString("en-GB", { hour12: false });

    var tagEl = document.createElement("span");
    tagEl.className = "log-tag " + tag;
    tagEl.textContent = TAG_TEXT[tag] || "INFO";

    var msg = document.createElement("span");
    msg.className = "log-message";
    msg.textContent = message;

    line.appendChild(time);
    line.appendChild(tagEl);
    line.appendChild(msg);
    activityLog.appendChild(line);

    while (activityLog.children.length > 180) {
      activityLog.removeChild(activityLog.firstChild);
    }
    activityLog.scrollTop = activityLog.scrollHeight;
  }

  function eventTag(level) {
    if (level === "ERROR") return "err";
    if (level === "WARN") return "warn";
    if (level === "OK" || level === "HIT") return "ok";
    return "info";
  }

  function resetLogDom() {
    activityLog.replaceChildren(logEmpty);
    if (logEmpty) logEmpty.hidden = false;
  }

  function clearLog() {
    if (isDesktop && bridge) {
      bridge.clear_log().then(function (res) {
        backend.seq = (res && typeof res.seq === "number") ? res.seq : backend.seq;
        backend.lastEventSeq = backend.seq;
        resetLogDom();
        toast("Activity log cleared");
      }, function () {
        resetLogDom();
        toast("Activity log cleared");
      });
      return;
    }
    resetLogDom();
    toast("Activity log cleared");
  }

  $("#clearLogBtn").addEventListener("click", clearLog);

  /* ----------------------------------------------------------------------
     Desktop bridge + polling
     ---------------------------------------------------------------------- */

  function connectBridge() {
    if (isDesktop) return;
    if (!window.pywebview || !window.pywebview.api) return;
    bridge = window.pywebview.api;
    isDesktop = true;
    document.body.classList.add("is-desktop");
    var idInput = $("#customId");
    if (idInput) {
      idInput.placeholder = "Channel ID or link (required)";
      idInput.required = true;
    }
    log("info", "Desktop engine connected");
    pollState(true);
    setInterval(function () { pollState(false); }, 900);
  }

  window.addEventListener("pywebviewready", connectBridge);

  function pollState(syncChannels) {
    if (!bridge) return;
    bridge.get_state(backend.seq).then(function (snapshot) {
      if (!snapshot || !snapshot.desktop) return;
      applySnapshot(snapshot, syncChannels || !backend.ready);
    }, function (err) {
      if (!backend.pollWarned) {
        backend.pollWarned = true;
        log("warn", "Lost contact with the desktop engine");
        console.error("get_state failed", err);
      }
    });
  }

  function applySnapshot(snapshot, syncChannels) {
    backend.ready = true;
    backend.status = snapshot.status || "offline";
    backend.tokenPresent = !!snapshot.token_present;
    backend.clockOffset = Number(snapshot.clock_offset) || 0;
    backend.latency = Number(snapshot.latency_ms);
    backend.watchCount = Number(snapshot.watch_count) || 0;
    backend.stats = snapshot.stats || {};

    if (snapshot.cleared_at && snapshot.cleared_at > backend.clearedAt) {
      backend.clearedAt = snapshot.cleared_at;
      resetLogDom();
    }

    var events = snapshot.events || [];
    events.forEach(function (event) {
      if (event.seq <= backend.lastEventSeq) return;
      backend.lastEventSeq = event.seq;
      log(eventTag(event.level), event.message, event.time);
    });

    if (typeof snapshot.seq === "number") backend.seq = snapshot.seq;

    applyLiveStatus(snapshot);

    if (syncChannels) {
      var live = normalizeServers(snapshot.channels);
      if (live) {
        state.servers = live;
        state.filter = "all";
        $$("#filterTabs .tab").forEach(function (tab) {
          var on = tab.dataset.filter === "all";
          tab.classList.toggle("active", on);
          tab.setAttribute("aria-selected", String(on));
        });
        $("#customForm").classList.remove("open");
        renderServers();
      }
    }

    if (snapshot.click_mode && MODE_LABELS[snapshot.click_mode] && snapshot.click_mode !== state.mode) {
      applyMode(snapshot.click_mode, true);
    }

    if (snapshot.ui && !backend.ui) {
      backend.ui = snapshot.ui;
      applyUiSettings(snapshot.ui);
    }

    if (!isDesktop) return;
    updateHelpChecks();
  }

  function applyLiveStatus(snapshot) {
    var status = snapshot.status || "offline";
    var labels = {
      online: "ONLINE",
      connecting: "CONNECTING",
      stopping: "STOPPING",
      error: "ERROR",
      offline: "OFFLINE"
    };
    var card = $("#statusCard");
    card.classList.toggle("is-online", status === "online");
    card.classList.toggle("is-connecting", status === "connecting" || status === "stopping");
    card.classList.toggle("is-error", status === "error");

    $("#statusLabel").textContent = labels[status] || "OFFLINE";
    $("#statusSub").textContent = snapshot.status_text ||
      (status === "online" ? "Connected to Discord" : "Not logged in");

    $("#startBtn").disabled = !!snapshot.running;
    $("#stopBtn").disabled = !snapshot.running;
    state.running = !!snapshot.running;
    if (snapshot.click_mode && MODE_LABELS[snapshot.click_mode]) {
      $("#watchingBadge").textContent = snapshot.click_mode.toUpperCase();
    }
  }

  /* ----------------------------------------------------------------------
     Start / stop
     ---------------------------------------------------------------------- */

  function watchCount() {
    return state.servers.filter(function (s) { return s.selected; }).length;
  }

  function demoSetRunning(on) {
    state.running = on;
    var card = $("#statusCard");
    card.classList.remove("is-connecting", "is-error");
    card.classList.toggle("is-online", on);

    $("#statusLabel").textContent = on ? "ONLINE" : "OFFLINE";
    $("#statusSub").textContent = on
      ? "Watching " + watchCount() + " channel" + (watchCount() === 1 ? "" : "s")
      : "Not logged in";
    $("#startBtn").disabled = on;
    $("#stopBtn").disabled = !on;

    if (on) {
      log("ok", "Sniper started");
      setTimeout(function () {
        if (!state.running) return;
        log("info", "Gateway connected · edge 15 ms");
      }, 260);
      setTimeout(function () {
        if (!state.running) return;
        log("info", "Watching " + watchCount() + " channels · " + MODE_LABELS[state.mode] + " mode");
      }, 540);
      setTimeout(function () {
        if (!state.running) return;
        log("ok", "Clock offset applied · -0.80 s");
      }, 820);
      toast("Sniper is watching", "ok");
    } else {
      log("warn", "Sniper stopped");
      toast("Sniper stopped", "warn");
    }
  }

  function startSniper() {
    if (isDesktop && bridge) {
      $("#startBtn").disabled = true;
      $("#statusLabel").textContent = "CONNECTING";
      $("#statusSub").textContent = "Starting sniper...";
      $("#statusCard").classList.add("is-connecting");
      bridge.start_sniper().then(function (res) {
        if (!res || !res.ok) {
          toast((res && res.error) || "Could not start the sniper", "warn");
          pollState(false);
          return;
        }
        toast("Connecting to Discord...", "ok");
        setTimeout(function () { pollState(false); }, 300);
      }, function (err) {
        toast("Start failed: " + err, "warn");
        pollState(false);
      });
      return;
    }
    if (!window.pywebview) demoSetRunning(true);
  }

  function stopSniper() {
    if (isDesktop && bridge) {
      $("#stopBtn").disabled = true;
      $("#statusLabel").textContent = "STOPPING";
      $("#statusSub").textContent = "Closing the Discord connection...";
      $("#statusCard").classList.add("is-connecting");
      bridge.stop_sniper().then(function () {
        toast("Sniper stopped", "warn");
        pollState(false);
      }, function (err) {
        toast("Stop failed: " + err, "warn");
        pollState(false);
      });
      return;
    }
    if (!window.pywebview) demoSetRunning(false);
  }

  $("#startBtn").addEventListener("click", function () {
    if (!state.running) startSniper();
  });

  $("#stopBtn").addEventListener("click", function () {
    if (state.running) stopSniper();
  });

  /* ----------------------------------------------------------------------
     Server list, tabs, custom servers, save
     ---------------------------------------------------------------------- */

  function visibleServers() {
    if (state.filter === "selected") {
      return state.servers.filter(function (s) { return s.selected; });
    }
    if (state.filter === "custom") {
      return state.servers.filter(function (s) { return s.custom; });
    }
    return state.servers;
  }

  function renderServers() {
    var list = $("#serverList");
    list.replaceChildren();
    var items = visibleServers();

    if (!items.length) {
      var empty = document.createElement("div");
      empty.className = "server-empty";
      var strong = document.createElement("strong");
      strong.textContent = state.filter === "custom" ? "No custom channels yet" : "Nothing selected";
      var span = document.createElement("span");
      span.textContent = state.filter === "custom"
        ? "Use the form below to add one."
        : "Open the All tab and pick a channel.";
      empty.appendChild(strong);
      empty.appendChild(span);
      list.appendChild(empty);
      return;
    }

    items.forEach(function (server) {
      var card = document.createElement("div");
      card.className = "server-card" + (server.selected ? " selected" : "");
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-pressed", String(!!server.selected));

      var avatar = document.createElement("span");
      avatar.className = "server-avatar";
      avatar.style.setProperty("--av", server.avatar);
      avatar.textContent = server.initials;

      var info = document.createElement("span");
      info.className = "server-info";
      var name = document.createElement("span");
      name.className = "server-name";
      name.textContent = server.name;
      var meta = document.createElement("span");
      meta.className = "server-meta";
      meta.textContent = server.meta;
      info.appendChild(name);
      info.appendChild(meta);

      var tag = document.createElement("span");
      tag.className = "tag " + server.tagClass;
      tag.textContent = server.tag;

      var gear = document.createElement("button");
      gear.type = "button";
      gear.className = "icon-btn gear";
      gear.title = "Channel settings";
      gear.setAttribute("aria-label", "Settings for " + server.name);
      gear.innerHTML = '<svg class="icon"><use href="#i-gear" /></svg>';
      gear.addEventListener("click", function (event) {
        event.stopPropagation();
        toast(server.name + ": " + server.meta);
        log("info", "Opened settings for " + server.name);
      });

      function toggle() {
        server.selected = !server.selected;
        log(server.selected ? "ok" : "warn", server.name + (server.selected ? " added to" : " removed from") + " watchlist");
        renderServers();
      }

      card.addEventListener("click", toggle);
      card.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggle();
        }
      });

      card.appendChild(avatar);
      card.appendChild(info);
      card.appendChild(tag);
      card.appendChild(gear);
      list.appendChild(card);
    });
  }

  $$("#filterTabs .tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      state.filter = tab.dataset.filter;
      $$("#filterTabs .tab").forEach(function (t) {
        var on = t === tab;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", String(on));
      });
      var form = $("#customForm");
      form.classList.toggle("open", state.filter === "custom");
      renderServers();
      if (state.filter === "custom") $("#customName").focus();
    });
  });

  $("#customForm").addEventListener("submit", function (event) {
    event.preventDefault();
    var nameInput = $("#customName");
    var idInput = $("#customId");
    var name = nameInput.value.trim();
    var ref = idInput.value.trim();

    if (!name) {
      toast("Enter a channel name", "warn");
      nameInput.focus();
      return;
    }

    if (isDesktop && bridge) {
      if (!ref) {
        toast("Paste the channel ID or a channel link", "warn");
        idInput.focus();
        return;
      }
      var addBtn = $("#customForm button[type=submit]");
      addBtn.disabled = true;
      addBtn.textContent = "ADDING";
      bridge.add_channel(name, ref).then(function (res) {
        addBtn.disabled = false;
        addBtn.textContent = "ADD";
        if (!res || !res.ok) {
          toast((res && res.error) || "Could not add that channel", "warn");
          return;
        }
        $("#customForm").reset();
        toast("Channel added", "ok");
        pollState(true);
      }, function (err) {
        addBtn.disabled = false;
        addBtn.textContent = "ADD";
        toast("Add failed: " + err, "warn");
      });
      return;
    }

    var initials = name.split(/\s+/).slice(0, 2).map(function (word) {
      return word.charAt(0);
    }).join("").toUpperCase() || "QS";

    state.servers.push({
      id: "custom-" + Date.now(),
      name: name.slice(0, 40),
      tag: "CUSTOM",
      tagClass: "tag-green",
      initials: initials,
      avatar: "linear-gradient(135deg, #22c55e, #0ea5e9)",
      meta: ref ? "ID " + ref.slice(0, 6) + "••••" : "Custom server · 1 channel",
      selected: true,
      custom: true
    });

    $("#customForm").reset();
    renderServers();
    log("ok", "Custom server added: " + name);
    toast("Custom server added", "ok");
  });

  function saveSettings() {
    if (isDesktop && bridge) {
      var ids = state.servers.filter(function (s) { return s.selected; }).map(function (s) {
        return s.id;
      });
      if (!ids.length) {
        toast("Select at least one channel", "warn");
        return;
      }
      var btn = $("#saveSettingsBtn");
      btn.disabled = true;
      btn.textContent = "SAVING";
      bridge.save_settings({ channels: ids, clickMode: state.mode, option: state.option }).then(function (res) {
        btn.disabled = false;
        btn.textContent = "SAVE SETTINGS";
        if (!res || !res.ok) {
          toast((res && res.error) || "Could not save settings", "warn");
          return;
        }
        toast(res.message || "Settings saved", "ok");
        pollState(true);
      }, function (err) {
        btn.disabled = false;
        btn.textContent = "SAVE SETTINGS";
        toast("Save failed: " + err, "warn");
      });
      return;
    }

    store.set("qs-servers", state.servers);
    log("ok", "Settings saved · " + watchCount() + " of " + state.servers.length + " servers selected");
    toast("Settings saved", "ok");
    if (state.running) {
      $("#statusSub").textContent = "Watching " + watchCount() + " channel" + (watchCount() === 1 ? "" : "s");
    }
  }

  $("#saveSettingsBtn").addEventListener("click", saveSettings);

  /* ----------------------------------------------------------------------
     Option toggle + click mode
     ---------------------------------------------------------------------- */

  $$("#optionToggle button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.option = btn.dataset.option;
      $$("#optionToggle button").forEach(function (b) {
        var on = b === btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", String(on));
      });
      store.set("qs-option", state.option);
      saveUiSettings({ option: state.option });
      log("info", "Watchlist option " + state.option + " selected");
      toast("Option " + state.option + " selected");
    });
  });

  function applyMode(mode, silent) {
    if (!MODE_LABELS[mode]) mode = "turbo";
    state.mode = mode;
    $$(".mode-item").forEach(function (item) {
      item.classList.toggle("active", item.dataset.mode === mode);
    });
    $("#modeHint").textContent = MODE_LABELS[mode];
    $("#watchingBadge").textContent = mode.toUpperCase();
    var checkMode = $("#checkMode");
    if (checkMode) checkMode.textContent = MODE_LABELS[mode];
    if (!silent) {
      store.set("qs-mode", mode);
      if (isDesktop && bridge) {
        bridge.set_click_mode(mode).then(function (res) {
          if (!res || !res.ok) toast((res && res.error) || "Could not change mode", "warn");
        });
      } else {
        log("info", "Click mode set to " + MODE_LABELS[mode]);
      }
      toast("Click mode: " + MODE_LABELS[mode]);
    }
  }

  $$(".mode-item").forEach(function (item) {
    item.addEventListener("click", function () {
      if (item.dataset.mode !== state.mode) applyMode(item.dataset.mode, false);
    });
  });

  /* ----------------------------------------------------------------------
     Help steps
     ---------------------------------------------------------------------- */

  function helpValues() {
    var gateway = backend.latency >= 0 ? backend.latency.toFixed(0) + " ms" : "offline";
    var channels = backend.ready ? backend.watchCount : watchCount();
    return {
      token: backend.ready ? (backend.tokenPresent ? "loaded" : "missing") : "checking",
      clock: backend.ready ? (backend.clockOffset >= 0 ? "+" : "") + backend.clockOffset.toFixed(2) + " s" : "checking",
      gateway: backend.ready ? gateway : "checking",
      channels: channels + " channel" + (channels === 1 ? "" : "s"),
      mode: MODE_LABELS[state.mode]
    };
  }

  function helpBlock() {
    var v = helpValues();
    return (
      '<div class="help-block">' +
        '<div class="help-block-title">Setup Check</div>' +
        '<div class="check-row"><span class="check-dot"></span><span class="check-label">Discord token</span><span class="check-value" id="checkToken">' + v.token + '</span></div>' +
        '<div class="check-row"><span class="check-dot"></span><span class="check-label">Clock offset</span><span class="check-value" id="checkClock">' + v.clock + '</span></div>' +
        '<div class="check-row"><span class="check-dot"></span><span class="check-label">Gateway</span><span class="check-value" id="checkGateway">' + v.gateway + '</span></div>' +
        '<div class="check-row"><span class="check-dot"></span><span class="check-label">Channel access</span><span class="check-value" id="checkChannels">' + v.channels + '</span></div>' +
        '<div class="check-row"><span class="check-dot"></span><span class="check-label">Click mode</span><span class="check-value" id="checkMode">' + v.mode + '</span></div>' +
      '</div>' +
      '<div class="help-block">' +
        '<div class="help-block-title">Quick Keys</div>' +
        '<div class="key-row"><span>Start / stop</span><span class="kbd">Space</span></div>' +
        '<div class="key-row"><span>Clear activity log</span><span class="kbd">Ctrl + L</span></div>' +
        '<div class="key-row"><span>Run diagnostics</span><span class="kbd">Ctrl + D</span></div>' +
        '<div class="key-row"><span>Close window</span><span class="kbd">Alt + F4</span></div>' +
      '</div>'
    );
  }

  function updateHelpChecks() {
    var v = helpValues();
    var pairs = {
      checkToken: v.token,
      checkClock: v.clock,
      checkGateway: v.gateway,
      checkChannels: v.channels,
      checkMode: v.mode
    };
    Object.keys(pairs).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.textContent = pairs[id];
    });
  }

  function renderHelp() {
    var step = HELP_STEPS[state.helpStep];
    $("#stepNum").textContent = String(state.helpStep + 1);
    $("#stepTitle").innerHTML = step.title + " <small>" + step.sub + "</small>";
    $("#helpBody").innerHTML = step.html + helpBlock();
    $("#stepCounter").textContent = (state.helpStep + 1) + " / " + HELP_STEPS.length;
    $("#helpStepLabel").textContent = "STEP " + (state.helpStep + 1) + " OF " + HELP_STEPS.length;
    $("#helpPrev").disabled = state.helpStep === 0;
    $("#helpNext").disabled = state.helpStep === HELP_STEPS.length - 1;

    var dots = $("#helpDots");
    dots.replaceChildren();
    HELP_STEPS.forEach(function (s, index) {
      var dot = document.createElement("button");
      dot.type = "button";
      dot.className = "page-dot" + (index === state.helpStep ? " active" : "");
      dot.setAttribute("aria-label", "Step " + (index + 1) + ": " + s.title);
      dot.addEventListener("click", function () {
        state.helpStep = index;
        renderHelp();
      });
      dots.appendChild(dot);
    });
  }

  $("#helpPrev").addEventListener("click", function () {
    if (state.helpStep > 0) {
      state.helpStep -= 1;
      renderHelp();
    }
  });

  $("#helpNext").addEventListener("click", function () {
    if (state.helpStep < HELP_STEPS.length - 1) {
      state.helpStep += 1;
      renderHelp();
    }
  });

  $("#statsLink").addEventListener("click", function (event) {
    event.preventDefault();
    var s = backend.stats || {};
    var summary = backend.ready
      ? "Messages " + (s.messages || 0) + " · detected " + (s.detected || 0) + " · clicks " + (s.clicks || 0) + " · errors " + (s.errors || 0)
      : "Start the sniper to collect stats";
    toast(summary);
    log("info", summary);
  });

  /* ----------------------------------------------------------------------
     Diagnostics
     ---------------------------------------------------------------------- */

  var diagRunning = false;

  function appendDiagLine(text, kind) {
    var out = $("#diagOutput");
    var line = document.createElement("span");
    if (kind) line.className = kind;
    line.textContent = text + "\n";
    out.appendChild(line);
    out.scrollTop = out.scrollHeight;
  }

  function appendNerds() {
    var out = $("#diagOutput");
    var extra = document.createElement("span");
    extra.className = "dim";
    extra.textContent =
      "\n--- nerd stats ---\n" +
      "region ....................... NZ / edge\n" +
      "keepalive .................... GET /users/@me\n" +
      "clock offset ................. " + (backend.clockOffset >= 0 ? "+" : "") + backend.clockOffset.toFixed(2) + " s\n" +
      "http / gateway ............... 312 ms / " + (backend.latency >= 0 ? backend.latency.toFixed(0) : "15") + " ms\n" +
      "timer resolution ............. 1 ms\n";
    out.appendChild(extra);
    out.scrollTop = out.scrollHeight;
  }

  function renderDiagnostics(lines) {
    var out = $("#diagOutput");
    out.replaceChildren();
    lines.forEach(function (entry) {
      appendDiagLine(entry[0], entry[1]);
    });
    if (state.verbose) appendNerds();
  }

  function finishDiagnostics(btn) {
    diagRunning = false;
    btn.disabled = false;
    btn.textContent = "RUN";
  }

  function runDiagnostics() {
    if (diagRunning) return;
    diagRunning = true;

    var btn = $("#runDiagBtn");
    $("#diagOutput").replaceChildren();
    btn.disabled = true;
    btn.textContent = "RUNNING";

    if (isDesktop && bridge) {
      log("info", "Running diagnostics...");
      bridge.run_diagnostics().then(function (res) {
        finishDiagnostics(btn);
        if (!res || !res.ok) {
          appendDiagLine("diagnostics failed", "err");
          toast("Diagnostics failed", "warn");
          return;
        }
        var lines = res.lines || [];
        var nerdAt = -1;
        for (var i = 0; i < lines.length; i += 1) {
          if (lines[i][0] && lines[i][0].indexOf("nerd stats") !== -1) { nerdAt = i; break; }
        }
        if (!state.verbose && nerdAt >= 0) lines = lines.slice(0, nerdAt - 1);
        state.diagnostics = res.lines || [];
        renderDiagnostics(lines);
        var last = lines.length ? lines[lines.length - 1][0] : "";
        toast(last.indexOf("NOT READY") === 0 ? "Diagnostics found a problem" : "Diagnostics complete",
          last.indexOf("NOT READY") === 0 ? "warn" : "ok");
      }, function (err) {
        finishDiagnostics(btn);
        appendDiagLine("diagnostics failed: " + err, "err");
        toast("Diagnostics failed", "warn");
      });
      return;
    }

    log("info", "Running diagnostics...");
    var index = 0;
    var timer = setInterval(function () {
      if (index >= DIAG_LINES.length) {
        clearInterval(timer);
        finishDiagnostics(btn);
        state.diagnostics = DIAG_LINES;
        log("ok", "Diagnostics complete · ALL GOOD");
        toast("Diagnostics passed", "ok");
        if (state.verbose) appendNerds();
        return;
      }
      var entry = DIAG_LINES[index];
      index += 1;
      appendDiagLine(entry[0], entry[1]);
    }, 240);
  }

  $("#runDiagBtn").addEventListener("click", runDiagnostics);

  $("#copyDiagBtn").addEventListener("click", function () {
    var text = $("#diagOutput").textContent;
    function fallback() {
      var area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      area.remove();
      toast(ok ? "Diagnostics copied" : "Copy failed", ok ? "ok" : "warn");
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast("Diagnostics copied", "ok");
      }, fallback);
    } else {
      fallback();
    }
    log("info", "Diagnostics copied to clipboard");
  });

  $("#nerdsBtn").addEventListener("click", function () {
    state.verbose = !state.verbose;
    $("#nerdsBtn").classList.toggle("active", state.verbose);
    if (state.diagnostics) {
      var lines = state.diagnostics;
      if (!state.verbose) {
        for (var i = 0; i < lines.length; i += 1) {
          if (lines[i][0] && lines[i][0].indexOf("nerd stats") !== -1) {
            lines = lines.slice(0, i - 1);
            break;
          }
        }
      }
      renderDiagnostics(lines);
    } else if (state.verbose) {
      appendNerds();
    }
    toast(state.verbose ? "Nerd stats on" : "Nerd stats off");
  });

  /* ----------------------------------------------------------------------
     Toggles, volume, uploads, webhook
     ---------------------------------------------------------------------- */

  function updateWebhook(on) {
    var details = $("#webhookDetails");
    details.classList.toggle("is-muted", !on);
    $("#webhookUrl").disabled = !on;
    $("#testWebhookBtn").disabled = !on;
  }

  function saveUiSettings(patch) {
    if (!isDesktop || !bridge) return;
    var payload = patch || {};
    if (!patch) {
      payload = {
        volume: parseInt(volumeSlider.value, 10),
        desktop_notifications: $("#switchDesktop") ? $("#switchDesktop").classList.contains("on") : true,
        sound_notifications: $("#switchSound") ? $("#switchSound").classList.contains("on") : true,
        option: state.option
      };
    }
    bridge.save_ui_settings(payload).then(function () { /* saved silently */ });
  }

  function applyUiSettings(ui) {
    if (!ui) return;
    if (typeof ui.volume === "number") {
      volumeSlider.value = ui.volume;
      paintVolume();
    }
    if (typeof ui.sound_notifications === "boolean") {
      setSwitch($("#switchSound"), ui.sound_notifications);
      $("#volumeRow").classList.toggle("is-muted", !ui.sound_notifications);
    }
    if (typeof ui.desktop_notifications === "boolean") {
      setSwitch($("#switchDesktop"), ui.desktop_notifications);
    }
  }

  function setSwitch(sw, on) {
    if (!sw) return;
    sw.classList.toggle("on", on);
    sw.setAttribute("aria-checked", String(on));
  }

  $$(".switch").forEach(function (sw) {
    sw.addEventListener("click", function () {
      var on = !sw.classList.contains("on");
      sw.classList.toggle("on", on);
      sw.setAttribute("aria-checked", String(on));

      var label = sw.getAttribute("aria-label") || "Setting";
      if (sw.id === "webhookToggle") {
        updateWebhook(on);
        log(on ? "ok" : "info", "Webhook notifications " + (on ? "enabled" : "disabled"));
        if (on) toast("Webhook notifications enabled");
      } else {
        if (label === "Sound Notifications") {
          $("#volumeRow").classList.toggle("is-muted", !on);
        }
        log("info", label + " " + (on ? "on" : "off"));
        saveUiSettings();
      }
    });
  });

  var volumeSlider = $("#volumeSlider");
  var volumeTimer = null;

  function paintVolume() {
    var value = volumeSlider.value;
    volumeSlider.style.setProperty("--vol", value + "%");
    $("#volumeValue").textContent = value + "%";
  }

  volumeSlider.addEventListener("input", function () {
    paintVolume();
    clearTimeout(volumeTimer);
    volumeTimer = setTimeout(function () {
      saveUiSettings({ volume: parseInt(volumeSlider.value, 10) });
    }, 250);
  });
  paintVolume();

  var uploadTarget = null;
  var fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = "audio/*";
  fileInput.hidden = true;
  document.body.appendChild(fileInput);

  $$(".upload-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      uploadTarget = btn.closest(".sound-row");
      fileInput.value = "";
      fileInput.click();
    });
  });

  fileInput.addEventListener("change", function () {
    var file = fileInput.files && fileInput.files[0];
    if (!file || !uploadTarget) return;
    var label = file.name.replace(/\.[^.]+$/, "").slice(0, 24);
    var nameEl = uploadTarget.querySelector("[data-sound-name]");
    if (nameEl) nameEl.textContent = label;
    log("ok", "Sound selected: " + label);
    toast("Sound selected · custom sounds are UI-only for now", "warn");
    uploadTarget = null;
  });

  $("#testWebhookBtn").addEventListener("click", function () {
    var input = $("#webhookUrl");
    var url = input.value.trim();
    if (!url) {
      toast("Paste a webhook URL first", "warn");
      log("warn", "Webhook test failed · no URL");
      input.focus();
      return;
    }
    if (!/^https:\/\/(discord|discordapp)\.com\/api\/webhooks\//i.test(url)) {
      toast("That does not look like a Discord webhook", "warn");
      log("warn", "Webhook test failed · invalid URL");
      return;
    }

    var btn = $("#testWebhookBtn");
    btn.disabled = true;
    btn.textContent = "TESTING";
    log("info", "Sending test webhook...");

    if (isDesktop && bridge) {
      bridge.test_webhook(url).then(function (res) {
        btn.disabled = false;
        btn.textContent = "TEST WEBHOOK";
        if (!res || !res.ok) {
          toast((res && res.error) || "Webhook test failed", "warn");
          return;
        }
        toast("Test webhook delivered", "ok");
      }, function (err) {
        btn.disabled = false;
        btn.textContent = "TEST WEBHOOK";
        toast("Webhook test failed: " + err, "warn");
      });
      return;
    }

    setTimeout(function () {
      btn.disabled = false;
      btn.textContent = "TEST WEBHOOK";
      toast("Test webhook delivered", "ok");
      log("ok", "Webhook delivered · 204");
    }, 700);
  });

  /* ----------------------------------------------------------------------
     Top bar + window chrome
     ---------------------------------------------------------------------- */

  var themeSelect = $("#themeSelect");

  function applyTheme(theme, silent) {
    if (theme !== "midnight") theme = "dark";
    document.documentElement.dataset.theme = theme;
    themeSelect.value = theme;
    store.set("qs-theme", theme);
    if (!silent) toast((theme === "midnight" ? "Midnight" : "Dark") + " theme applied", "ok");
  }

  themeSelect.addEventListener("change", function () {
    applyTheme(themeSelect.value, false);
  });

  $("#bellBtn").addEventListener("click", function () {
    $("#bellBtn").classList.remove("has-dot");
    toast("No unread alerts");
  });

  $("#helpBtn").addEventListener("click", function () {
    var card = $("#helpCard");
    card.classList.remove("pulse");
    void card.offsetWidth;
    card.classList.add("pulse");
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    setTimeout(function () { card.classList.remove("pulse"); }, 950);
  });

  $("#settingsBtn").addEventListener("click", function () {
    toast("Theme, alerts and webhook live here");
    var panel = $(".panel.right");
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  $("#logoutBtn").addEventListener("click", function () {
    if (state.running) stopSniper();
    log("warn", "Session logged out");
    toast("Sniper stopped", "warn");
  });

  var appWindow = $("#appWindow");
  var closedOverlay = $("#closedOverlay");

  function minimizeWindow() {
    if (isDesktop && bridge) {
      bridge.minimize_window();
      return;
    }
    appWindow.classList.toggle("minimized");
  }

  function maximizeWindow() {
    if (isDesktop && bridge) {
      bridge.toggle_maximize();
      return;
    }
    var on = appWindow.classList.toggle("maximized");
    document.body.classList.toggle("maximized", on);
  }

  function closeWindow() {
    if (isDesktop && bridge) {
      bridge.close_app();
      return;
    }
    appWindow.classList.remove("minimized");
    appWindow.classList.add("closed");
    setTimeout(function () { closedOverlay.classList.add("show"); }, 120);
  }

  function reopenWindow() {
    closedOverlay.classList.remove("show");
    appWindow.classList.remove("closed");
  }

  $("#redLight").addEventListener("click", closeWindow);
  $("#yellowLight").addEventListener("click", minimizeWindow);
  $("#greenLight").addEventListener("click", maximizeWindow);
  $("#minBtn").addEventListener("click", minimizeWindow);
  $("#maxBtn").addEventListener("click", maximizeWindow);
  $("#closeBtn").addEventListener("click", closeWindow);
  $("#reopenBtn").addEventListener("click", reopenWindow);

  var titlebar = $(".titlebar");
  if (titlebar) {
    titlebar.addEventListener("dblclick", function (event) {
      if (event.target.closest("button")) return;
      maximizeWindow();
    });
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      if (closedOverlay.classList.contains("show")) reopenWindow();
      return;
    }
    if (event.defaultPrevented) return;

    var active = document.activeElement;
    var tag = active ? active.tagName : "";
    var typing = tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || (active && active.isContentEditable);
    if (typing) return;

    if (event.code === "Space" && tag !== "BUTTON" && tag !== "A") {
      event.preventDefault();
      if (state.running) stopSniper();
      else startSniper();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "l") {
      event.preventDefault();
      clearLog();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "d") {
      event.preventDefault();
      runDiagnostics();
    }
  });

  /* ----------------------------------------------------------------------
     Init
     ---------------------------------------------------------------------- */

  applyTheme(store.get("qs-theme", "dark"), true);
  applyMode(store.get("qs-mode", "turbo"), true);
  state.option = store.get("qs-option", "A") === "B" ? "B" : "A";
  $$("#optionToggle button").forEach(function (b) {
    var on = b.dataset.option === state.option;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
  renderServers();
  renderHelp();
  updateWebhook(false);

  connectBridge();
  setTimeout(connectBridge, 300);
  setTimeout(connectBridge, 1200);
})();
