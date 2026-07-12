/*!
 * Atelier multi-tab event coordination.
 * One leader tab opens EventSource; others listen via BroadcastChannel.
 * Falls back to per-tab SSE, then 10s polling if needed.
 */
(function (global) {
  "use strict";

  var rt = global.AtelierRuntime;
  if (!rt || !rt.ready) {
    console.warn("[AtelierEvents] AtelierRuntime not ready");
    return;
  }

  var projectKey = rt.projectKey || "legacy";
  var channelName = "atelier:" + projectKey;
  var leaderKey = "atelier-leader:" + projectKey;
  var LEADER_TTL_MS = 4000;
  var POLL_MS = 10000;
  var listeners = [];
  var source = null;
  var pollTimer = null;
  var isLeader = false;
  var channel = null;

  try {
    if (typeof BroadcastChannel !== "undefined") {
      channel = new BroadcastChannel(channelName);
      channel.onmessage = function (ev) {
        if (ev && ev.data && ev.data.type === "atelier-event") {
          dispatch(ev.data.payload);
        }
      };
    }
  } catch (_) {
    channel = null;
  }

  function dispatch(payload) {
    for (var i = 0; i < listeners.length; i++) {
      try {
        listeners[i](payload);
      } catch (err) {
        console.error("[AtelierEvents] listener error", err);
      }
    }
  }

  function now() {
    return Date.now();
  }

  function tryBecomeLeader() {
    try {
      var raw = localStorage.getItem(leaderKey);
      var claim = raw ? JSON.parse(raw) : null;
      if (
        claim &&
        claim.id &&
        claim.expires > now() &&
        claim.id !== leaderId
      ) {
        return false;
      }
      localStorage.setItem(
        leaderKey,
        JSON.stringify({ id: leaderId, expires: now() + LEADER_TTL_MS })
      );
      return true;
    } catch (_) {
      return true;
    }
  }

  var leaderId =
    "tab-" +
    Math.random().toString(36).slice(2) +
    "-" +
    now().toString(36);

  function openSse() {
    if (source) return;
    var url = rt.eventsUrl ? rt.eventsUrl() : "/events";
    try {
      source = new EventSource(url);
    } catch (err) {
      console.warn("[AtelierEvents] EventSource failed", err);
      startPoll();
      return;
    }
    source.onmessage = function (ev) {
      var payload = null;
      try {
        payload = JSON.parse(ev.data);
      } catch (_) {
        payload = { type: "raw", data: ev.data };
      }
      dispatch(payload);
      if (channel && isLeader) {
        channel.postMessage({ type: "atelier-event", payload: payload });
      }
    };
    source.onerror = function () {
      // Keep EventSource auto-reconnect; only poll if permanently closed.
      if (source && source.readyState === 2) {
        closeSse();
        startPoll();
      }
    };
  }

  function closeSse() {
    if (source) {
      try {
        source.close();
      } catch (_) {}
      source = null;
    }
  }

  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      dispatch({ type: "poll.tick", timestamp: now() });
    }, POLL_MS);
  }

  function stopPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function elect() {
    var shouldLead = !channel || tryBecomeLeader();
    if (shouldLead) {
      isLeader = true;
      stopPoll();
      openSse();
    } else {
      isLeader = false;
      closeSse();
      // Followers only receive via BroadcastChannel; light poll as safety net.
      // Do NOT use 1.8s polling — plan forbids it when SSE is healthy.
      if (!channel) openSse();
    }
  }

  setInterval(function () {
    if (isLeader) {
      tryBecomeLeader(); // renew lease
    } else {
      elect();
    }
  }, 1500);

  elect();

  global.AtelierEvents = {
    on: function (fn) {
      if (typeof fn === "function") listeners.push(fn);
      return function off() {
        listeners = listeners.filter(function (x) {
          return x !== fn;
        });
      };
    },
    isLeader: function () {
      return isLeader;
    },
    projectKey: projectKey,
  };
})(typeof window !== "undefined" ? window : globalThis);
