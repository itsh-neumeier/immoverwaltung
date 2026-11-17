/* PWA (Service Worker, Installieren-Knopf) und mobiles Menü */
(function () {
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  }
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  let deferred = null;
  const buttons = () => document.querySelectorAll("[data-install]");

  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferred = e;
    buttons().forEach((b) => (b.hidden = false));
    const hint = document.getElementById("st-hint");
    if (hint) hint.hidden = true;
  });
  window.addEventListener("appinstalled", () => {
    deferred = null;
    buttons().forEach((b) => (b.hidden = true));
  });
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-install]");
    if (btn && deferred) {
      deferred.prompt();
      await deferred.userChoice;
      deferred = null;
      buttons().forEach((b) => (b.hidden = true));
    }
    const themeBtn = e.target.closest("[data-theme-toggle]");
    if (themeBtn) {  // Automatisch → Hell → Dunkel → Automatisch (gemerkt per Cookie, gilt für dieses Gerät)
      const order = ["auto", "light", "dark"];
      const labels = { auto: "🌓 Automatisch", light: "☀️ Hell", dark: "🌙 Dunkel" };
      const next = order[(order.indexOf(themeBtn.dataset.theme || "auto") + 1) % order.length];
      themeBtn.dataset.theme = next;
      themeBtn.textContent = labels[next];
      if (next === "auto") document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", next);
      document.cookie = "nk_theme=" + next + ";path=/;max-age=" + 400 * 86400 + ";samesite=lax";
    }
    // Navigationsgruppen: am Desktop nur eine offen, Klick daneben schließt
    const group = e.target.closest(".nav-group");
    if (!window.matchMedia("(max-width: 860px)").matches) {
      document.querySelectorAll(".nav-group[open]").forEach((g) => { if (g !== group) g.removeAttribute("open"); });
    }
    const toggle = e.target.closest(".menu-toggle");
    if (toggle) {
      const nav = toggle.closest("nav");
      const open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    if (standalone) document.documentElement.classList.add("standalone");
    // Handy: Gruppe der aktuellen Seite im Menü aufgeklappt
    if (window.matchMedia("(max-width: 860px)").matches) {
      document.querySelectorAll(".nav-group.active").forEach((g) => g.setAttribute("open", ""));
    }
    const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
    set("st-standalone", standalone ? "✔ Läuft als installierte App" : "ℹ Läuft im Browser");
    set("st-sw", "serviceWorker" in navigator
      ? (window.isSecureContext ? "✔ Offline-Hinweis aktiv (Service Worker)" : "✘ Service Worker nur über HTTPS")
      : "✘ Browser unterstützt keine Web-Apps");
    const hint = document.getElementById("st-hint");
    if (hint && window.isSecureContext && !standalone) setTimeout(() => { if (!deferred) hint.hidden = false; }, 1500);
  });

  /* ---- Push-Benachrichtigungen ---- */
  function b64ToBytes(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }
  async function pushSetup() {
    const card = document.getElementById("push-card");
    if (!card) return;
    const st = document.getElementById("push-status");
    const on = document.getElementById("push-on"), off = document.getElementById("push-off"),
      test = document.getElementById("push-test");
    const show = (txt, sub) => { st.textContent = txt; on.hidden = !!sub; off.hidden = !sub; test.hidden = !sub; };
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !window.isSecureContext) {
      const ios = /iPhone|iPad/.test(navigator.userAgent);
      st.textContent = ios && !standalone
        ? "Auf dem iPhone/iPad gehen Benachrichtigungen nur in der installierten App (Teilen → Zum Home-Bildschirm)."
        : "Dieser Browser bzw. diese Verbindung (HTTPS nötig) unterstützt keine Benachrichtigungen.";
      return;
    }
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (Notification.permission === "denied") {
      st.textContent = "Benachrichtigungen sind für diese Seite blockiert – in den Browser-/App-Einstellungen erlauben.";
      return;
    }
    show(sub ? "✔ Auf diesem Gerät aktiv." : "Auf diesem Gerät noch nicht aktiv.", sub);
    if (sub) {  // Abo beim Server auffrischen (z. B. nach neuem Login)
      fetch("/api/push/subscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sub) });
    }
    on.onclick = async () => {
      try {
        const perm = await Notification.requestPermission();
        if (perm !== "granted") { show("Nicht erlaubt – Benachrichtigungen wurden abgelehnt.", null); return; }
        const cfg = await fetch("/api/push/config").then((r) => r.json());
        sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(cfg.publicKey) });
        const r = await fetch("/api/push/subscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sub) });
        show(r.ok ? "✔ Aktiviert – du bekommst ab jetzt Benachrichtigungen." : "Fehler beim Anmelden am Server.", r.ok ? sub : null);
      } catch (e) { show("Aktivieren fehlgeschlagen: " + e.message, null); }
    };
    off.onclick = async () => {
      if (sub) {
        await fetch("/api/push/unsubscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint: sub.endpoint }) });
        await sub.unsubscribe();
        sub = null;
      }
      show("Auf diesem Gerät deaktiviert.", null);
    };
    test.onclick = async () => {
      const r = await fetch("/api/push/test", { method: "POST" }).then((x) => x.json()).catch(() => ({ ok: 0 }));
      st.textContent = r.ok ? "Test gesendet – die Benachrichtigung sollte gleich erscheinen."
        : "Test fehlgeschlagen" + (r.errors && r.errors.length ? ": " + r.errors[0] : "");
    };
  }
  document.addEventListener("DOMContentLoaded", () => { pushSetup().catch(() => {}); });
})();
